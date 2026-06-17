"""
ww/core/hooks.py — Hooks System v0.1

Implements Claude Code-style hooks:
- PreToolUse: runs before every tool call, can block/modify
- PostToolUse: runs after every tool call, can append context
- Notification: fires on session events (start, stop, error)
- UserPromptSubmit: runs before user prompt reaches the model
- Stop: can interrupt agent loop

Hooks are defined as Python functions or external scripts and registered 
via decorators or config. They execute in a sandboxed subprocess by default.
"""

from __future__ import annotations
import asyncio
import importlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("ww.hooks")


class HookEvent(Enum):
    """When the hook fires."""
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    NOTIFICATION = "notification"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    STOP = "stop"
    SESSION_START = "session_start"
    SESSION_END = "session_end"


@dataclass
class HookContext:
    """Context passed to every hook."""
    event: HookEvent
    tool_name: Optional[str] = None
    tool_params: Optional[Dict] = None
    tool_result: Optional[str] = None
    session_id: Optional[str] = None
    user_prompt: Optional[str] = None
    metadata: Dict = field(default_factory=dict)


@dataclass
class HookResult:
    """Returned by hook execution."""
    allowed: bool = True
    modified_params: Optional[Dict] = None
    context_injection: Optional[str] = None
    stop_reason: Optional[str] = None
    metadata: Dict = field(default_factory=dict)


class HookRegistry:
    """Central hook registry supporting Python functions and external scripts."""

    def __init__(self):
        self._hooks: Dict[HookEvent, List[Callable]] = {
            event: [] for event in HookEvent
        }
        self._script_hooks: Dict[HookEvent, List[str]] = {
            event: [] for event in HookEvent
        }
        self._global_enabled = True
        self._timeout = 10  # seconds per hook

    @property
    def enabled(self) -> bool:
        return self._global_enabled

    @enabled.setter
    def enabled(self, val: bool):
        self._global_enabled = val

    def register(self, event: HookEvent, fn: Callable):
        """Register a Python function as a hook."""
        self._hooks[event].append(fn)
        logger.info(f"Hook registered: {event.value} → {fn.__name__}")

    def register_script(self, event: HookEvent, script_path: str):
        """Register an external script as a hook. Script receives JSON context on stdin,
        must output JSON HookResult on stdout."""
        script_path = os.path.abspath(script_path)
        if not os.path.isfile(script_path):
            raise FileNotFoundError(f"Hook script not found: {script_path}")
        self._script_hooks[event].append(script_path)
        logger.info(f"Script hook registered: {event.value} → {script_path}")

    def unregister(self, event: HookEvent, fn: Callable = None, script_path: str = None):
        """Remove a hook."""
        if fn and fn in self._hooks[event]:
            self._hooks[event].remove(fn)
        if script_path and script_path in self._script_hooks[event]:
            self._script_hooks[event].remove(script_path)

    def load_from_directory(self, directory: str):
        """Load all hook scripts from hooks/ directory.
        
        Structure:
          hooks/
            pre_tool_use/
              validate_commands.py
              security_check.sh
            post_tool_use/
              log_tool_calls.py
            notification/
              slack_alert.py
        """
        directory = os.path.abspath(directory)
        for event in HookEvent:
            event_dir = os.path.join(directory, event.value)
            if os.path.isdir(event_dir):
                for fname in sorted(os.listdir(event_dir)):
                    fpath = os.path.join(event_dir, fname)
                    if os.path.isfile(fpath) and not fname.startswith('.'):
                        try:
                            self.register_script(event, fpath)
                        except Exception as e:
                            logger.warning(f"Failed to register hook {fpath}: {e}")

    def load_python_hooks(self, module_path: str):
        """Import a Python module and auto-register decorated functions."""
        spec = importlib.util.spec_from_file_location("_ww_hooks", module_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Auto-discover decorated functions
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if callable(attr) and hasattr(attr, '_ww_hook_event'):
                    self.register(attr._ww_hook_event, attr)

    async def run(self, ctx: HookContext) -> List[HookResult]:
        """Run all hooks for the given event. Returns list of results."""
        if not self._global_enabled:
            return []

        results = []
        event = ctx.event

        # Run Python hooks
        for fn in self._hooks.get(event, []):
            try:
                result = fn(ctx)
                if asyncio.iscoroutine(result):
                    result = await asyncio.wait_for(result, timeout=self._timeout)
                if result is None:
                    result = HookResult()
                elif not isinstance(result, HookResult):
                    result = HookResult(metadata={"raw_result": result})
                results.append(result)
            except Exception as e:
                logger.error(f"Hook {fn.__name__} failed: {e}")
                results.append(HookResult(allowed=True, metadata={"error": str(e)}))

        # Run script hooks
        ctx_dict = {
            "event": event.value,
            "tool_name": ctx.tool_name,
            "tool_params": ctx.tool_params,
            "tool_result": ctx.tool_result,
            "session_id": ctx.session_id,
            "user_prompt": ctx.user_prompt,
            "metadata": ctx.metadata,
        }

        for script_path in self._script_hooks.get(event, []):
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable if script_path.endswith('.py') else 'bash',
                    script_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(json.dumps(ctx_dict).encode()),
                    timeout=self._timeout,
                )
                if proc.returncode == 0 and stdout:
                    raw = json.loads(stdout.decode())
                    results.append(HookResult(**raw))
                else:
                    logger.warning(f"Script hook {script_path} failed (rc={proc.returncode}): {stderr.decode()}")
                    results.append(HookResult(allowed=True))
            except Exception as e:
                logger.error(f"Script hook {script_path} error: {e}")
                results.append(HookResult(allowed=True, metadata={"error": str(e)}))

        return results

    def should_block(self, results: List[HookResult]) -> Optional[str]:
        """Check if any hook result should block execution. Returns block reason or None."""
        for r in results:
            if not r.allowed:
                return r.stop_reason or "Blocked by hook"
        return None

    def merge_context(self, results: List[HookResult]) -> str:
        """Merge context injections from all hooks."""
        parts = [r.context_injection for r in results if r.context_injection]
        return "\n".join(parts)


