"""ww/pm/shell.py — Sentinel-driven persistent execution shell v0.1

Implements Gemini's WW-PM Subsystem 3.3:
- Persistent virtual terminal with sentinel-marked command completion
- SWE-ReX inspired: unique completion markers instead of fragile timeouts
- Supports interactive tools, background services, concurrent sessions
"""

from __future__ import annotations
import os
import select
import signal
import subprocess
import threading
import time
import uuid
from typing import Dict, List, Optional


# ── Sentinel Shell ────────────────────────────────────────────────────

SHELL_SENTINEL_PREFIX = "WW-PM-EXEC-COMPLETE-"


class SentinelShell:
    """Persistent shell session with sentinel-based completion detection.

    Each command is suffixed with a unique sentinel marker.
    Output is captured until the sentinel appears, eliminating
    fragile timeout-based completion detection.

    Supports:
    - Multiple concurrent sessions (via session_id)
    - Background services (server, database)
    - Interactive tools (gdb, ipython)
    - Concurrent terminal sessions
    """

    def __init__(self, shell: str = "/bin/bash"):
        self._shell = shell
        self._sessions: Dict[str, subprocess.Popen] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._timeout_default = 60  # seconds

    def create_session(self, session_id: str = None, workdir: str = None) -> Dict:
        """Create a new persistent shell session."""
        session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"

        if session_id in self._sessions:
            return {"error": f"Session {session_id} already exists", "session_id": session_id}

        try:
            proc = subprocess.Popen(
                [self._shell],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=workdir or os.getcwd(),
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, PermissionError) as e:
            return {"error": f"Failed to start shell: {e}"}

        self._sessions[session_id] = proc
        self._locks[session_id] = threading.Lock()

        return {
            "success": True,
            "session_id": session_id,
            "pid": proc.pid,
            "shell": self._shell,
            "workdir": workdir or os.getcwd(),
        }

    def exec(
        self,
        command: str,
        session_id: str = None,
        timeout: int = None,
        workdir: str = None,
    ) -> Dict:
        """Execute command in a session. Auto-creates session if needed.

        Args:
            command: Shell command to execute
            session_id: Session identifier. Auto-created if None
            timeout: Max seconds to wait for sentinel (default: 60)
            workdir: Working directory for auto-created sessions

        Returns:
            Dict with output, exit code, session_id
        """
        timeout = timeout or self._timeout_default
        if session_id is None or session_id not in self._sessions:
            result = self.create_session(session_id, workdir)
            if not result.get("success"):
                return result
            session_id = result["session_id"]

        proc = self._sessions.get(session_id)
        if proc is None:
            return {"error": f"Session {session_id} not found"}

        sentinel = f"{SHELL_SENTINEL_PREFIX}{uuid.uuid4().hex}"
        # Append sentinel to capture command boundary
        full_cmd = f"{command}\necho '{sentinel}' $?\n"

        lock = self._locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            self._locks[session_id] = lock

        with lock:
            try:
                proc.stdin.write(full_cmd)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                return {"error": f"Shell write failed (session dead?): {e}"}

            # Wait for sentinel in output
            output_lines = []
            start = time.time()
            exit_code = None

            while time.time() - start < timeout:
                line = self._read_line(proc, timeout=1)
                if line is None:
                    # Timeout reading line
                    continue

                # Check for sentinel
                if sentinel in line:
                    # Extract exit code
                    parts = line.strip().split()
                    for p in parts:
                        if p != sentinel and p.isdigit():
                            exit_code = int(p)
                            break
                    break

                output_lines.append(line)

            if exit_code is None:
                # Timed out waiting for sentinel
                remaining = self._read_all_available(proc)
                if remaining:
                    output_lines.extend(remaining)
                return {
                    "error": "Command timed out waiting for completion marker",
                    "partial_output": "".join(output_lines[-200:]),
                    "session_id": session_id,
                    "timed_out": True,
                }

        output = "".join(output_lines)
        return {
            "success": True,
            "output": output,
            "exit_code": exit_code,
            "session_id": session_id,
            "duration": round(time.time() - start, 2),
        }

    def exec_inline(self, command: str, timeout: int = None) -> Dict:
        """Execute a command in a fresh one-shot session.

        Convenience method for simple commands.
        """
        result = self.create_session()
        if not result.get("success"):
            return result
        sid = result["session_id"]
        try:
            return self.exec(command, session_id=sid, timeout=timeout)
        finally:
            self.close_session(sid)

    def close_session(self, session_id: str) -> Dict:
        """Close a shell session."""
        proc = self._sessions.pop(session_id, None)
        lock = self._locks.pop(session_id, None)

        if proc is None:
            return {"error": f"Session {session_id} not found"}

        try:
            proc.stdin.close()
        except OSError:
            pass

        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                pass

        return {"success": True, "session_id": session_id, "pid": proc.pid}

    def close_all(self) -> Dict:
        """Close all shell sessions."""
        closed = []
        for sid in list(self._sessions.keys()):
            result = self.close_session(sid)
            if result.get("success"):
                closed.append(sid)
        return {"closed_sessions": closed, "count": len(closed)}

    def list_sessions(self) -> Dict:
        """List all active sessions."""
        sessions = {}
        for sid, proc in list(self._sessions.items()):
            alive = proc.poll() is None
            sessions[sid] = {
                "pid": proc.pid,
                "alive": alive,
            }
            if not alive:
                # Clean up dead sessions
                del self._sessions[sid]
                self._locks.pop(sid, None)

        return {"sessions": sessions, "count": len(sessions)}

    def inject_input(self, session_id: str, text: str) -> Dict:
        """Inject input into a running session (for interactive tools)."""
        proc = self._sessions.get(session_id)
        if proc is None:
            return {"error": f"Session {session_id} not found"}

        try:
            proc.stdin.write(text)
            proc.stdin.flush()
            return {"success": True, "injected": text}
        except (BrokenPipeError, OSError) as e:
            return {"error": f"Write failed: {e}"}

    def _read_line(self, proc: subprocess.Popen, timeout: float = 1) -> Optional[str]:
        """Read a single line blocking with timeout via signal.alarm."""

        class _Timeout(Exception):
            pass

        def _handler(signum, frame):
            raise _Timeout()

        if proc.stdout is None:
            return None

        old_handler = signal.signal(signal.SIGALRM, _handler)
        old_alarm = signal.alarm(max(1, int(timeout)))

        try:
            line = proc.stdout.readline()
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            return line if line else None
        except _Timeout:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            return None
        except (ValueError, OSError):
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            return None

    def _read_all_available(self, proc: subprocess.Popen) -> List[str]:
        """Read all currently available output without blocking."""
        lines = []
        try:
            while True:
                r, _, _ = select.select([proc.stdout], [], [], 0)
                if not r:
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                lines.append(line)
        except (ValueError, OSError):
            pass
        return lines


