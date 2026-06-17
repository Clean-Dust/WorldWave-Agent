"""ww/tools/terminal_tools.py — Terminal command tool

Dependencies: None (pure stdlib)
Purpose: execute shell commands, manage background processes
"""

from __future__ import annotations
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

from tools.registry import ToolRegistry, ToolDef


# ── background process management ──

_processes: dict = {}
_process_lock = threading.Lock()


def register_tools(registry: ToolRegistry):
    """Register terminal tools with the given registry."""

    # ── run_command ────────────────────────────────────

    def handle_run_command(command: str, timeout: int = 60, workdir: Optional[str] = None, **kwargs) -> dict:
        """Execute a shell command and return output."""
        try:
            cwd = os.path.expanduser(workdir) if workdir else None
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            return {
                "result": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {timeout}s", "exit_code": -1}
        except Exception as e:
            return {"error": str(e), "exit_code": -1}

    registry.register(ToolDef(
        name="run_command",
        description="Execute a shell command and return stdout/stderr/exit_code.",
        handler=handle_run_command,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Max seconds to wait", "default": 60},
                "workdir": {"type": "string", "description": "Working directory (optional)"},
            },
            "required": ["command"],
        },
        examples=[
            "run_command(command='ls -la')",
            "run_command(command='python3 script.py', timeout=120, workdir='/home/project')",
        ],
        category="terminal",
    ))

    # ── run_background ─────────────────────────────────

    def handle_run_background(command: str, workdir: Optional[str] = None, **kwargs) -> dict:
        """Start a background process and return its session ID."""
        session_id = f"proc_{int(time.time())}_{len(_processes)}"
        try:
            cwd = os.path.expanduser(workdir) if workdir else None
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN),
            )
            with _process_lock:
                _processes[session_id] = {
                    "proc": proc,
                    "command": command,
                    "started": datetime.now().isoformat(),
                    "stdout_lines": [],
                    "stderr_lines": [],
                    "done": False,
                }

            # Background reader threads
            def reader(stream, store_key):
                for line in iter(stream.readline, b""):
                    with _process_lock:
                        if session_id in _processes:
                            _processes[session_id][store_key].append(
                                line.decode("utf-8", errors="replace").rstrip()
                            )
                stream.close()

            threading.Thread(target=reader, args=(proc.stdout, "stdout_lines"), daemon=True).start()
            threading.Thread(target=reader, args=(proc.stderr, "stderr_lines"), daemon=True).start()

            return {
                "result": "Background process started",
                "session_id": session_id,
                "pid": proc.pid,
            }
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="run_background",
        description="Start a command in the background. Returns a session_id for status checking.",
        handler=handle_run_background,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "workdir": {"type": "string", "description": "Working directory (optional)"},
            },
            "required": ["command"],
        },
        category="terminal",
    ))

    # ── process_status ─────────────────────────────────

    def handle_process_status(session_id: str, **kwargs) -> dict:
        """Check status of a background process."""
        with _process_lock:
            if session_id not in _processes:
                return {"error": f"No process with session_id: {session_id}"}
            info = _processes[session_id]
            proc = info["proc"]
            poll = proc.poll()
            done = poll is not None

            if done and not info["done"]:
                info["done"] = True
                info["exit_code"] = poll

            stdout = "\n".join(info["stdout_lines"][-200:])
            stderr = "\n".join(info["stderr_lines"][-100:])

            return {
                "result": {
                    "running": not done,
                    "exit_code": info.get("exit_code"),
                    "stdout": stdout,
                    "stderr": stderr,
                    "command": info["command"],
                    "started": info["started"],
                }
            }

    registry.register(ToolDef(
        name="process_status",
        description="Check the status and output of a background process.",
        handler=handle_process_status,
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID from run_background"},
            },
            "required": ["session_id"],
        },
        category="terminal",
    ))

    # ── process_kill ───────────────────────────────────

    def handle_process_kill(session_id: str, **kwargs) -> dict:
        """Kill a background process."""
        with _process_lock:
            if session_id not in _processes:
                return {"error": f"No process with session_id: {session_id}"}
            info = _processes[session_id]
            proc = info["proc"]
            try:
                proc.terminate()
                proc.wait(timeout=5)
                info["done"] = True
                info["exit_code"] = -15
                return {"result": f"Process {proc.pid} terminated"}
            except Exception as e:
                try:
                    proc.kill()
                    return {"result": f"Process {proc.pid} killed"}
                except Exception:
                    return {"error": str(e)}

    registry.register(ToolDef(
        name="process_kill",
        description="Terminate a background process by session_id.",
        handler=handle_process_kill,
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID from run_background"},
            },
            "required": ["session_id"],
        },
        category="terminal",
    ))