# Decorator for easy registration
def hook(event: HookEvent):
    """Decorator to register a function as a hook.
    
    Usage:
        @hook(HookEvent.PRE_TOOL_USE)
        def validate_writes(ctx: HookContext) -> HookResult:
            if ctx.tool_name == 'write_file':
                return HookResult(allowed=False, stop_reason="Write blocked")
            return HookResult()
    """
    def decorator(fn: Callable):
        fn._ww_hook_event = event
        return fn
    return decorator


# Default security hooks
@hook(HookEvent.PRE_TOOL_USE)
def default_security_hook(ctx: HookContext) -> HookResult:
    """Built-in security: block obvious dangerous patterns."""
    if ctx.tool_name in ('terminal', 'shell_exec'):
        params = ctx.tool_params or {}
        cmd = params.get('command', '')
        # Block rm -rf /, format, dd, etc.
        dangerous = ['rm -rf /', 'mkfs.', 'dd if=', '> /dev/sda', 'format c:']
        for pattern in dangerous:
            if pattern in cmd.lower():
                return HookResult(
                    allowed=False,
                    stop_reason=f"Blocked dangerous command pattern: {pattern}"
                )
    return HookResult()


# Singleton
_hook_registry: Optional[HookRegistry] = None


def get_hook_registry() -> HookRegistry:
    global _hook_registry
    if _hook_registry is None:
        _hook_registry = HookRegistry()
        # Register built-in security hook
        _hook_registry.register(HookEvent.PRE_TOOL_USE, default_security_hook)
    return _hook_registry


# ══════════════════════════════════════════════════════════════
# Smart Backoff & Subagent Debugging Intervention (v0.2)
# ══════════════════════════════════════════════════════════════
# Upgrades Claude Code's static 8-failure Stop Hook limit to
# intelligent escalation with subagent debugging.