# ── Tool definitions ──────────────────────────────────────────────────

_shell: SentinelShell = None


def get_shell() -> SentinelShell:
    global _shell
    if _shell is None:
        _shell = SentinelShell()
    return _shell


def _safe_exec(shell: SentinelShell, command, session_id=None, timeout=None, workdir=None):
    """Policy-gated shell exec with semantic denial reasons."""
    try:
        from coding.policy import check_command_allowed
        gate = check_command_allowed(command)
        if not gate.get("allowed", True):
            return {
                "success": False,
                "denied": True,
                "error": gate.get("reason", "Command denied by coding policy"),
                "reason": gate.get("reason", "Command denied by coding policy"),
                "command_preview": (command or "")[:200],
            }
    except Exception:
        pass
    return shell.exec(command, session_id, timeout, workdir)


def create_shell_tools(shell: SentinelShell) -> List[Dict]:
    """Create tool definitions for shell execution."""
    return [
        {
            "name": "coding_exec",
            "description": "Execute a command in a persistent shell session. Supports long-running processes, background services, and interactive tools. Uses sentinel markers for reliable completion detection. Dangerous patterns are deny-first blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (auto-created if omitted)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default: 60)",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory",
                    },
                },
                "required": ["command"],
            },
            "handler": lambda command, session_id=None, timeout=None, workdir=None: _safe_exec(
                shell, command, session_id, timeout, workdir
            ),
            "category": "code_aci",
            "permission": "requires_approval",
        },
        {
            "name": "coding_shell_session_create",
            "description": "Create a new persistent shell session for interactive or long-running work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Custom session ID (auto-generated if omitted)",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory for the session",
                    },
                },
            },
            "handler": lambda session_id=None, workdir=None: shell.create_session(session_id, workdir),
            "category": "code_aci",
        },
        {
            "name": "coding_shell_sessions_list",
            "description": "List all active shell sessions.",
            "parameters": {"type": "object", "properties": {}},
            "handler": shell.list_sessions,
            "category": "code_aci",
        },
        {
            "name": "coding_shell_session_close",
            "description": "Close a persistent shell session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to close",
                    }
                },
                "required": ["session_id"],
            },
            "handler": shell.close_session,
            "category": "code_aci",
        },
    ]


def get_shell_tools() -> List[Dict]:
    return create_shell_tools(get_shell())
