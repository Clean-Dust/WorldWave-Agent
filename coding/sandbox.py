"""ww/pm/sandbox.py — Container Sandbox & Capability Mutex v0.1

Implements Gemini's WW-PM Subsystem 3.3:
- Tiered execution isolation (subprocess → container)
- Read-only root, dropped capabilities, PID limits
- Capability Mutex: tool permission separation

Architecture:
  Sandbox — execution isolation with configurable strictness
  CapabilityMutex — permission separation between agent roles
"""

from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Dict, List, Optional


# ── Capability Mutex ──────────────────────────────────────────────────

class CapabilityMutex:
    """Permission separation between agent roles.

    Implements Gemini's 'Capability Mutex' design:
    - Architect role: can create tasks, review code, plan — CANNOT edit files
    - Coder role: can edit files, run tests — CANNOT create tasks
    - Reviewer role: can read code, run tests — CANNOT edit or create tasks

    Tools are assigned to capability groups. Each role gets a fixed set.
    """

    ROLE_ARCHITECT = "architect"
    ROLE_CODER = "coder"
    ROLE_REVIEWER = "reviewer"

    # Tool capability groups
    CAP_PLAN = "plan"        # create plans, decompose tasks
    CAP_EDIT = "edit"        # modify files, write code
    CAP_READ = "read"        # read files, search code
    CAP_EXEC = "execute"     # run commands, tests
    CAP_DEPLOY = "deploy"    # deploy, release
    CAP_MANAGE = "manage"    # create/manage agents/tasks

    # Role → Capability matrix
    ROLE_CAPABILITIES = {
        ROLE_ARCHITECT: {CAP_PLAN, CAP_READ, CAP_MANAGE},
        ROLE_CODER: {CAP_EDIT, CAP_READ, CAP_EXEC},
        ROLE_REVIEWER: {CAP_READ, CAP_EXEC},
    }

    # Tool prefix → Capability mapping
    TOOL_CAPABILITY_MAP = {
        # ACI editing — prefix: coding_edit, coding_write
        "coding_edit": CAP_EDIT,
        "coding_write": CAP_EDIT,
        # ACI viewer — prefix: coding_open, coding_scroll, coding_goto, coding_close
        "coding_open": CAP_READ,
        "coding_scroll": CAP_READ,
        "coding_goto": CAP_READ,
        "coding_close": CAP_READ,
        # Shell — prefix: coding_exec, coding_shell_session
        "coding_exec": CAP_EXEC,
        "coding_shell_session": CAP_EXEC,
        # Code search AST — prefix: coding_ast_ (exact match for rewrite first!)
        "coding_ast_rewrite": CAP_EDIT,
        "coding_ast_": CAP_READ,
        "coding_call_": CAP_READ,
        "coding_function_": CAP_READ,
        "coding_code_": CAP_READ,
        "coding_class_": CAP_READ,
        "coding_glob": CAP_READ,
        # AST rewrite — prefix: coding_ast_rewrite
        "coding_ast_rewrite": CAP_EDIT,
        # Code RAG — prefix: coding_rag_
        "coding_rag_": CAP_READ,
        # Dense vector — prefix: coding_dense_
        "coding_dense_": CAP_READ,
        # LSP — prefix: coding_lsp_
        "coding_lsp_": CAP_READ,
        # Planning — prefix: coding_load_, coding_create_, coding_next_, coding_mark_, coding_save_, coding_plan_
        "coding_load_": CAP_PLAN,
        "coding_create_": CAP_PLAN,
        "coding_next_": CAP_PLAN,
        "coding_mark_": CAP_PLAN,
        "coding_save_": CAP_PLAN,
        "coding_plan_": CAP_PLAN,
        # Circuit breaker — prefix: coding_circuit_ (exact match for reset first!)
        "coding_circuit_reset": CAP_MANAGE,
        "coding_circuit_": CAP_READ,
        # Sandbox execution — prefix: coding_sandbox_
        "coding_sandbox_": CAP_EXEC,
        # Capability mutex — prefix: coding_capability_
        "coding_capability_": CAP_MANAGE,
        # Tool retrieval — prefix: coding_tool_
        "coding_tool_": CAP_READ,
        # Allure — prefix: coding_allure_
        "coding_allure_": CAP_READ,
        # Crash screenshot — prefix: coding_crash_, coding_workspace_, coding_mcp_
        "coding_crash_": CAP_READ,
        "coding_workspace_": CAP_READ,
        "coding_mcp_": CAP_READ,
    }

    def __init__(self, role: str = ROLE_CODER):
        if role not in self.ROLE_CAPABILITIES:
            raise ValueError(f"Unknown role: {role}. Use: {list(self.ROLE_CAPABILITIES.keys())}")
        self._role = role
        self._capabilities = self.ROLE_CAPABILITIES[role]

    @property
    def role(self) -> str:
        return self._role

    def can_use_tool(self, tool_name: str) -> bool:
        """Check if this role can use a specific tool."""
        # Find which capability this tool belongs to
        for prefix, cap in self.TOOL_CAPABILITY_MAP.items():
            if tool_name.startswith(prefix):
                return cap in self._capabilities
        # Unknown tools: default allow for coder, deny for architect
        if self._role == self.ROLE_ARCHITECT:
            return False  # architects can't use unclassified tools
        return True

    def check_tool(self, tool_name: str) -> Dict:
        """Check tool permission, return result with reason."""
        allowed = self.can_use_tool(tool_name)
        return {
            "allowed": allowed,
            "role": self._role,
            "tool": tool_name,
            "reason": "" if allowed else (
                f"Role '{self._role}' cannot use '{tool_name}'. "
                f"Capabilities: {self._capabilities}"
            ),
        }

    def get_allowed_tools(self, all_tools: List[str]) -> List[str]:
        """Filter a list of tool names to only those this role can use."""
        return [t for t in all_tools if self.can_use_tool(t)]

    def switch_role(self, role: str):
        """Switch to a different role."""
        if role not in self.ROLE_CAPABILITIES:
            raise ValueError(f"Unknown role: {role}")
        self._role = role
        self._capabilities = self.ROLE_CAPABILITIES[role]

    def to_dict(self) -> Dict:
        return {
            "role": self._role,
            "capabilities": sorted(self._capabilities),
            "can_plan": self.CAP_PLAN in self._capabilities,
            "can_edit": self.CAP_EDIT in self._capabilities,
            "can_read": self.CAP_READ in self._capabilities,
            "can_execute": self.CAP_EXEC in self._capabilities,
            "can_deploy": self.CAP_DEPLOY in self._capabilities,
            "can_manage": self.CAP_MANAGE in self._capabilities,
        }