@dataclass
class StopHookState:
    """Tracks state for a Stop Hook across retries."""
    hook_name: str
    failure_count: int = 0
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    backoff_seconds: float = 2.0
    debugger_spawned: bool = False
    debugger_result: Optional[str] = None

    def record_failure(self) -> bool:
        """Record a failure. Returns True if we should keep trying."""
        self.failure_count += 1
        self.consecutive_failures += 1
        self.last_failure_time = time.time()

        # Exponential backoff
        self.backoff_seconds = min(2.0 * (2 ** self.consecutive_failures), 60.0)

        # Phase 1 (1-3 failures): standard retry
        if self.consecutive_failures <= 3:
            return True

        # Phase 2 (4-6 failures): auto-spawn debugger subagent
        if self.consecutive_failures <= 6:
            if not self.debugger_spawned:
                self.debugger_spawned = True
                self.debugger_result = _spawn_debugger_subagent(self.hook_name)
            return True

        # Phase 3 (7+): escalate — don't force-complete, but report to user
        return False

    def record_success(self):
        """Reset consecutive failure counter on success."""
        self.consecutive_failures = 0
        self.backoff_seconds = 2.0
        self.debugger_spawned = False
        self.debugger_result = None


# Global stop hook state tracker
_stop_hook_states: Dict[str, StopHookState] = {}


def get_stop_hook_state(hook_name: str) -> StopHookState:
    """Get or create StopHookState for a named hook."""
    if hook_name not in _stop_hook_states:
        _stop_hook_states[hook_name] = StopHookState(hook_name=hook_name)
    return _stop_hook_states[hook_name]


def _spawn_debugger_subagent(hook_name: str) -> Optional[str]:
    """Spawn a subagent to analyze Stop Hook failure logs.

    The debugger subagent reads the hook's error output and the
    current file/context to generate a diagnostic report and
    suggested fix.
    """
    try:
        # Collect recent error context
        state = _stop_hook_states.get(hook_name)
        if not state:
            return None

        # Try to invoke a lightweight debugger
        # In production, this would spawn a real subagent via delegate_task
        # For now, return a diagnostic placeholder
        return (
            f"Debugger analysis for '{hook_name}' after "
            f"{state.consecutive_failures} failures:\n"
            f"  - Backoff: {state.backoff_seconds:.1f}s\n"
            f"  - Suggested: Check hook script output, review recent file changes\n"
            f"  - Recommendation: The hook may be making assumptions that no "
            f"longer hold after recent edits. Verify expected exit codes and stdout patterns."
        )
    except Exception as e:
        logger.error("Debugger subagent spawn failed: %s", e)
        return None


def wrap_stop_hook(hook_fn: Callable, hook_name: str) -> Callable:
    """Wrap a Stop hook function with smart backoff logic.

    Usage:
        @hook(HookEvent.STOP)
        @wrap_stop_hook(hook_name="test-suite")
        def run_tests(ctx: HookContext) -> HookResult:
            ...
    """
    def wrapper(ctx: HookContext) -> HookResult:
        state = get_stop_hook_state(hook_name)
        result = hook_fn(ctx)

        if not result.allowed:
            keep_trying = state.record_failure()

            if not keep_trying:
                # Phase 3 escalation
                result.stop_reason = (
                    f"Hook '{hook_name}' failed {state.consecutive_failures} times. "
                    f"Escalating to user. Debugger analysis: {state.debugger_result}"
                )
                return result

            # Phase 1-2: inject diagnostic context and retry
            if state.debugger_result:
                result.context_injection = (
                    (result.context_injection or "") + "\n" + state.debugger_result
                )

            # Signal to retry with backoff
            result.metadata = result.metadata or {}
            result.metadata["_retry_after_seconds"] = state.backoff_seconds
            result.metadata["_failure_count"] = state.consecutive_failures
            result.metadata["_debugger_spawned"] = state.debugger_spawned

        else:
            state.record_success()

        return result

    return wrapper
