"""ww/tools/code_exec.py — Sandbox-first code execution

Default: Docker sandbox with network=none, read-only root, no capabilities.
Falls back to subprocess if Docker unavailable.
Trusted users can use unsafe_exec to bypass sandbox.

Security: static AST analysis blocks eval/exec/import os/subprocess.
"""

from __future__ import annotations
import ast
import os
import subprocess
import sys
import textwrap
from typing import Optional

from tools.registry import ToolRegistry, ToolDef


# ── Safety check ────────────────────────────────────────────────

BLOCKED_KEYWORDS = [
    "import os", "import sys", "import subprocess",
    "import shutil", "import ctypes", "import socket",
    "__import__", "eval(", "exec(",
    "compile(", "__builtins__",
]


def _basic_safety_check(code: str) -> Optional[str]:
    """Static analysis to block dangerous operations."""
    code_lower = code.lower()
    for kw in BLOCKED_KEYWORDS:
        if kw in code_lower:
            return f"Blocked: '{kw}' not allowed in code execution"

    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("eval", "exec", "compile", "__import__"):
                        return f"Blocked: '{node.func.id}()' not allowed"
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("system", "popen", "fork", "kill"):
                        return f"Blocked: '{node.func.attr}()' not allowed"
    except SyntaxError as e:
        return f"Syntax error: {e}"

    return None


# ── Sandbox executor (lazy init) ─────────────────────────────────

_sandbox = None


def _get_sandbox():
    """Get or create the sandbox executor (Docker if available, else subprocess)."""
    global _sandbox
    if _sandbox is None:
        try:
            from sandbox.docker import DockerSandbox
            _sandbox = DockerSandbox(timeout=30)
        except Exception:
            _sandbox = None
    return _sandbox


def _run_sandboxed(code: str, timeout: int = 30) -> dict:
    """Execute code in sandbox. Returns {success, output, error, sandbox_type, duration}."""
    sb = _get_sandbox()
    if sb is None:
        return _run_subprocess(code, timeout)

    # Update timeout
    sb._timeout = timeout
    result = sb.run(code)
    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "sandbox_type": result.sandbox_type,
        "duration": round(result.duration, 2),
    }


def _run_subprocess(code: str, timeout: int = 30) -> dict:
    """Fallback subprocess execution."""
    import time
    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            },
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "sandbox_type": "subprocess",
            "duration": round(time.time() - start, 2),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout after {timeout}s", "sandbox_type": "subprocess"}
    except Exception as e:
        return {"success": False, "error": str(e), "sandbox_type": "subprocess"}


# ── Tool registration ────────────────────────────────────────────

def register_tools(registry: ToolRegistry):
    """Register sandbox-first code execution tools."""

    def handle_execute_python(code: str, timeout: int = 30, sandbox: bool = True, **kwargs) -> dict:
        """Execute Python code. Default: Docker sandbox. Set sandbox=false for subprocess."""
        safety_error = _basic_safety_check(code)
        if safety_error:
            return {"error": safety_error}

        if sandbox:
            return _run_sandboxed(code, timeout)
        else:
            return _run_subprocess(code, timeout)

    def handle_sandbox_exec(code: str, language: str = "python", timeout: int = 30, **kwargs) -> dict:
        """Execute code in a secure Docker sandbox. Default language: python."""
        safety_error = _basic_safety_check(code)
        if safety_error:
            return {"error": safety_error}

        if language != "python":
            return {"error": f"Only python supported in sandbox currently. Got: {language}"}

        return _run_sandboxed(code, timeout)

    def handle_unsafe_exec(code: str, timeout: int = 30, **kwargs) -> dict:
        """Execute code without sandbox (trusted users only). Bypasses Docker isolation."""
        safety_error = _basic_safety_check(code)
        if safety_error:
            return {"error": safety_error}
        return _run_subprocess(code, timeout)

    def handle_sandbox_status(**kwargs) -> dict:
        """Check sandbox availability and configuration."""
        sb = _get_sandbox()
        docker_available = sb is not None
        return {
            "docker_available": docker_available,
            "sandbox_default": True,
            "sandbox_type": "docker" if docker_available else "subprocess",
            "image": getattr(sb, "_image", "N/A") if sb else "N/A",
            "memory_limit": getattr(sb, "_memory", "N/A") if sb else "N/A",
        }

    registry.register(ToolDef(
        name="execute_python",
        description="Execute Python code in a sandbox (Docker isolation by default). Returns output with sandbox info.",
        handler=handle_execute_python,
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute."},
                "timeout": {"type": "integer", "description": "Max seconds.", "default": 30},
                "sandbox": {"type": "boolean", "description": "Use Docker sandbox (default: true).", "default": True},
            },
            "required": ["code"],
        },
        examples=[
            "execute_python(code='print(sum(range(100)))')",
            "execute_python(code='from datetime import datetime\\nprint(datetime.now())')",
        ],
        category="code",
    ))

    registry.register(ToolDef(
        name="sandbox_exec",
        description="Execute code in a secure Docker container (network=none, read-only root, no capabilities).",
        handler=handle_sandbox_exec,
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code to execute."},
                "language": {"type": "string", "description": "Language (default: python).", "default": "python"},
                "timeout": {"type": "integer", "description": "Max seconds.", "default": 30},
            },
            "required": ["code"],
        },
        category="code",
    ))

    registry.register(ToolDef(
        name="unsafe_exec",
        description="Execute code directly on host (no sandbox). Requires trusted user. Use sandbox_exec for untrusted code.",
        handler=handle_unsafe_exec,
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute."},
                "timeout": {"type": "integer", "description": "Max seconds.", "default": 30},
            },
            "required": ["code"],
        },
        category="code",
        permission="approval",  # Requires explicit approval
    ))

    registry.register(ToolDef(
        name="sandbox_status",
        description="Check sandbox availability and configuration.",
        handler=handle_sandbox_status,
        parameters={"type": "object", "properties": {}, "required": []},
        category="code",
    ))
