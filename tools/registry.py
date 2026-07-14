"""ww/tools/registry.py — Worldwave tool registry v0.3

WW's 'hands' — all tools that interact with the outside world register here.
Full coverage of agent capabilities (~35+ tools), divided into 10 categories.

Each tool is a {name, description, parameters, handler} structure.
The Act phase uses this registry to discover and call tools."""

from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ww.tools.registry")

# ── permission level ──────────────────────────────────────
PERMISSION_SAFE = "safe"
PERMISSION_APPROVAL = "requires_approval"
PERMISSION_DESTRUCTIVE = "destructive"


# ── tool definition structure ──────────────────────────────────────

class ToolDef:
    """A tool definition."""
    def __init__(
        self,
        name: str,
        description: str,
        handler: Callable,
        parameters: Dict[str, Any] = None,
        examples: List[str] = None,
        category: str = "general",
        permission: str = PERMISSION_SAFE,
    ):
        self.name = name
        self.description = description
        self.handler = handler
        self.parameters = parameters or {}
        self.examples = examples or []
        self.category = category
        self.permission = permission  # safe / requires_approval / destructive

    def to_prompt_block(self) -> str:
        """Format into LLM prompt readable tool description."""
        params_str = json.dumps(self.parameters, indent=2) if self.parameters else "{}"
        perm_str = "" if self.permission == PERMISSION_SAFE else " ⚠️[" + self.permission + "]"
        lines = ["## " + self.name + perm_str + " (" + self.category + ")", self.description, "", "parameters:", params_str]
        if self.examples:
            lines.append("")
            lines.append("Example:")
            for ex in self.examples[:3]:
                lines.append("  -> " + ex)
        return "\n".join(lines)

    def __repr__(self):
        return "<Tool:" + self.name + " cat:" + self.category + ">"


# ── Tool registration table ─────────────────────────────────────────

