"""Gateway Security Enforcer — per-session security profiles.

Blueprint ref:
  "For public-facing group chat bots, the system defaults to the strictest
   security profile (security='deny'), directly disabling all host execution
   and environment modification permissions at the gateway layer."

  "If a task indeed requires high-privilege system operations, WW supports
   HITL pre-execution confirmation (ask='always')."

This module integrates:
- SecurityProfile from sandbox/docker.py (deny/ask/allow)
- HITL approval via Telegram inline keyboard
- Tool permission enforcement at the gateway layer
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, Optional

log = logging.getLogger("gateway.security")


class SecurityEnforcer:
    """Enforces security profiles at the gateway layer.

    Usage:
        enforcer = SecurityEnforcer()
        enforcer.set_profile("telegram:-100123", "deny")

        result = enforcer.check_tool("telegram:-100123", "shell", {"command": "ls"})
        if not result.allowed:
            # Block or request HITL approval
    """

    def __init__(self, approval_callback: Optional[Callable] = None):
        """
        Args:
            approval_callback: async function(chat_id, tool_name, description, risk)
                              that returns True (approved) or False (rejected).
                              Usually calls TelegramAdapter.request_approval + wait.
        """
        from sandbox.docker import SecurityProfile
        self._profiles: Dict[str, SecurityProfile] = {}
        self._lock = threading.Lock()
        self._approval_callback = approval_callback

    def set_profile(self, session_key: str, profile: str):
        """Set the security profile for a session."""
        from sandbox.docker import SecurityProfile
        with self._lock:
            self._profiles[session_key] = SecurityProfile(profile)
        log.info("Security profile set: %s → %s", session_key, profile)

    def get_profile(self, session_key: str) -> str:
        """Get the security profile for a session. Defaults to 'ask'."""
        profile = self._profiles.get(session_key)
        if profile:
            return profile.profile
        return "ask"

    def check_tool(
        self,
        session_key: str,
        tool_name: str,
        params: Optional[Dict] = None,
    ) -> "EnforcerResult":
        """Check if a tool call should be allowed.

        Returns EnforcerResult with:
        - allowed: bool
        - requires_approval: bool (should trigger HITL)
        - reason: str
        """
        from sandbox.docker import SecurityProfile

        profile = self._profiles.get(session_key)
        if not profile:
            profile = SecurityProfile("ask")

        # Categorize the tool
        tool_category = self._categorize(tool_name)

        # DENY: block all destructive tools
        if profile.profile == SecurityProfile.DENY:
            if tool_category in ("exec", "file_write", "network"):
                return EnforcerResult(
                    allowed=False,
                    requires_approval=False,
                    reason=f"Tool '{tool_name}' blocked by security=deny profile",
                )
            return EnforcerResult(allowed=True)

        # ASK: require HITL for destructive tools
        if profile.profile == SecurityProfile.ASK:
            if tool_category in ("exec", "file_write",):
                return EnforcerResult(
                    allowed=True,  # Allowed, but requires approval
                    requires_approval=True,
                    reason=f"Tool '{tool_name}' requires HITL approval",
                    tool_name=tool_name,
                    params=params,
                )
            return EnforcerResult(allowed=True)

        # ALLOW: everything passes (guardrails still active at core level)
        return EnforcerResult(allowed=True)

    def _categorize(self, tool_name: str) -> str:
        """Categorize a tool by its risk level."""
        exec_tools = {
            "shell", "exec", "bash", "terminal", "run",
            "subprocess", "command", "script",
        }
        file_tools = {
            "file_write", "write_file", "patch", "edit",
            "delete", "rm", "mv", "cp",
        }
        network_tools = {
            "web_search", "http", "curl", "wget",
            "browser", "fetch", "download",
        }

        if tool_name in exec_tools:
            return "exec"
        if tool_name in file_tools:
            return "file_write"
        if tool_name in network_tools:
            return "network"
        return "safe"


class EnforcerResult:
    """Result from SecurityEnforcer.check_tool()."""

    def __init__(
        self,
        allowed: bool,
        requires_approval: bool = False,
        reason: str = "",
        tool_name: str = "",
        params: Optional[Dict] = None,
    ):
        self.allowed = allowed
        self.requires_approval = requires_approval
        self.reason = reason
        self.tool_name = tool_name
        self.params = params or {}

    def __bool__(self):
        return self.allowed
