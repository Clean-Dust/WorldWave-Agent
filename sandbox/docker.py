"""Docker-based sandbox for isolated code execution.

Upgrades the existing subprocess sandbox (sandbox/runner.py) to run
inside Docker containers with strict isolation:

- Network: none (no internet access)
- Filesystem: read-only root, writable /tmp only
- Capabilities: drop all
- Memory: capped via cgroups (default 256MB)
- CPU: limited to 1 core
- Non-root user inside container (uid 1000)

Falls back to CodeSandbox (subprocess) if Docker is not available.

Blueprint ref:
  "All potentially destructive tools (host exec, filesystem overwrite,
   network scrapers) are isolated in restricted non-main threads or
   independent lightweight containers (e.g. WebContainers or Docker)."
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Read defaults from config if available
try:
    from core.config import default_config
    _cfg = default_config()
    DEFAULT_MEMORY = _cfg.get("sandbox_memory", "256m")
    DEFAULT_CPU = _cfg.get("sandbox_cpu", "1.0")
    DEFAULT_TIMEOUT = int(_cfg.get("sandbox_timeout", 30))
    DEFAULT_NETWORK = _cfg.get("sandbox_network", "none")
except Exception:
    DEFAULT_MEMORY = "256m"
    DEFAULT_CPU = "1.0"
    DEFAULT_TIMEOUT = 30
    DEFAULT_NETWORK = "none"

log = logging.getLogger("ww.sandbox.docker")


# ── Docker image ─────────────────────────────────────────────────

DOCKER_IMAGE = "python:3.12-slim"
SANDBOX_USER = "sandbox"
SANDBOX_UID = 1000


def _docker_available() -> bool:
    """Check if Docker is installed and accessible."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════
# Docker Sandbox
# ════════════════════════════════════════════════════════════════