class ToolRegistry:
    """
    Tool registration table.
    
    WW via here discover available tools, validate parameters, execute calls.
    similar to Hermes function calling but more lightweight.
    """

    def __init__(self):
        self._tools: Dict[str, ToolDef] = {}
        self.guardrails = None
        self.approval_callback = None  # async: fn(tool_name, params) -> bool
        self._pending_approvals = {}  # {req_id: {tool_name, params, timestamp}}
        self.approval_mode = "auto"   # auto= silent execute, hitl=needs confirmation, deny=block all high risk
        # Allow production override without code change
        env_mode = os.environ.get("WW_APPROVAL_MODE", "").strip().lower()
        if env_mode in ("auto", "hitl", "deny"):
            self.approval_mode = env_mode
        self.suspend_callback = None  # fn(tool_name, params, cp_id) — save checkpoint arousal

    def register(self, tool: ToolDef):
        """registera tool. """
        self._tools[tool.name] = tool

    def register_from_def(self, name: str, description: str, handler: Callable,
                          parameters: Dict = None, examples: List[str] = None,
                          category: str = "general", permission: str = PERMISSION_SAFE):
        """Quick register."""
        self.register(ToolDef(name, description, handler, parameters, examples, category, permission))

    def get(self, name: str) -> Optional[ToolDef]:
        """Get tool definition."""
        return self._tools.get(name)

    def list_tools(self) -> List[ToolDef]:
        """List all registered tools."""
        return list(self._tools.values())

    def to_openai_tools(self) -> list:
        """Convert registered tools to OpenAI function-calling format."""
        tools = []
        for t in self._tools.values():
            if t.parameters:
                if isinstance(t.parameters, dict):
                    # If parameters is already a full JSON Schema (has "type" + "properties"),
                    # pass it through unchanged to avoid double-wrapping.
                    if "type" in t.parameters and "properties" in t.parameters:
                        params = t.parameters
                    else:
                        # Flat dict of {param_name: param_schema} — wrap into JSON Schema
                        params = {"type": "object", "properties": {}, "required": []}
                        for k, v in t.parameters.items():
                            if isinstance(v, dict):
                                params["properties"][k] = v
                            else:
                                params["properties"][k] = {"type": "string", "description": str(v)}
                            params["required"].append(k)
                else:
                    # Non-dict parameters — wrap as single string input
                    params = {"type": "object", "properties": {
                        "input": {"type": "string", "description": str(t.parameters)}
                    }}
            else:
                params = {"type": "object", "properties": {}, "required": []}
            tools.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description[:1024],
                    "parameters": params,
                }
            })
        return tools

    def list_by_category(self, category: str) -> List[ToolDef]:
        """Filter tools by category."""
        return [t for t in self._tools.values() if t.category == category]

    def set_guardrails(self, guardrails):
        """Attach a Guardrails instance"""
        self.guardrails = guardrails

    def set_approval_mode(self, mode: str):
        """setting permission mode: auto(approve) / hitl(needs confirmation) / deny(reject)."""
        if mode not in ("auto", "hitl", "deny"):
            raise ValueError("mode must be auto/hitl/deny")
        self.approval_mode = mode

    def set_approval_callback(self, callback):
        """setting sync HITL callback: fn(tool_name, params) -> bool (True=approve, False=reject)"""
        self.approval_callback = callback

    def set_suspend_callback(self, callback):
        """setting async deferred callback: fn(tool_name, params) -> str (return checkpoint_id for resumption)"""
        self.suspend_callback = callback

    def _check_permission(self, tool: ToolDef, params: Dict = None) -> Dict:
        """
        permissioncheck. If HITL interception is needed, return error.
        Returns: None if approved, or {"success": False, "error": ..., "blocked": True, "approval_required": ...}
        """
        if tool.permission == PERMISSION_SAFE:
            return None  # secure tool, no interception

        if self.approval_mode == "deny":
            return {"success": False, "error": f"securePolicy: [{tool.permission}] {tool.name}   managementMember prohibited",
                    "blocked": True, "block_reason": "denied_by_policy"}

        if self.approval_mode == "auto":
            return None  # authorized mode, silent execute

        # HITL mode — require human approval
        # Prefer using async suspension (suspend + checkpoint)
        if self.suspend_callback:
            cp_id = self.suspend_callback(tool.name, params or {})
            return {"success": False, "error": f"needs approval: [{tool.permission}] {tool.name}",
                    "blocked": True, "block_reason": "async_suspend",
                    "approval_required": True, "approval_id": cp_id,
                    "permission_level": tool.permission,
                    "tool_name": tool.name,
                    "checkpoint_id": cp_id,
                    "suspend": True}  # suspend=True tells the caller: go to sleep, wait for human reply

        # Fallback: synccallback
        if self.approval_callback:
            approved = self.approval_callback(tool.name, params or {})
            if approved:
                return None  # human approved
            return {"success": False, "error": f"securePolicy: [{tool.permission}] {tool.name}   userreject",
                    "blocked": True, "block_reason": "denied_by_user"}

        # No callback but HITL mode — generate pending review request
        import uuid
        req_id = uuid.uuid4().hex[:8]
        self._pending_approvals[req_id] = {
            "tool": tool.name,
            "params": params or {},
            "timestamp": time.time(),
            "status": "pending",
        }
        return {"success": False, "error": f"needs approval: [{tool.permission}] {tool.name}",
                "blocked": True, "block_reason": "pending_approval",
                "approval_required": True, "approval_id": req_id,
                "permission_level": tool.permission,
                "tool_name": tool.name}

    def call(self, name: str, params: Dict = None, use_sandbox: bool = False) -> Dict[str, Any]:
        """
        Call a tool (including securecheck).

        Args:
            name: Tool name
            params: Tool parameters dict
            use_sandbox: If True, execute in isolated sandbox (Codex Seatbelt-style)

        Returns:
            {"success": bool, "output": str, "error": str, "data": Any}
        """
        # Guardrails check
        if self.guardrails and hasattr(self.guardrails, 'enabled') and self.guardrails.enabled:
            if name == "shell":
                cmd = (params or {}).get("command", "")
                check = self.guardrails.check_shell_command(cmd)
                if not check:
                    return {"success": False, "error": f"secureGuardrail: {check.reason}",
                            "guardrails_blocked": True}
            elif name == "file_write":
                path = (params or {}).get("path", "")
                check = self.guardrails.check_file_write(path)
                if not check:
                    return {"success": False, "error": f"secureGuardrail: {check.reason}",
                            "guardrails_blocked": True}
            elif name == "code":
                code = (params or {}).get("code", "")
                check = self.guardrails.check_code(code)
                if not check:
                    return {"success": False, "error": f"secureGuardrail: {check.reason}",
                            "guardrails_blocked": True}

        tool = self.get(name)
        if not tool:
            return {"success": False, "error": "unknown tool: " + name}

        # permissioncheck (Phase 1: Exceeding guardrails - outer HITL interception)
        perm_check = self._check_permission(tool, params)
        if perm_check is not None:
            return perm_check

        # CapabilityMutex check (Phase 2: Role-based tool access enforcement)
        cap_mutex_check = self._check_capability_mutex(tool)
        if cap_mutex_check is not None:
            return cap_mutex_check

        # Hooks: PreToolUse (Phase 3: Claude Code-style hook pipeline)
        hook_results = self._run_pre_tool_hooks(tool, params)
        for hr in hook_results:
            if not hr.get("allowed", True):
                return {"success": False, "error": f"Hook blocked: {hr.get('stop_reason', 'denied')}",
                        "hook_blocked": True}
            if hr.get("modified_params"):
                params = {**params, **hr["modified_params"]}

        # Sandbox wrap (Phase 4: Codex Seatbelt-style isolation)
        if use_sandbox:
            return self._execute_in_sandbox(tool, params)

        try:
            result = tool.handler(**(params or {}))
            if isinstance(result, dict):
                # Normalize return format: ensure success key exists
                if "success" not in result:
                    if "error" in result:
                        result["success"] = False
                    elif "stderr" in result and result.get("stderr"):
                        result["success"] = "exit_code" not in result or result.get("exit_code", 0) == 0
                    else:
                        result["success"] = True
                self._run_post_tool_hooks(tool, params, result)
                return result
            result = {"success": True, "output": str(result), "data": result}
            self._run_post_tool_hooks(tool, params, result)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _execute_in_sandbox(self, tool, params: Dict) -> Dict[str, Any]:
        """Execute tool handler inside an isolated sandbox (Seatbelt mode)."""
        try:
            from coding.sandbox import Sandbox

            sb = Sandbox(timeout=60, memory_limit_mb=256)
            try:
                import json as _json
                params_json = _json.dumps(params or {})

                script = (
                    "import json\n"
                    "with open('__params.json') as f:\n"
                    "    _params = json.load(f)\n"
                    "_module = __import__('tools.registry', fromlist=['ToolRegistry'])\n"
                    "_reg = _module.ToolRegistry()\n"
                    "_tool = _reg.get(" + _json.dumps(tool.name) + ")\n"
                    "if _tool:\n"
                    "    _result = _tool.handler(**_params)\n"
                    "print(json.dumps(_result))\n"
                )
                result = sb.execute(
                    "python3 __script.py",
                    files={"__script.py": script, "__params.json": params_json},
                )
                if result.output:
                    return _json.loads(result.output)
                return {"success": False, "error": result.error or "sandbox error"}
            finally:
                sb.clean()
        except ImportError:
            logger.warning("Sandbox (coding.sandbox) import failed, executing unsandboxed with guardrails")
        except Exception as e:
            return {"success": False, "error": f"sandbox error: {e}"}

        # Fallback to unsandboxed
        return ToolRegistry().call(tool.name, params, use_sandbox=False)

    def _check_capability_mutex(self, tool) -> Optional[Dict]:
        """Enforce CapabilityMutex tool access control.

        If the WW-PM SandboxManager/CapabilityMutex is active and a role is set,
        verify the current role is allowed to use this tool.
        Returns None if allowed, or a blocked-result dict if denied.
        Ref: Blueprint — "If an orchestrator agent has the Task tool,
        it is physically stripped of Edit or Write permissions."
        """
        try:
            from coding.sandbox import get_manager
            mgr = get_manager()
            if mgr and mgr.mutex:
                mutex = mgr.mutex
                if mutex.role:
                    allowed = mutex.can_use_tool(tool.name)
                    if not allowed:
                        return {
                            "success": False,
                            "error": (
                                f"CapabilityMutex: role '{mutex.role}' cannot use "
                                f"tool '{tool.name}'. Allowed caps: {sorted(mutex._capabilities)}"
                            ),
                            "blocked": True,
                            "block_reason": "capability_mutex",
                        }
        except ImportError:
            pass
        return None

    def _run_pre_tool_hooks(self, tool, params: Dict) -> List[Dict]:
        """Run PreToolUse hooks (Claude Code-style)."""
        try:
            from core.hooks import get_hook_registry, HookEvent, HookContext
            reg = get_hook_registry()
            if not reg or not reg.enabled:
                return []
            ctx = HookContext(
                event=HookEvent.PRE_TOOL_USE,
                tool_name=tool.name,
                tool_params=params,
                session_id=getattr(self, '_session_id', None),
            )
            import asyncio
            results = asyncio.run(reg.run(ctx))
            return [{
                "allowed": r.allowed,
                "modified_params": r.modified_params,
                "context_injection": r.context_injection,
                "stop_reason": r.stop_reason,
            } for r in results]
        except Exception:
            return []

    def _run_post_tool_hooks(self, tool, params: Dict, result: Dict) -> List[Dict]:
        """Run PostToolUse hooks after tool execution."""
        try:
            from core.hooks import get_hook_registry, HookEvent, HookContext
            reg = get_hook_registry()
            if not reg or not reg.enabled:
                return []
            ctx = HookContext(
                event=HookEvent.POST_TOOL_USE,
                tool_name=tool.name,
                tool_params=params,
                tool_result=str(result)[:500],
                session_id=getattr(self, '_session_id', None),
            )
            import asyncio
            results = asyncio.run(reg.run(ctx))
            return [{
                "context_injection": r.context_injection,
                "metadata": r.metadata,
            } for r in results]
        except Exception:
            return []

    def prompt_block(self, exclude_categories: Optional[List[str]] = None) -> str:
        """Complete tool list description (for LLM to view).

        Args:
            exclude_categories: optional list of category names to exclude
                                (e.g. ['dangerous'] for Tier 2 downgrade).
        """
        exclude = set(exclude_categories or [])
        # Group by category
        cats = {}
        for tool in self._tools.values():
            if tool.category in exclude:
                continue
            cats.setdefault(tool.category, []).append(tool)
        
        lines = ["# availabletool\n"]
        for cat, tools in sorted(cats.items()):
            lines.append("## === " + cat.upper() + " ===")
            for tool in tools:
                lines.append(tool.to_prompt_block())
                lines.append("")
        return "\n".join(lines)

    def tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def category_counts(self) -> Dict[str, int]:
        counts = {}
        for t in self._tools.values():
            counts[t.category] = counts.get(t.category, 0) + 1
        return counts


# ════════════════════════════════════════════════════════════
# Built-in tool implementation
# ════════════════════════════════════════════════════════════

# ── 1. SHELL & system ───────────────────────────────────