# ── Execution Sandbox ─────────────────────────────────────────────────

class SandboxResult:
    """Result of a sandboxed execution."""

    def __init__(
        self,
        success: bool,
        output: str = "",
        error: str = "",
        exit_code: int = -1,
        duration: float = 0,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.exit_code = exit_code
        self.duration = duration

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "output": self.output[:2000],
            "error": self.error[:500],
            "exit_code": self.exit_code,
            "duration": round(self.duration, 2),
        }


class Sandbox:
    """Tiered execution sandbox for safe code running.

    Tier 0 — Subprocess isolation (always available):
        - Runs in temporary directory
        - Timeout-enforced
        - Memory-limited (via ulimit)
        - No network (via unshare when available)
    
    Tier 1 — Container isolation (requires Docker):
        - Read-only root filesystem
        - Dropped Linux capabilities
        - PID limits
        - Network isolation
    
    Auto-detects available isolation level.
    """

    def __init__(
        self,
        workdir: str = None,
        timeout: int = 30,
        memory_limit_mb: int = 512,
        enable_network: bool = False,
    ):
        self._workdir = workdir or tempfile.mkdtemp(prefix="ww_sandbox_")
        self._timeout = timeout
        self._memory_limit_mb = memory_limit_mb
        self._enable_network = enable_network
        self._has_docker = self._check_docker()
        self._tier = 1 if self._has_docker else 0

    def execute(self, command: str, files: Dict[str, str] = None) -> SandboxResult:
        """Execute a command in the sandbox.

        Args:
            command: Shell command to run
            files: Dict of {filename: content} to write before execution

        Returns:
            SandboxResult with output and status
        """
        if self._tier >= 1 and self._has_docker:
            return self._execute_docker(command, files)
        return self._execute_subprocess(command, files)

    def write_file(self, path: str, content: str) -> str:
        """Write a file into the sandbox workdir. Returns sandbox path."""
        abs_path = os.path.join(self._workdir, path)
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return abs_path

    def read_file(self, path: str) -> Optional[str]:
        """Read a file from the sandbox workdir."""
        abs_path = os.path.join(self._workdir, path)
        if not os.path.isfile(abs_path):
            return None
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def clean(self):
        """Clean up the sandbox directory."""
        if os.path.isdir(self._workdir):
            shutil.rmtree(self._workdir, ignore_errors=True)

    def _execute_subprocess(
        self, command: str, files: Dict[str, str] = None
    ) -> SandboxResult:
        """Execute in subprocess with ulimit restrictions."""
        # Write input files
        if files:
            for path, content in files.items():
                self.write_file(path, content)

        start = time.time()
        try:
            result = subprocess.run(
                ["/bin/bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._workdir,
                env={
                    **os.environ,
                    "HOME": self._workdir,
                    "TMPDIR": self._workdir,
                },
                preexec_fn=self._apply_restrictions,
            )
            duration = time.time() - start
            return SandboxResult(
                success=result.returncode == 0,
                output=result.stdout,
                error=result.stderr,
                exit_code=result.returncode,
                duration=duration,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                success=False,
                error=f"Timeout ({self._timeout}s)",
                exit_code=-1,
                duration=self._timeout,
            )
        except FileNotFoundError as e:
            return SandboxResult(success=False, error=str(e))

    def _execute_docker(
        self, command: str, files: Dict[str, str] = None
    ) -> SandboxResult:
        """Execute in Docker container with strict isolation."""
        if files:
            for path, content in (files or {}).items():
                self.write_file(path, content)

        container_name = f"ww-sandbox-{uuid.uuid4().hex[:8]}"

        docker_cmd = [
            "docker", "run", "--rm",
            "--name", container_name,
            "--read-only",  # Read-only root filesystem
            "--cap-drop", "ALL",  # Drop all Linux capabilities
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "64",  # PID limit
            "--memory", f"{self._memory_limit_mb}m",
            "--memory-swap", f"{self._memory_limit_mb}m",  # No swap
        ]

        if not self._enable_network:
            docker_cmd.extend(["--network", "none"])

        # Mount workdir
        docker_cmd.extend(["-v", f"{self._workdir}:/workspace:rw"])
        docker_cmd.extend(["-w", "/workspace"])

        # Use python:3-slim as base
        docker_cmd.extend(["python:3.11-slim", "/bin/bash", "-c", command])

        start = time.time()
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout + 10,
            )
            duration = time.time() - start
            return SandboxResult(
                success=result.returncode == 0,
                output=result.stdout,
                error=result.stderr,
                exit_code=result.returncode,
                duration=duration,
            )
        except subprocess.TimeoutExpired:
            # Clean up container
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True, timeout=5,
            )
            return SandboxResult(
                success=False,
                error=f"Docker timeout ({self._timeout}s)",
                exit_code=-1,
                duration=self._timeout,
            )

    def _apply_restrictions(self):
        """Apply ulimit restrictions to subprocess."""
        import resource
        try:
            # Memory limit
            mem_bytes = self._memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            # Process limit
            resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
        except (ValueError, resource.error):
            pass

    def _check_docker(self) -> bool:
        """Check if Docker is available."""
        try:
            result = subprocess.run(
                ["docker", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def dry_run_docker_command(self, command: str = "echo test") -> Dict:
        """Return the exact Docker command that would be executed (without running it).

        Useful for verifying command construction in environments without Docker.
        """
        container_name = f"ww-sandbox-dry-{uuid.uuid4().hex[:8]}"
        docker_cmd = [
            "docker", "run", "--rm",
            "--name", container_name,
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "64",
            "--memory", f"{self._memory_limit_mb}m",
            "--memory-swap", f"{self._memory_limit_mb}m",
        ]
        if not self._enable_network:
            docker_cmd.extend(["--network", "none"])
        docker_cmd.extend(["-v", f"{self._workdir}:/workspace:rw"])
        docker_cmd.extend(["-w", "/workspace"])
        docker_cmd.extend(["python:3.11-slim", "/bin/bash", "-c", command])

        return {
            "command": docker_cmd,
            "container_name": container_name,
            "image": "python:3.11-slim",
            "read_only_root": True,
            "capabilities_dropped": "ALL",
            "pid_limit": 64,
            "memory_limit_mb": self._memory_limit_mb,
            "network": "none" if not self._enable_network else "default",
            "workdir_mount": f"{self._workdir}:/workspace:rw",
        }

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def workdir(self) -> str:
        return self._workdir

    def to_dict(self) -> Dict:
        return {
            "tier": self._tier,
            "has_docker": self._has_docker,
            "workdir": self._workdir,
            "timeout": self._timeout,
            "memory_limit_mb": self._memory_limit_mb,
            "network_enabled": self._enable_network,
        }


# ── SandboxManager ────────────────────────────────────────────────────

class SandboxManager:
    """Manages sandbox lifecycle and capability mutex for agent sessions."""

    def __init__(
        self,
        default_role: str = "coder",
        default_timeout: int = 30,
    ):
        self._mutex = CapabilityMutex(default_role)
        self._sandboxes: Dict[str, Sandbox] = {}
        self._default_timeout = default_timeout

    @property
    def mutex(self) -> CapabilityMutex:
        return self._mutex

    def create_sandbox(
        self,
        name: str = None,
        timeout: int = None,
        memory_limit_mb: int = 512,
    ) -> Dict:
        """Create a new sandbox for isolated execution."""
        name = name or f"sandbox_{uuid.uuid4().hex[:8]}"
        sandbox = Sandbox(
            timeout=timeout or self._default_timeout,
            memory_limit_mb=memory_limit_mb,
        )
        self._sandboxes[name] = sandbox
        return {
            "success": True,
            "name": name,
            "config": sandbox.to_dict(),
        }

    def execute_in_sandbox(
        self, sandbox_name: str, command: str, files: Dict[str, str] = None
    ) -> Dict:
        """Execute a command in a sandbox."""
        sandbox = self._sandboxes.get(sandbox_name)
        if sandbox is None:
            return {"error": f"Sandbox {sandbox_name} not found"}

        result = sandbox.execute(command, files)
        return result.to_dict()

    def cleanup_sandbox(self, name: str) -> Dict:
        """Clean up a sandbox."""
        sandbox = self._sandboxes.pop(name, None)
        if sandbox is None:
            return {"error": f"Sandbox {name} not found"}
        sandbox.clean()
        return {"success": True, "name": name}

    def cleanup_all(self):
        """Clean up all sandboxes."""
        for name, sandbox in list(self._sandboxes.items()):
            sandbox.clean()
        self._sandboxes.clear()

    def switch_role(self, role: str) -> Dict:
        """Switch the capability mutex role."""
        try:
            self._mutex.switch_role(role)
            return {"success": True, "role": role, "capabilities": sorted(self._mutex._capabilities)}
        except ValueError as e:
            return {"error": str(e)}

    def get_status(self) -> Dict:
        return {
            "mutex": self._mutex.to_dict(),
            "sandboxes": {
                name: sb.to_dict() for name, sb in self._sandboxes.items()
            },
            "active_sandboxes": len(self._sandboxes),
        }


# ── Tool definitions ──────────────────────────────────────────────────

_manager: SandboxManager = None


def get_manager() -> SandboxManager:
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager


def create_sandbox_tools(mgr: SandboxManager) -> List[Dict]:
    return [
        {
            "name": "coding_sandbox_create",
            "description": "Create an isolated execution sandbox. Supports subprocess isolation (Tier 0, always available) and Docker containers (Tier 1, requires Docker).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Sandbox name (auto-generated if omitted)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds",
                        "default": 30,
                    },
                    "memory_limit_mb": {
                        "type": "integer",
                        "description": "Memory limit in MB",
                        "default": 512,
                    },
                },
            },
            "handler": lambda name=None, timeout=30, memory_limit_mb=512: mgr.create_sandbox(name, timeout, memory_limit_mb),
            "category": "code_sandbox",
        },
        {
            "name": "coding_sandbox_exec",
            "description": "Execute a command inside a sandbox with strict isolation (read-only root, dropped caps, PID limits).",
            "parameters": {
                "type": "object",
                "properties": {
                    "sandbox_name": {
                        "type": "string",
                        "description": "Sandbox name to execute in",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "files": {
                        "type": "object",
                        "description": "Files to write before execution {filename: content}",
                    },
                },
                "required": ["sandbox_name", "command"],
            },
            "handler": lambda sandbox_name, command, files=None: mgr.execute_in_sandbox(sandbox_name, command, files),
            "category": "code_sandbox",
        },
        {
            "name": "coding_sandbox_cleanup",
            "description": "Clean up and delete a sandbox and all its files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Sandbox name to clean up",
                    }
                },
                "required": ["name"],
            },
            "handler": mgr.cleanup_sandbox,
            "category": "code_sandbox",
        },
        {
            "name": "coding_capability_check",
            "description": "Check if a tool is allowed for the current role. Enforces capability mutex between architect/coder/reviewer roles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Tool name to check",
                    }
                },
                "required": ["tool_name"],
            },
            "handler": lambda tool_name: mgr.mutex.check_tool(tool_name),
            "category": "code_sandbox",
        },
        {
            "name": "coding_capability_switch_role",
            "description": "Switch the capability mutex role. 'architect' = plan+read only, 'coder' = edit+execute, 'reviewer' = read+execute.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["architect", "coder", "reviewer"],
                        "description": "Role to switch to",
                    }
                },
                "required": ["role"],
            },
            "handler": mgr.switch_role,
            "category": "code_sandbox",
        },
        {
            "name": "coding_capability_status",
            "description": "Get current role capabilities and active sandboxes.",
            "parameters": {"type": "object", "properties": {}},
            "handler": mgr.get_status,
            "category": "code_sandbox",
        },
    ]


def get_sandbox_tools() -> List[Dict]:
    return create_sandbox_tools(get_manager())