class DockerSandbox:
    """Execute code inside an ephemeral Docker container.

    Usage:
        sandbox = DockerSandbox()
        result = sandbox.run('print("hello")')
        print(result.output)

    Falls back to CodeSandbox if Docker is unavailable.
    """

    def __init__(
        self,
        image: str = DOCKER_IMAGE,
        memory: str = DEFAULT_MEMORY,
        cpu: str = DEFAULT_CPU,
        timeout: int = DEFAULT_TIMEOUT,
        network: Optional[bool] = None,
        writable_paths: Optional[List[str]] = None,
    ):
        self._image = image
        self._memory = memory
        self._cpu = cpu
        self._timeout = timeout
        self._network = network if network is not None else (DEFAULT_NETWORK != "none")
        self._writable_paths = writable_paths or ["/tmp"]
        self._docker_ok = None  # Lazy check

        # Fallback subprocess sandbox
        from sandbox.runner import CodeSandbox
        self._fallback = CodeSandbox(timeout=timeout)

    # ── Public API ──────────────────────────────────────────────

    def run(self, code: str, context: Optional[Dict] = None) -> "SandboxResult":
        """Execute code in a Docker container (or fallback).

        Returns SandboxResult with output, errors, and timing.
        """
        if not self._check_docker():
            log.debug("Docker not available, falling back to subprocess sandbox")
            subprocess_result = self._fallback.run_code(code, context)
            return SandboxResult(
                success=subprocess_result.success,
                output=subprocess_result.output,
                error=subprocess_result.error,
                duration=subprocess_result.duration,
                sandbox_type="subprocess",
                result_data=subprocess_result.result_data,
            )

        start = time.time()
        script_path = None
        container_id = None

        try:
            # Write code to temp file
            script_path = self._write_script(code, context or {})

            # Build docker run command
            cmd = self._build_command(script_path)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout + 10,  # Extra time for container start
            )

            duration = time.time() - start

            if result.returncode == 0:
                return SandboxResult(
                    success=True,
                    output=result.stdout.strip(),
                    duration=duration,
                    sandbox_type="docker",
                )
            else:
                return SandboxResult(
                    success=False,
                    output=result.stdout.strip(),
                    error=result.stderr.strip(),
                    duration=duration,
                    sandbox_type="docker",
                )

        except subprocess.TimeoutExpired:
            return SandboxResult(
                success=False,
                error=f"Timeout ({self._timeout}s)",
                duration=time.time() - start,
                sandbox_type="docker",
            )
        except Exception as e:
            return SandboxResult(
                success=False,
                error=str(e),
                duration=time.time() - start,
                sandbox_type="docker",
            )
        finally:
            if script_path and os.path.exists(os.path.dirname(script_path)):
                shutil.rmtree(os.path.dirname(script_path), ignore_errors=True)

    def ensure_image(self) -> bool:
        """Pull the base Docker image if not present."""
        if not self._check_docker():
            return False
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self._image],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.info("Pulling Docker image: %s", self._image)
                subprocess.run(
                    ["docker", "pull", self._image],
                    check=True,
                    timeout=120,
                )
            return True
        except Exception as e:
            log.warning("Failed to pull Docker image: %s", e)
            return False

    # ── Internal ────────────────────────────────────────────────

    def _check_docker(self) -> bool:
        if self._docker_ok is None:
            self._docker_ok = _docker_available()
            if not self._docker_ok:
                log.info("Docker not available — using subprocess sandbox")
        return self._docker_ok

    def _write_script(self, code: str, context: Dict) -> str:
        """Write code + context to a temp directory."""
        tmpdir = tempfile.mkdtemp(prefix="ww_docker_")
        script = os.path.join(tmpdir, "user_code.py")

        # Prepare the script wrapper
        context_lines = "\n".join(
            f"{key} = {json.dumps(value)}"
            for key, value in context.items()
        )
        full_code = f"""
import json, sys, os

# Injected context
{context_lines}

# User code
{code}
"""

        with open(script, "w") as f:
            f.write(full_code)

        return script

    def _build_command(self, script_path: str) -> List[str]:
        """Build the docker run command with security options."""
        tmpdir = os.path.dirname(script_path)

        cmd = [
            "docker", "run",
            "--rm",                          # Auto-remove after execution
            "--network", "none" if not self._network else "bridge",
            "--memory", self._memory,
            "--cpus", self._cpu,
            "--read-only",                   # Root filesystem read-only
            f"--tmpfs=/tmp:rw,noexec,nosuid,size=128m",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            "--user", str(SANDBOX_UID),
            "-v", f"{tmpdir}:/code:ro",      # Mount code as read-only
            "-w", "/tmp",
            self._image,
            "python", "-u", "/code/user_code.py",
        ]

        return cmd


# ════════════════════════════════════════════════════════════════
# Sandbox Result
# ════════════════════════════════════════════════════════════════

class SandboxResult:
    """Result from sandboxed code execution."""

    def __init__(
        self,
        success: bool,
        output: str = "",
        error: str = "",
        duration: float = 0.0,
        sandbox_type: str = "subprocess",
        result_data: Any = None,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.duration = duration
        self.sandbox_type = sandbox_type
        self.result_data = result_data

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "output": self.output[:2000],
            "error": self.error[:500] if self.error else "",
            "duration_seconds": round(self.duration, 2),
            "sandbox_type": self.sandbox_type,
            "has_result": self.result_data is not None,
        }

    def __repr__(self):
        status = "OK" if self.success else "ERR"
        return f"<SandboxResult {status} ({self.sandbox_type}, {self.duration:.1f}s)>"


# ════════════════════════════════════════════════════════════════
# Security Profile Settings
# ════════════════════════════════════════════════════════════════

class SecurityProfile:
    """Per-session security profile aligned with the WW blueprint.

    Three levels:
    - deny:    Block all host exec/filesystem/network tools (public groups)
    - ask:     Require HITL confirmation for destructive operations
    - allow:   Trusted users, all tools available (with guardrails)
    """

    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"

    ALLOWED_PROFILES = {DENY, ASK, ALLOW}

    def __init__(self, profile: str = ASK):
        self.profile = profile if profile in self.ALLOWED_PROFILES else self.ASK

    def allows_exec(self) -> bool:
        return self.profile != self.DENY

    def requires_approval(self) -> bool:
        return self.profile == self.ASK

    def to_dict(self) -> dict:
        return {"security": self.profile}