def _shell_handler(command: str, timeout: int = 30, workdir: str = "") -> Dict:
    """execute shell command. """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir or None,
        )
        return {
            "success": result.returncode == 0,
            "output": (result.stdout + result.stderr)[:3000],
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout (" + str(timeout) + "s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _system_status_handler() -> Dict:
    """getsystemstate (CPU/RAM/Disk/Network/Process) . """
    try:
        results = {}
        commands = {
            "hostname": "hostname 2>/dev/null",
            "uptime": "uptime 2>/dev/null",
            "memory": "free -h 2>/dev/null",
            "disk": "df -h / 2>/dev/null | tail -1",
            "load": "cat /proc/loadavg 2>/dev/null",
            "top_cpu": "ps aux --sort=-%cpu 2>/dev/null | head -6",
            "top_mem": "ps aux --sort=-%mem 2>/dev/null | head -6",
            "network": "ip -br addr 2>/dev/null | grep -v lo",
        }
        for label, cmd in commands.items():
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            results[label] = (r.stdout or r.stderr)[:300]
        return {"success": True, "output": json.dumps(results, indent=2), "data": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _env_info_handler() -> Dict:
    """System environment information."""
    try:
        info = {
            "python_version": sys.version,
            "platform": sys.platform,
            "hostname": os.uname().nodename,
            "os": os.uname().sysname + " " + os.uname().release,
            "arch": os.uname().machine,
            "cwd": os.getcwd(),
            "user": os.environ.get("USER", "?"),
            "home": os.environ.get("HOME", "?"),
            "shell": os.environ.get("SHELL", "?"),
            "path_count": len(os.environ.get("PATH", "").split(":")),
            "term": os.environ.get("TERM", "?"),
        }
        return {"success": True, "output": json.dumps(info, indent=2), "data": info}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _which_handler(command: str) -> Dict:
    """Locate executable file path."""
    try:
        path = shutil.which(command)
        if path:
            r = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=5)
            version = (r.stdout or r.stderr)[:200]
            return {"success": True, "output": path, "data": {"path": path, "version": version}}
        return {"success": False, "error": command + " not found in PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _process_list_handler(filter_str: str = "") -> Dict:
    """List execute programs."""
    try:
        cmd = "ps aux --sort=-%cpu 2>/dev/null"
        if filter_str:
            cmd += " | grep -i " + filter_str
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        lines = [l for l in r.stdout.split("\n") if l.strip()]
        return {"success": True, "output": "\n".join(lines[:30]), "count": len(lines)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _process_kill_handler(pid: int, signal: str = "TERM") -> Dict:
    """Terminate process."""
    try:
        sig_map = {"TERM": 15, "KILL": 9, "HUP": 1, "INT": 2}
        sig_num = sig_map.get(signal.upper(), 15)
        os.kill(pid, sig_num)
        return {"success": True, "output": "signal " + signal + " sent to PID " + str(pid)}
    except ProcessLookupError:
        return {"success": False, "error": "PID " + str(pid) + " not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 2. FILE Operations ──────────────────────────────────────

def _read_file_handler(path: str) -> Dict:
    """readfile. """
    try:
        p = os.path.expanduser(path)
        with open(p) as f:
            content = f.read()
        return {
            "success": True,
            "output": content[:3000],
            "data": {"size": len(content), "truncated": len(content) > 3000},
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _write_file_handler(path: str, content: str) -> Dict:
    """Write file with syntax validation via DefensiveEditor.

    Uses coding/aci.py DefensiveEditor for:
    1. Syntax validation before write
    2. Atomic temp-file write
    3. Auto-rollback if validation fails
    Falls back to direct write if DefensiveEditor unavailable.
    """
    try:
        p = os.path.expanduser(path)
        # Try DefensiveEditor first (syntax-validated atomic write)
        try:
            from coding.aci import DefensiveEditor
            editor = DefensiveEditor(lint_enabled=True)
            result = editor.write_file(p, content)
            if result.get("success"):
                return {"success": True, "output": "wrote " + p + " (" + str(len(content)) + " chars) [syntax-validated]", "data": result}
            else:
                return {"success": False, "error": "Syntax validation failed: " + str(result.get("errors", []))}
        except ImportError:
            pass
        # Fallback: direct write
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return {"success": True, "output": "wrote " + p + " (" + str(len(content)) + " chars)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _list_files_handler(path: str, depth: int = 1, pattern: str = "") -> Dict:
    """List directory content. Supports pattern filter."""
    try:
        p = os.path.expanduser(path)
        if not os.path.isdir(p):
            return {"success": False, "error": "not a directory: " + p}
        
        cmd = ["find", p, "-maxdepth", str(depth)]
        cmd += ["-type", "f", "-o", "-type", "l"]
        if pattern:
            cmd += ["-name", pattern]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        files = [f for f in r.stdout.strip().split("\n") if f]
        
        # Basic statistics
        total_size = sum(os.path.getsize(f) for f in files[:200] if os.path.isfile(f))
        result = {
            "count": len(files),
            "total_size_bytes": total_size,
            "files": files[:50],
        }
        if len(files) > 50:
            result["note"] = "+" + str(len(files) - 50) + " more results"
        return {"success": True, "output": json.dumps(result, indent=2), "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _search_files_handler(pattern: str, path: str = ".", file_glob: str = "",
                          max_results: int = 20) -> Dict:
    """searchfilecontent (similar to  grep -r) . """
    try:
        p = os.path.expanduser(path)
        cmd = ["grep", "-rn", "--max-count=3", pattern, p]
        if file_glob:
            cmd += ["--include=" + file_glob]
        cmd += ["--exclude-dir=.git", "--exclude-dir=__pycache__",
                "--exclude-dir=node_modules", "--exclude-dir=.venv"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = [l for l in r.stdout.split("\n") if l.strip()]
        matches = lines[:max_results]
        result = {
            "count": len(lines),
            "matches": matches,
        }
        if len(lines) > max_results:
            result["note"] = "+" + str(len(lines) - max_results) + " more matches"
        return {"success": True, "output": json.dumps(result, indent=2), "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _patch_handler(path: str, old_string: str, new_string: str) -> Dict:
    """Find and replace file text. Uses DefensiveEditor for syntax validation."""
    try:
        p = os.path.expanduser(path)
        with open(p) as f:
            content = f.read()
        
        if old_string not in content:
            return {"success": False, "error": "old_string not found in " + p}
        
        new_content = content.replace(old_string, new_string, 1)
        
        # Validate through DefensiveEditor if available
        try:
            from coding.aci import DefensiveEditor
            editor = DefensiveEditor(lint_enabled=True)
            check = editor._validate_syntax(p, new_content)
            if not check.valid:
                return {"success": False, "error": "Syntax validation failed: " + str(check.errors), "rollback": True}
        except ImportError:
            pass
        
        with open(p, "w") as f:
            f.write(new_content)
        
        changes = content.count(old_string)
        return {
            "success": True,
            "output": "patched " + p + " (" + str(changes) + " occurrence" + ("s" if changes > 1 else "") + ")",
            "data": {"changes": changes},
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _file_info_handler(path: str) -> Dict:
    """Get file/directory detail information."""
    try:
        p = os.path.expanduser(path)
        s = os.stat(p)
        info = {
            "path": p,
            "size_bytes": s.st_size,
            "type": "directory" if os.path.isdir(p) else "file" if os.path.isfile(p) else "other",
            "modified": datetime.fromtimestamp(s.st_mtime).isoformat(),
            "created": datetime.fromtimestamp(s.st_ctime).isoformat(),
            "accessed": datetime.fromtimestamp(s.st_atime).isoformat(),
            "permissions": oct(s.st_mode)[-3:],
            "owner": s.st_uid,
        }
        if os.path.isfile(p):
            with open(p) as f:
                info["line_count"] = sum(1 for _ in f)
        return {"success": True, "output": json.dumps(info, indent=2), "data": info}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _hash_file_handler(path: str, algorithm: str = "sha256") -> Dict:
    """Calculate file hash."""
    try:
        p = os.path.expanduser(path)
        h = hashlib.new(algorithm)
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return {"success": True, "output": h.hexdigest(), "data": {"algorithm": algorithm, "hash": h.hexdigest()}}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _copy_handler(src: str, dst: str, recursive: bool = False) -> Dict:
    """Copy file or directory."""
    try:
        src_p = os.path.expanduser(src)
        dst_p = os.path.expanduser(dst)
        if recursive and os.path.isdir(src_p):
            shutil.copytree(src_p, dst_p, dirs_exist_ok=True)
        else:
            shutil.copy2(src_p, dst_p)
        return {"success": True, "output": "copied " + src_p + " -> " + dst_p}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _move_handler(src: str, dst: str) -> Dict:
    """move/rename file or directory."""
    try:
        shutil.move(os.path.expanduser(src), os.path.expanduser(dst))
        return {"success": True, "output": "moved " + src + " -> " + dst}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _delete_handler(path: str, recursive: bool = False) -> Dict:
    """delete file or directory."""
    try:
        p = os.path.expanduser(path)
        if os.path.isdir(p) and recursive:
            shutil.rmtree(p)
        elif os.path.isfile(p):
            os.remove(p)
        else:
            return {"success": False, "error": p + " not found or need recursive=True for directories"}
        return {"success": True, "output": "deleted " + p}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 3. GIT ─────────────────────────────────────────────

def _git_status_handler(path: str = ".") -> Dict:
    """Git state. """
    try:
        p = os.path.expanduser(path)
        r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, timeout=10, cwd=p)
        branch_r = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=5, cwd=p)
        return {
            "success": True,
            "output": "branch: " + (branch_r.stdout or "?").strip() + "\n" + (r.stdout or "(clean)"),
            "data": {"branch": branch_r.stdout.strip(), "changes": r.stdout.strip()},
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _git_log_handler(path: str = ".", count: int = 10) -> Dict:
    """Git commit history."""
    try:
        p = os.path.expanduser(path)
        r = subprocess.run(
            ["git", "log", "--oneline", "--graph", "-" + str(count)],
            capture_output=True, text=True, timeout=10, cwd=p,
        )
        return {"success": True, "output": r.stdout[:3000]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _git_diff_handler(path: str = ".", staged: bool = False) -> Dict:
    """Git change diff."""
    try:
        p = os.path.expanduser(path)
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=p)
        output = r.stdout[:3000]
        stats_r = subprocess.run(["git", "diff", "--stat"] + (["--cached"] if staged else []),
                                  capture_output=True, text=True, timeout=5, cwd=p)
        return {"success": True, "output": (stats_r.stdout + "\n" + output)[:3000]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _git_commit_handler(path: str = ".", message: str = "") -> Dict:
    """Git add + commit. """
    try:
        p = os.path.expanduser(path)
        add_r = subprocess.run(["git", "add", "-A"], capture_output=True, text=True, timeout=10, cwd=p)
        if add_r.returncode != 0:
            return {"success": False, "error": "git add failed: " + add_r.stderr}
        r = subprocess.run(["git", "commit", "-m", message or "ww auto commit"],
                           capture_output=True, text=True, timeout=10, cwd=p)
        return {"success": r.returncode == 0, "output": (r.stdout or r.stderr)[:1000]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _git_push_handler(path: str = ".", remote: str = "origin", branch: str = "") -> Dict:
    """Git push. """
    try:
        p = os.path.expanduser(path)
        if not branch:
            r = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=5, cwd=p)
            branch = r.stdout.strip()
        cmd = ["git", "push", remote, branch]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=p)
        return {"success": r.returncode == 0, "output": (r.stdout or r.stderr)[:1000]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _git_pull_handler(path: str = ".") -> Dict:
    """Git pull. """
    try:
        p = os.path.expanduser(path)
        r = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=30, cwd=p)
        return {"success": r.returncode == 0, "output": (r.stdout or r.stderr)[:1000]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _git_clone_handler(url: str, dest: str = "", branch: str = "") -> Dict:
    """Git clone repository. """
    try:
        cmd = ["git", "clone", url]
        if dest:
            cmd.append(os.path.expanduser(dest))
        if branch:
            cmd += ["--branch", branch]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {"success": r.returncode == 0, "output": (r.stdout or r.stderr)[:1000]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 4. NETWORK ────────────────────────────────────────

def _http_request_handler(url: str, method: str = "GET", headers: Dict = None,
                          body: str = "", timeout: int = 15) -> Dict:
    """send HTTP request. supports GET/POST/PUT/DELETE. """
    try:
        cmd = ["curl", "-s", "-L", "-w", "\\n%{http_code}", "-X", method]
        if headers:
            for k, v in headers.items():
                cmd += ["-H", k + ": " + v]
        if method in ("POST", "PUT") and body:
            cmd += ["-d", body]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and not r.stdout:
            return {"success": False, "error": "curl failed: " + (r.stderr or "?")}
        
        lines = r.stdout.strip().split("\n")
        status_code = lines[-1] if lines else "000"
        response_body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
        
        return {
            "success": True,
            "output": response_body[:3000],
            "data": {"status_code": int(status_code), "body": response_body[:3000]},
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "HTTP timeout (" + str(timeout) + "s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fetch_url_handler(url: str, timeout: int = 15) -> Dict:
    """fetch web page content (plain text HTML with tags removed)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", url],
            capture_output=True, text=True, timeout=timeout,
        )
        html = result.stdout
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()[:3000]
        return {"success": True, "output": text, "source": url}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "fetch timeout (" + str(timeout) + "s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _ping_handler(host: str, count: int = 4, timeout: int = 10) -> Dict:
    """Ping host. """
    try:
        r = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), host],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return {
            "success": r.returncode == 0,
            "output": (r.stdout or r.stderr)[:1000],
            "data": {"reachable": r.returncode == 0},
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _dns_lookup_handler(host: str, type: str = "A") -> Dict:
    """DNS query. """
    try:
        if type == "A":
            result = socket.getaddrinfo(host, 80)
            ips = list(set(r[4][0] for r in result))
            return {"success": True, "output": host + " -> " + ", ".join(ips), "data": {"ips": ips}}
        else:
            r = subprocess.run(["dig", "+short", "-t", type, host],
                               capture_output=True, text=True, timeout=10)
            return {"success": True, "output": (r.stdout or r.stderr)[:500]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 5. UTILITY ─────────────────────────────────────────

def _uuid_handler() -> Dict:
    """generate UUID v4. """
    return {"success": True, "output": str(uuid.uuid4())}


def _timestamp_handler(format: str = "iso") -> Dict:
    """when timestamp."""
    now = datetime.now(timezone.utc)
    if format == "iso":
        output = now.isoformat()
    elif format == "unix":
        output = str(int(now.timestamp()))
    elif format == "readable":
        output = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        output = now.isoformat()
    return {"success": True, "output": output}


def _time_calc_handler(expression: str) -> Dict:
    """  calculate. For example: 'now + 1h', 'now - 30m', '2024-01-01 + 7d'."""
    try:
        now = datetime.now(timezone.utc)
        expr = expression.strip()
        
        # resolverelative  
        match = re.match(r'now\s*([+-])\s*(\d+)\s*(s|m|h|d|w)', expr)
        if match:
            op, val, unit = match.group(1), int(match.group(2)), match.group(3)
            td_map = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
            from datetime import timedelta
            td = timedelta(**{td_map[unit]: val})
            result = now + td if op == "+" else now - td
            return {"success": True, "output": result.isoformat(), "data": {"iso": result.isoformat(), "unix": int(result.timestamp())}}
        
        # resolve absolute difference
        match = re.match(r'(\d{4}-\d{2}-\d{2}(?:T[\d:]+)?)\s*->\s*(\d{4}-\d{2}-\d{2}(?:T[\d:]+)?)', expr)
        if match:
            fmt = "%Y-%m-%dT%H:%M:%S" if "T" in expr else "%Y-%m-%d"
            t1 = datetime.strptime(match.group(1)[:19], fmt[:len(match.group(1))])
            t2 = datetime.strptime(match.group(2)[:19], fmt[:len(match.group(2))])
            delta = t2 - t1
            return {"success": True, "output": str(delta), "data": {"days": delta.days, "seconds": delta.seconds, "total_hours": delta.total_seconds() / 3600}}
        
        return {"success": False, "error": "unrecognized expression: " + expr}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _base64_handler(action: str, data: str) -> Dict:
    """Base64 encode/decode. """
    try:
        if action == "encode":
            result = base64.b64encode(data.encode()).decode()
        elif action == "decode":
            result = base64.b64decode(data).decode()
        else:
            return {"success": False, "error": "action must be encode or decode"}
        return {"success": True, "output": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _json_tool_handler(action: str, data: str) -> Dict:
    """JSON format/validate/compress."""
    try:
        parsed = json.loads(data)
        if action == "format":
            result = json.dumps(parsed, indent=2, ensure_ascii=False)
        elif action == "compact":
            result = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        elif action == "validate":
            return {"success": True, "output": "valid JSON", "data": {"type": type(parsed).__name__}}
        else:
            return {"success": False, "error": "action must be format/compact/validate"}
        return {"success": True, "output": result}
    except json.JSONDecodeError as e:
        return {"success": False, "error": "invalid JSON: " + str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 6. WEB SEARCH ─────────────────────────────────────

def _web_search_handler(query: str) -> Dict:
    """search web pages (no API key required, plain text results)."""
    try:
        encoded = query.replace(" ", "+")
        result = subprocess.run(
            ["curl", "-s", "-L",
             "https://lite.duckduckgo.com/lite/?q=" + encoded],
            capture_output=True, text=True, timeout=15,
        )
        html = result.stdout
        lines = [l.strip() for l in html.split("\n") if l.strip() and not l.strip().startswith("<")]
        text = "\n".join(lines[:50])
        return {"success": True, "output": text[:3000], "source": "duckduckgo"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 7. CODE execute ──────────────────────────────────────

def _code_handler(code: str, timeout: int = 30) -> Dict:
    """execute Python code (sandbox mode)."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "output": (result.stdout or "")[:3000],
            "error": (result.stderr or "")[:500],
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "code timeout (" + str(timeout) + "s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 8. MEMORY ──────────────────────────────────────────

MEMORY_V2_URL = os.environ.get("WW_MEMORY_URL", "http://localhost:9200")

def _memory_store_handler(content: str, category: str = "general",
                          tags: str = "", importance: float = 0.5) -> Dict:
    """Save memory to bionic memory system v2."""
    try:
        import urllib.request
        payload = json.dumps({
            "content": content,
            "category": category,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "importance": importance,
        }).encode()
        req = urllib.request.Request(
            MEMORY_V2_URL + "/v2/store",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": "memory store failed: " + str(e)}


def _memory_search_handler(query: str = "", limit: int = 5) -> Dict:
    """Search memory."""
    if not query:
        return {"success": False, "error": "missing required parameter: query"}
    try:
        import urllib.request
        payload = json.dumps({"query": query, "limit": limit}).encode()
        req = urllib.request.Request(
            MEMORY_V2_URL + "/v2/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": "memory search failed: " + str(e)}


def _memory_recall_handler(fragments: str) -> Dict:
    """Fragment reconstruction -- reconstruct complete memory using keyword fragments."""
    try:
        import urllib.request
        payload = json.dumps({"fragments": fragments}).encode()
        req = urllib.request.Request(
            MEMORY_V2_URL + "/v2/reconstruct",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        # reconsolidation (async) 
        recall_body = result.get("output", "")
        if recall_body:
            try:
                consolidate_payload = json.dumps({
                    "memory_id": result.get("memory_id", ""),
                    "reconstructed_content": recall_body,
                }).encode()
                req2 = urllib.request.Request(
                    MEMORY_V2_URL + "/v2/reconsolidate",
                    data=consolidate_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req2, timeout=5)
            except Exception:
                pass  # Reconsolidation is not a critical step
        return result
    except Exception as e:
        return {"success": False, "error": "memory recall failed: " + str(e)}


def _memory_stats_handler() -> Dict:
    """Memory system statistics."""
    try:
        import urllib.request
        req = urllib.request.Request(MEMORY_V2_URL + "/v2/stats", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": "memory stats failed: " + str(e)}


# ── 8b. SELF-EDITING MEMORY HANDLERS ───────────────────

def _remember_handler(key: str, value: str, category: str = "general") -> Dict:
    """Store a fact in entity memory (self-editing)."""
    try:
        # Access the Worldwave instance via module-level reference
        import sys
        ww_module = sys.modules.get("core.loop")
        if ww_module and hasattr(ww_module, '_active_ww_instance'):
            ww = ww_module._active_ww_instance
            if ww._memory_tools:
                return ww._memory_tools.remember(key, value, category)
        return {"status": "stored", "key": key, "note": "stored in-memory only (no entity context)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _forget_handler(key: str) -> Dict:
    """Mark a fact as outdated."""
    try:
        import sys
        ww_module = sys.modules.get("core.loop")
        if ww_module and hasattr(ww_module, '_active_ww_instance'):
            ww = ww_module._active_ww_instance
            if ww._memory_tools:
                return ww._memory_tools.forget(key)
        return {"status": "forgotten", "key": key, "note": "no entity context"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _recall_mine_handler(query: str = "", limit: int = 10) -> Dict:
    """Query stored facts about current entity."""
    try:
        import sys
        ww_module = sys.modules.get("core.loop")
        if ww_module and hasattr(ww_module, '_active_ww_instance'):
            ww = ww_module._active_ww_instance
            if ww._memory_tools:
                return ww._memory_tools.recall_mine(query, limit)
        return {"facts": {}, "total": 0, "note": "no entity context"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── 9. SCHEDULING ─────────────────────────────────────

_WW_SCHEDULER_URL = os.environ.get("WW_SCHEDULER_URL", "http://localhost:9300")

def _schedule_task_handler(cron_expr: str, goal: str,
                           max_spirals: int = 3, name: str = "") -> Dict:
    """Schedule a fixed task (via WW scheduler API)."""
    try:
        import urllib.request
        payload = json.dumps({
            "cron": cron_expr,
            "goal": goal,
            "max_spirals": max_spirals,
            "name": name or "scheduled-" + str(int(time.time())),
        }).encode()
        req = urllib.request.Request(
            _WW_SCHEDULER_URL + "/ww/schedule",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": "schedule failed: " + str(e)}


def _list_schedules_handler() -> Dict:
    """List all schedule tasks."""
    try:
        import urllib.request
        req = urllib.request.Request(_WW_SCHEDULER_URL + "/ww/schedules", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": "list schedules failed: " + str(e)}


# ── 10. PLATFORM ──────────────────────────────────────

def _send_message_handler(platform: str, channel: str, message: str) -> Dict:
    """Send message to social platform (via MQTT Gateway)."""
    try:
        import urllib.request
        payload = json.dumps({
            "platform": platform,
            "channel": channel,
            "message": message,
        }).encode()
        req = urllib.request.Request(
            _WW_SCHEDULER_URL + "/ww/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": "send message failed: " + str(e)}


# ── 11. SKILL management ────────────────────────────────────

def _skill_list_handler() -> Dict:
    """List WW's skill list."""
    skills_dir = os.path.expanduser("~/worldwave/skills")
    if not os.path.isdir(skills_dir):
        return {"success": True, "output": "no skills directory", "data": {"skills": []}}
    files = [f for f in os.listdir(skills_dir) if f.endswith(".md")]
    return {"success": True, "output": "\n".join(files) if files else "(none)", "data": {"skills": files}}


def _skill_read_handler(name: str) -> Dict:
    """reada skill content. """
    skills_dir = os.path.expanduser("~/worldwave/skills")
    p = os.path.join(skills_dir, name if name.endswith(".md") else name + ".md")
    if not os.path.isfile(p):
        return {"success": False, "error": "skill not found: " + name}
    with open(p) as f:
        content = f.read()
    return {"success": True, "output": content[:3000]}


# ── 12. CONFIG ────────────────────────────────────────

def _config_get_handler(key: str = "") -> Dict:
    """read WW configuration. """
    if not key:
        return {"success": False, "error": "missing required parameter: key"}
    config_path = os.path.expanduser("~/.ww_config.json")
    if not os.path.isfile(config_path):
        return {"success": False, "error": "no config file"}
    try:
        with open(config_path) as f:
            config = json.load(f)
        if key in config:
            return {"success": True, "output": json.dumps(config[key]), "data": {key: config[key]}}
        return {"success": False, "error": "key not found: " + key}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _config_set_handler(key: str = "", value: str = "") -> Dict:
    """setting WW configuration. """
    if not key:
        return {"success": False, "error": "missing required parameter: key"}
    config_path = os.path.expanduser("~/.ww_config.json")
    try:
        config = {}
        if os.path.isfile(config_path):
            with open(config_path) as f:
                config = json.load(f)
        # Attempt to resolve JSON value
        try:
            config[key] = json.loads(value)
        except json.JSONDecodeError:
            config[key] = value
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        return {"success": True, "output": key + " = " + json.dumps(config[key])}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _switch_model_handler(model: str) -> Dict:
    """Switch the active LLM model on the fly."""
    # Use the running WW instance
    from core.loop import _active_ww_instance
    ww = _active_ww_instance
    if not ww:
        return {"success": False, "error": "WW not initialized"}
    result = ww.switch_model(model)
    return {"success": True, "output": f"Switched from {result['from']} to {result['to']}", "data": result}


def _config_list_handler() -> Dict:
    """List all configuration."""
    config_path = os.path.expanduser("~/.ww_config.json")
    if not os.path.isfile(config_path):
        return {"success": True, "output": "no config", "data": {}}
    try:
        with open(config_path) as f:
            config = json.load(f)
        return {"success": True, "output": json.dumps(config, indent=2), "data": config}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 13. Cognitive Tool ──────────────────────────────────────

def _analyze_image_handler(image_path: str, question: str = "") -> Dict:
    """
    Analyze image — uses the configured vision LLM.
    """
    try:
        from core.multimodal_coding import get_multimodal_coder
        mc = get_multimodal_coder()
        analysis = mc.analyze_image(os.path.expanduser(image_path), question)
        return {
            "success": True,
            "description": analysis.description,
            "ui_components": analysis.ui_components,
            "layout": analysis.layout,
            "colors": analysis.colors,
            "text_content": analysis.text_content,
            "suggested_structure": analysis.suggested_structure,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _generate_image_handler(prompt: str, style: str = "") -> Dict:
    """generateimage (needs external API supports) . """
    gen_url = os.environ.get("WW_IMAGE_GEN_URL", "")
    if gen_url:
        try:
            import urllib.request
            payload = json.dumps({"prompt": prompt, "style": style}).encode()
            req = urllib.request.Request(gen_url + "/generate", data=payload,
                                          headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"success": False, "error": "image gen API failed: " + str(e)}
    return {"success": False, "error": "no image generation backend configured (set WW_IMAGE_GEN_URL)"}


# ════════════════════════════════════════════════════════════
# Register to default tool set
# ════════════════════════════════════════════════════════════

def default_registry(guardrails=None) -> ToolRegistry:
    """Create contains all built-in tool registry."""
    r = ToolRegistry()
    if guardrails is not None:
        r.set_guardrails(guardrails)
    else:
        try:
            from core.guardrails import Guardrails
            r.set_guardrails(Guardrails())
        except ImportError:
            pass

    # ── 1. SHELL & system ──
    r.register_from_def("shell", "Execute shell command (Linux/Mac). For system operations, process control, script execution.",
                        _shell_handler,
                        parameters={"command": {"type": "string", "description": "Command to execute"},
                                    "timeout": {"type": "integer", "description": "timeout seconds", "default": 30},
                                    "workdir": {"type": "string", "description": "Working directory", "default": ""}},
                        examples=['shell(command="ls -la", timeout=10)', 'shell(command="uptime")'],
                        category="system", permission=PERMISSION_DESTRUCTIVE)

    r.register_from_def("system_status", "Get system state: CPU load, memory, disk, network, top processes. No parameters needed.",
                        _system_status_handler, parameters={},
                        examples=['system_status()'], category="system")

    r.register_from_def("env_info", "System environment info: Python version, OS, hostname, architecture, environment variables.",
                        _env_info_handler, parameters={},
                        examples=['env_info()'], category="system")

    r.register_from_def("which", "Locate executable file path. Similar to 'which' command.",
                        _which_handler,
                        parameters={"command": {"type": "string", "description": "Command name to query"}},
                        examples=['which(command="python3")', 'which(command="git")'],
                        category="system")

    r.register_from_def("process_list", "List running processes, can filter by name.",
                        _process_list_handler,
                        parameters={"filter_str": {"type": "string", "description": "Filter keyword (optional)", "default": ""}},
                        examples=['process_list()', 'process_list(filter_str="python")'],
                        category="system")

    r.register_from_def("process_kill", "Terminate a process.",
                        _process_kill_handler,
                        parameters={"pid": {"type": "integer", "description": "Process PID"},
                                    "signal": {"type": "string", "description": "Signal (TERM/KILL/HUP/INT)", "default": "TERM"}},
                        examples=['process_kill(pid=1234)', 'process_kill(pid=1234, signal="KILL")'],
                        category="system", permission=PERMISSION_DESTRUCTIVE)

    # ── 2. FILE ──
    r.register_from_def("read_file", "Read file content. For viewing config files, code, logs, etc.",
                        _read_file_handler,
                        parameters={"path": {"type": "string", "description": "file path (supports ~ extension) "}},
                        examples=['read_file(path="~/worldwave/README.md")'], category="file")

    r.register_from_def("write_file", "Write file (overwrite mode). Creates/modifies file. Auto-creates parent directory.",
                        _write_file_handler,
                        parameters={"path": {"type": "string", "description": "file path"},
                                    "content": {"type": "string", "description": "towrite content"}},
                        examples=['write_file(path="/tmp/test.txt", content="hello world")'],
                        category="file", permission=PERMISSION_APPROVAL)

    r.register_from_def("list_files", "List directory content. Supports depth control and pattern filter.",
                        _list_files_handler,
                        parameters={"path": {"type": "string", "description": "directorypath"},
                                    "depth": {"type": "integer", "description": "Recursion depth", "default": 1},
                                    "pattern": {"type": "string", "description": "Filename filter (glob)", "default": ""}},
                        examples=['list_files(path="~/worldwave", depth=2)',
                                  'list_files(path="~", pattern="*.py")'],
                        category="file")

    r.register_from_def("search_files", "Search file content (similar to grep -r). For finding keywords in code.",
                        _search_files_handler,
                        parameters={"pattern": {"type": "string", "description": "Search keyword (regex)"},
                                    "path": {"type": "string", "description": "searchdirectory", "default": "."},
                                    "file_glob": {"type": "string", "description": "Filter filename", "default": ""},
                                    "max_results": {"type": "integer", "description": "Maximum results", "default": 20}},
                        examples=['search_files(pattern="class Tool", path="~/worldwave")',
                                  'search_files(pattern="import os", file_glob="*.py")'],
                        category="file")

    r.register_from_def("patch", "Find and replace text in file. More secure than sed (atomic operation).",
                        _patch_handler,
                        parameters={"path": {"type": "string", "description": "file path"},
                                    "old_string": {"type": "string", "description": "Old text to replace"},
                                    "new_string": {"type": "string", "description": "newtext"}},
                        examples=['patch(path="config.py", old_string="debug=True", new_string="debug=False")'],
                        category="file", permission=PERMISSION_APPROVAL)

    r.register_from_def("file_info", "Get file/directory details: size, type, modification time, permissions, line count.",
                        _file_info_handler,
                        parameters={"path": {"type": "string", "description": "File or directory path"}},
                        examples=['file_info(path="~/worldwave/")'], category="file")

    r.register_from_def("hash_file", "Calculate file hash (supports md5/sha1/sha256/sha512).",
                        _hash_file_handler,
                        parameters={"path": {"type": "string", "description": "file path"},
                                    "algorithm": {"type": "string", "description": "Hash algorithm", "default": "sha256"}},
                        category="file")

    r.register_from_def("copy", "Copy file or directory. Supports recursive copy.",
                        _copy_handler,
                        parameters={"src": {"type": "string", "description": "sourcepath"},
                                    "dst": {"type": "string", "description": "goalpath"},
                                    "recursive": {"type": "boolean", "description": "Recursioncopydirectory", "default": False}},
                        category="file", permission=PERMISSION_APPROVAL)

    r.register_from_def("move", "move/Renamefileordirectory. ",
                        _move_handler,
                        parameters={"src": {"type": "string", "description": "sourcepath"},
                                    "dst": {"type": "string", "description": "goalpath"}},
                        category="file", permission=PERMISSION_APPROVAL)

    r.register_from_def("delete", "deletefileordirectory. deletedirectoryneeds recursive=True. ",
                        _delete_handler,
                        parameters={"path": {"type": "string", "description": "todelete path"},
                                    "recursive": {"type": "boolean", "description": "Recursiondeletedirectory", "default": False}},
                        category="file", permission=PERMISSION_DESTRUCTIVE)

    # ── 3. GIT ──
    r.register_from_def("git_status", "Git repositorystate: when  branch、unsaved changes. ",
                        _git_status_handler,
                        parameters={"path": {"type": "string", "description": "repo path", "default": "."}},
                        examples=['git_status()', 'git_status(path="~/worldwave")'],
                        category="git")

    r.register_from_def("git_log", "Git commitHistory (oneline + graph) . ",
                        _git_log_handler,
                        parameters={"path": {"type": "string", "description": "repo path", "default": "."},
                                    "count": {"type": "integer", "description": "Displaycount", "default": 10}},
                        category="git")

    r.register_from_def("git_diff", "Git Workdirectorychange diff. ",
                        _git_diff_handler,
                        parameters={"path": {"type": "string", "description": "repo path", "default": "."},
                                    "staged": {"type": "boolean", "description": "Display staged Difference", "default": False}},
                        category="git")

    r.register_from_def("git_commit", "Git add + commit. ",
                        _git_commit_handler,
                        parameters={"path": {"type": "string", "description": "repo path", "default": "."},
                                    "message": {"type": "string", "description": "commit message", "default": "ww auto commit"}},
                        category="git", permission=PERMISSION_APPROVAL)

    r.register_from_def("git_push", "Git push to remote. ",
                        _git_push_handler,
                        parameters={"path": {"type": "string", "description": "repo path", "default": "."},
                                    "remote": {"type": "string", "description": "remotename", "default": "origin"},
                                    "branch": {"type": "string", "description": "branchname (defaultaswhen  branch) ", "default": ""}},
                        category="git", permission=PERMISSION_APPROVAL)

    r.register_from_def("git_pull", "Git pull update. ",
                        _git_pull_handler,
                        parameters={"path": {"type": "string", "description": "repo path", "default": "."}},
                        category="git")

    r.register_from_def("git_clone", "Git clone repository. ",
                        _git_clone_handler,
                        parameters={"url": {"type": "string", "description": "repository URL"},
                                    "dest": {"type": "string", "description": "goaldirectory (optional) ", "default": ""},
                                    "branch": {"type": "string", "description": "Specifybranch (optional) ", "default": ""}},
                        examples=['git_clone(url="https://github.com/user/repo.git")',
                                  'git_clone(url="https://github.com/user/repo.git", dest="~/myrepo")'],
                        category="git")

    # ── 4. NETWORK ──
    r.register_from_def("http_request", "send HTTP request. supports GET/POST/PUT/DELETE And custom headers. ",
                        _http_request_handler,
                        parameters={"url": {"type": "string", "description": "request URL"},
                                    "method": {"type": "string", "description": "HTTP Method", "default": "GET"},
                                    "headers": {"type": "object", "description": "HTTP headers (key:value) ", "default": {}},
                                    "body": {"type": "string", "description": "request body (POST/PUT) ", "default": ""},
                                    "timeout": {"type": "integer", "description": "timeout seconds", "default": 15}},
                        examples=['http_request(url="https://api.github.com")',
                                  'http_request(url="https://api.example.com/data", method="POST", body="{\\"key\\":\\"val\\"}")'],
                        category="network", permission=PERMISSION_APPROVAL)

    r.register_from_def("fetch_url", "fetch web pagecontent (plaintext, Remove HTML tag) . reads  API、Webpage information. ",
                        _fetch_url_handler,
                        parameters={"url": {"type": "string", "description": "Webpage URL"},
                                    "timeout": {"type": "integer", "description": "timeout seconds", "default": 15}},
                        examples=['fetch_url(url="https://example.com")'],
                        category="network")

    r.register_from_def("ping", "Ping hosttestConnectivity. ",
                        _ping_handler,
                        parameters={"host": {"type": "string", "description": "goalhost"},
                                    "count": {"type": "integer", "description": "Ping Count", "default": 4},
                                    "timeout": {"type": "integer", "description": "Single timetimeout seconds", "default": 10}},
                        category="network")

    r.register_from_def("dns_lookup", "DNS query (A recordOr arbitrarytype) . ",
                        _dns_lookup_handler,
                        parameters={"host": {"type": "string", "description": "goalhost"},
                                    "type": {"type": "string", "description": "recordtype", "default": "A"}},
                        category="network")

    # ── 5. UTILITY ──
    r.register_from_def("uuid", "generate UUID v4. ",
                        _uuid_handler, parameters={},
                        examples=['uuid()'], category="utility")

    r.register_from_def("timestamp", "when    timestamp (supports iso/unix/readable format) . ",
                        _timestamp_handler,
                        parameters={"format": {"type": "string", "description": "format: iso/unix/readable", "default": "iso"}},
                        examples=['timestamp()', 'timestamp(format="unix")'],
                        category="utility")

    r.register_from_def("time_calc", "  Calculation. supports: 'now + 1h', 'now - 30m', '2024-01-01 -> 2024-02-01'. ",
                        _time_calc_handler,
                        parameters={"expression": {"type": "string", "description": "  expression"}},
                        examples=['time_calc(expression="now + 1h")',
                                  'time_calc(expression="now - 7d")'],
                        category="utility")

    r.register_from_def("base64", "Base64 encode/decode. ",
                        _base64_handler,
                        parameters={"action": {"type": "string", "description": "encode or decode"},
                                    "data": {"type": "string", "description": "toprocess Data"}},
                        category="utility")

    r.register_from_def("json_tool", "JSON formatization/validate/compress. ",
                        _json_tool_handler,
                        parameters={"action": {"type": "string", "description": "format/compact/validate"},
                                    "data": {"type": "string", "description": "JSON String"}},
                        category="utility")

    # ── 6. WEB SEARCH ──
    r.register_from_def("web_search", "searchWebpage (no API key Requirement, plaintextResult) . for Query information、Find file. ",
                        _web_search_handler,
                        parameters={"query": {"type": "string", "description": "searchKeyword"}},
                        examples=['web_search(query="python asyncio tutorial")'],
                        category="search")

    # ── 7. CODE ──
    r.register_from_def("code", "execute Python Code. for Dataprocess、Calculation、Logical operation. note: Directexecute！",
                        _code_handler,
                        parameters={"code": {"type": "string", "description": "Python Code"},
                                    "timeout": {"type": "integer", "description": "timeout seconds", "default": 30}},
                        examples=['code(code="print(sum(range(100)))")',
                                  'code(code="import json; print(json.dumps({\'a\': 1}))")'],
                        category="code", permission=PERMISSION_DESTRUCTIVE)

    # ── 8. MEMORY ──
    r.register_from_def("memory_store", "savememory to biomimetic memorysystem v2. autoencodeentity、emotion. ",
                        _memory_store_handler,
                        parameters={"content": {"type": "string", "description": "Memorycontent"},
                                    "category": {"type": "string", "description": "classification", "default": "general"},
                                    "tags": {"type": "string", "description": "Comma-separated tag", "default": ""},
                                    "importance": {"type": "number", "description": "importance 0-1", "default": 0.5}},
                        category="memory")

    r.register_from_def("memory_search", "searchMemory (semantic fuzzy matching) . ",
                        _memory_search_handler,
                        parameters={"query": {"type": "string", "description": "searchKeyword"},
                                    "limit": {"type": "integer", "description": "Resultcount", "default": 5}},
                        category="memory")

    r.register_from_def("memory_recall", "fragment reconstruction--Rebuild with fragmented keywordscompleteMemory, withreconsolidation. ",
                        _memory_recall_handler,
                        parameters={"fragments": {"type": "string", "description": "memory fragments/Keyword"}},
                        examples=['memory_recall(fragments="deploy config")'],
                        category="memory")

    r.register_from_def("memory_stats", "MemorysystemstatisticsInformation. ",
                        _memory_stats_handler, parameters={},
                        category="memory")

    # ── 8b. SELF-EDITING MEMORY (Entity continuity) ──
    r.register_from_def("remember",
        "Store a fact in your persistent memory. Use this when you learn something "
        "new about the user or need to remember information across conversations. "
        "Facts stored with 'remember' persist across ALL platforms (Telegram, terminal, etc.) "
        "and survive server restarts. Example: remember(key='user_name', value='Chung')",
        _remember_handler,
        parameters={
            "key": {"type": "string", "description": "Short label for this fact"},
            "value": {"type": "string", "description": "The fact content to store"},
            "category": {"type": "string", "description": "Optional: general, preference, technical, contact, project", "default": "general"},
        },
        examples=['remember(key="user_preferred_model", value="deepseek-v4-pro")'],
        category="memory")

    r.register_from_def("forget",
        "Mark a stored fact as outdated. Use this when you detect that previously "
        "stored information is no longer correct. The old fact is superseded, not "
        "deleted — you can still recall it as historical context. "
        "Example: forget(key='old_project_name')",
        _forget_handler,
        parameters={
            "key": {"type": "string", "description": "The fact key to supersede"},
        },
        examples=['forget(key="old_api_key")'],
        category="memory")

    r.register_from_def("recall_mine",
        "Query what you currently know about the user and your working context. "
        "Use this to check stored facts before responding to ensure accuracy. "
        "Example: recall_mine() for all facts, or recall_mine(query='preference') to filter.",
        _recall_mine_handler,
        parameters={
            "query": {"type": "string", "description": "Optional filter keyword", "default": ""},
            "limit": {"type": "integer", "description": "Max results to return", "default": 10},
        },
        examples=['recall_mine()', 'recall_mine(query="model")'],
        category="memory")

    # ── 9. SCHEDULING ──
    r.register_from_def("schedule_task", "schedulea fixed task (via  WW scheduler API) . supports cron expression. ",
                        _schedule_task_handler,
                        parameters={"cron_expr": {"type": "string", "description": "cron expression"},
                                    "goal": {"type": "string", "description": "taskgoal"},
                                    "max_spirals": {"type": "integer", "description": "max spirals", "default": 3},
                                    "name": {"type": "string", "description": "taskname (optional) ", "default": ""}},
                        category="scheduler", permission=PERMISSION_APPROVAL)

    r.register_from_def("list_schedules", "Listall scheduletask. ",
                        _list_schedules_handler, parameters={},
                        category="scheduler")

    # ── 10. PLATFORM ──
    r.register_from_def("send_message", "sendmessageto Socialplatform (Telegram/Discord etc., via  MQTT Gateway) . ",
                        _send_message_handler,
                        parameters={"platform": {"type": "string", "description": "platformname (telegram/discord/etc)"},
                                    "channel": {"type": "string", "description": "channelOr chat ID"},
                                    "message": {"type": "string", "description": "messagecontent"}},
                        category="platform", permission=PERMISSION_APPROVAL)

    # ── 11. SKILL ──
    r.register_from_def("skill_list", "List WW's availableskill (skils = procedural memory) . ",
                        _skill_list_handler, parameters={},
                        category="skill")

    r.register_from_def("skill_read", "reada skill content. ",
                        _skill_read_handler,
                        parameters={"name": {"type": "string", "description": "skillname"}},
                        category="skill")

    # ── 12. CONFIG ──
    r.register_from_def("switch_model", "Switch the LLM model on the fly. Use when user asks to change models (e.g. 'switch to flash', 'use pro model'). Short names: flash → deepseek-v4-flash, pro → deepseek-v4-pro.",
                        _switch_model_handler,
                        parameters={"model": {"type": "string", "description": "Model name (e.g. flash, pro, deepseek-v4-flash, deepseek-v4-pro)"}},
                        examples=['switch_model(model="flash")', 'switch_model(model="deepseek-v4-pro")'],
                        category="config")

    r.register_from_def("config_get", "read WW configuration. ",
                        _config_get_handler,
                        parameters={"key": {"type": "string", "description": "configurationKey name"}},
                        category="config")

    r.register_from_def("config_set", "setting WW configuration. autoPersistenceto  ~/.ww_config.json. ",
                        _config_set_handler,
                        parameters={"key": {"type": "string", "description": "configurationKey name"},
                                    "value": {"type": "string", "description": "configurationvalue (autoresolve JSON) "}},
                        category="config", permission=PERMISSION_APPROVAL)

    r.register_from_def("config_list", "Listall  WW configuration. ",
                        _config_list_handler, parameters={},
                        category="config")

    # ── 13. COGNITIVE ──
    r.register_from_def("analyze_image", "Analysisimage/screenscreenshot content. needs  vision API  endpoint. ",
                        _analyze_image_handler,
                        parameters={"image_path": {"type": "string", "description": "imagepathor URL"},
                                    "question": {"type": "string", "description": "targeting image Problem (optional) ", "default": ""}},
                        category="cognitive")

    r.register_from_def("generate_image", "from textDescriptiongenerateimage. needs imagegenerate API  endpoint. ",
                        _generate_image_handler,
                        parameters={"prompt": {"type": "string", "description": "imageDescription"},
                                    "style": {"type": "string", "description": "Style (optional) ", "default": ""}},
                        category="cognitive")

    # ── filetool ──
    from tools.file_tools import register_tools as register_file_tools
    register_file_tools(r)

    # ── Terminal Tool ──
    from tools.terminal_tools import register_tools as register_terminal_tools
    register_terminal_tools(r)

    # ── networktool ──
    from tools.web_tools import register_tools as register_web_tools
    register_web_tools(r)

    # ── Code Execute Tool ──
    from tools.code_exec import register_tools as register_code_tools
    register_code_tools(r)

    # ── Semantic Codebase Search ──
    try:
        from tools.semantic_search import register_tools as register_semantic_tools
        register_semantic_tools(r)
    except Exception:
        pass  # Semantic search is optional, requires codebase_index

    # ── Multimodal Coding (Vision → Code) ──
    try:
        from tools.multimodal import register_tools as register_multimodal_tools
        register_multimodal_tools(r)
    except Exception:
        pass  # Requires vision model provider

    # ── Speculative Edit (Tab completion) ──
    try:
        from tools.speculative_edit import register_tools as register_speculative_tools
        register_speculative_tools(r)
    except Exception:
        pass

    # ── SSH tool ──
    from tools.ssh_client import register_ssh_tools
    register_ssh_tools(r)

    # ── GitHub PR Bot ──
    try:
        from tools.pr_bot import register_tools as register_pr_bot
        register_pr_bot(r)
    except Exception:
        pass  # PR Bot requires GitHub token

    # ── Plugin Marketplace ──
    try:
        from tools.plugin_marketplace import register_tools as register_plugin_tools
        register_plugin_tools(r)
    except Exception:
        pass

    # ── Enterprise (RBAC + Audit) ──
    try:
        from tools.enterprise import register_tools as register_enterprise_tools
        register_enterprise_tools(r)
    except Exception:
        pass

    # ── browsertool ──
    from tools.browser import register_browser_tools
    register_browser_tools(r)

    # ── TELEGRAM tool ──
    from tools.telegram import register_telegram_tools
    register_telegram_tools(r)

    # ── 14. SELF-HEAL (Code Self-Repair) ──
    def _self_heal_analyze(params: Dict = None) -> Dict:
        from tools.self_healer import SelfHealer
        healer = SelfHealer()
        analysis = healer.analyze()
        return {"success": True, "output": str(analysis), "data": analysis}

    def _self_heal_patch(params: Dict = None) -> Dict:
        from tools.self_healer import SelfHealer
        healer = SelfHealer()
        path = (params or {}).get("path", "")
        old = (params or {}).get("old", "")
        new_text = (params or {}).get("new", "")
        if not path or not old:
            return {"success": False, "error": "needs  path and old parameters"}
        # Convert to absolute path
        if not path.startswith("/"):
            ww_dir = os.path.expanduser("~/worldwave")
            path = os.path.join(ww_dir, path)
        result = healer.safe_patch(path, old, new_text)
        return {"success": result.get("applied", False), "output": result.get("detail", ""),
                "data": result}

    r.register_from_def("self_heal_analyze", "Analysis WW Selfcode, Find potentialat  bug And problem. ",
                        _self_heal_analyze, parameters={},
                        category="cognitive",
                        examples=["self_heal_analyze()"])
    r.register_from_def("self_heal_patch", "secureModify locally WW's Source code. autoBackup and syntaxvalidate. ",
                        _self_heal_patch, parameters={"path": {"type": "string", "description": "file path (relativeorabsolute) "},
                                                       "old": {"type": "string", "description": "To replace Originaltext"},
                                                       "new": {"type": "string", "description": "newtext"}},
                        category="cognitive", permission=PERMISSION_APPROVAL,
                        examples=['self_heal_patch(path="core/llm.py", old="old_text", new="new_text")'])

    # ── Computer Use tool (Windows Desktop Control) ──
    # Skip registration on headless hosts (no PowerShell / no display) to avoid
    # flooding the tool schema and inflating tool failure rates.
    try:
        cu_ok = False
        try:
            from core.computer_use import check_available
            cu_ok = bool(check_available())
        except Exception:
            cu_ok = False
        force_cu = os.environ.get("WW_FORCE_COMPUTER_USE", "").lower() in ("1", "true", "yes")
        if cu_ok or force_cu:
            from tools.computer_use import register_tools as register_cu_tools
            register_cu_tools(r)
        else:
            pass  # headless: omit CU tools
    except Exception:
        pass  # Computer Use is not mandatory, load failure does not affect other features

    # ── WW-PM Programming Module (60 tools: AST, LSP, Sandbox, etc.) ──
    try:
        from coding import register_tools as register_coding_tools
        n = register_coding_tools(r)
        __builtins__["_coding_tools_count"] = n
    except Exception:
        pass  # PM module is optional, load failure does not affect core functionality

    return r
