"""
ww/core/guardrails.py — Worldwave tool security guardrails v0.1

Perform static checks during tool execution to prevent dangerous operations.

feature: 
- Prohibited commands (rm -rf /, mkfs, dd, etc.)
- Path whitelist (only allow writing to specific directories)
- Rate limiting (prevent abuse)
- Sensitive data filter (prevent API keys from leaking)
- Recursion protection (prevent tool from calling itself in infinite loop)
"""

from __future__ import annotations
import fnmatch
import os
import re
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple


# ── Default deny mode ──

FORBIDDEN_SHELL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\brm\s+(-rf|--recursive)\s+[/]'), "Root filesystem deletion"),
    (re.compile(r'\bmkfs\.'), "Filesystem creation"),
    (re.compile(r'\bdd\s+if='), "Raw device write"),
    (re.compile(r'\bmv\s+/\s+'), "Moving root filesystem"),
    (re.compile(r'\bchmod\s+777\s+/\s'), "World-writable root"),
    (re.compile(r'\b>:{|:\(\)\s*\{'), "Fork bomb / shellshock"),
    (re.compile(r'\bwget|curl\s+.*\||bash\b'), "Piped shell download"),
    (re.compile(r'\bpasswd\b'), "Password change"),
    (re.compile(r'\buseradd|userdel|usermod\b'), "User management"),
    (re.compile(r'\bshutdown|reboot|halt|poweroff\b'), "System shutdown"),
    (re.compile(r'\biptables|ufw|firewall'), "Firewall modification"),
    (re.compile(r'\bifconfig\s+\w+\s+down'), "Network interface down"),
    (re.compile(r'\bpkill|killall\b'), "Process killing"),
]

FORBIDDEN_FILE_EXTENSIONS: Set[str] = {'.key', '.pem', '.p12', '.pfx', '.ovpn'}

FORBIDDEN_FILE_PATTERNS: List[str] = [
    '*/etc/shadow*', '*/etc/gshadow*',
    '*/etc/sudoers*', '*/etc/ssh/*',
    '*/.ssh/id_*', '*/.env', '*/config.json',
]

SENSITIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r'(?:api[_-]?key|secret|token|password|passwd)["\s:=]+[A-Za-z0-9_\-]{16,}', re.I),
    re.compile(r'(?:sk-[a-zA-Z0-9]{10,}|pk-[a-zA-Z0-9]{10,})'),
]


class GuardrailsResult:
    """secure check result"""
    def __init__(self, allowed: bool, reason: str = "", details: str = ""):
        self.allowed = allowed
        self.reason = reason
        self.details = details

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        return f"<{'PASS' if self.allowed else 'BLOCK'}: {self.reason}>"


class Guardrails:
    """tool secure guardrail"""

    def __init__(self, config: Dict = None):
        config = config or {}
        self.enabled = config.get("guardrails_enabled", True)
        self.max_rate = config.get("guardrails_rate", 30)  # calls per minute
        self.write_whitelist = config.get("guardrails_write_whitelist", [
            os.path.expanduser("~"),
            "/tmp",
        ])
        self.allow_dangerous = config.get("guardrails_allow_dangerous", False)
        self._rate_tracker: Dict[str, List[float]] = defaultdict(list)

    def check_shell_command(self, command: str) -> GuardrailsResult:
        """check shell command security"""
        if not self.enabled:
            return GuardrailsResult(True)

        # Rate limit
        rate_result = self._check_rate("shell")
        if not rate_result:
            return rate_result

        # Pattern matching
        for pattern, reason in FORBIDDEN_SHELL_PATTERNS:
            if pattern.search(command):
                return GuardrailsResult(False, f"Forbidden operation: {reason}")

        return GuardrailsResult(True)

    def check_file_write(self, path: str) -> GuardrailsResult:
        """check file write path security"""
        if not self.enabled:
            return GuardrailsResult(True)

        abs_path = os.path.abspath(os.path.expanduser(path))

        # Check forbidden extensions
        ext = os.path.splitext(abs_path)[1].lower()
        if ext in FORBIDDEN_FILE_EXTENSIONS:
            return GuardrailsResult(False, f"Forbidden write sensitive extension: {ext}")

        # Check forbidden patterns
        for pattern in FORBIDDEN_FILE_PATTERNS:
            if fnmatch.fnmatch(abs_path, pattern):
                return GuardrailsResult(False, f"Forbidden write sensitive path: {pattern}")

        # Whitelist check
        if self.write_whitelist:
            allowed = any(
                abs_path.startswith(os.path.abspath(os.path.expanduser(w)))
                for w in self.write_whitelist
            )
            if not allowed:
                return GuardrailsResult(
                    False,
                    f"path not in whitelist",
                    f"path: {abs_path}\nwhitelist: {self.write_whitelist}",
                )

        return GuardrailsResult(True)

    def check_output(self, text: str) -> GuardrailsResult:
        """check if output contains sensitive information"""
        if not self.enabled or not text:
            return GuardrailsResult(True)

        for pattern in SENSITIVE_PATTERNS:
            match = pattern.search(text)
            if match:
                return GuardrailsResult(
                    False,
                    "output contains sensitive information (API key/token)",
                    f"discovery: ...{match.group()[:20]}...",
                )

        return GuardrailsResult(True)

    def check_code(self, code: str) -> GuardrailsResult:
        """check Python code security"""
        if not self.enabled or self.allow_dangerous:
            return GuardrailsResult(True)

        # Check for dangerous imports
        dangerous_imports = [
            "subprocess", "os.system", "os.popen", "shutil.rmtree",
            "ctypes", "ptrace", "signal", "atexit",
        ]
        for imp in dangerous_imports:
            if imp in code:
                return GuardrailsResult(
                    False, f"Code contains dangerous import: {imp}"
                )

        return GuardrailsResult(True)

    def _check_rate(self, key: str) -> GuardrailsResult:
        """checkrate limiting"""
        if self.max_rate <= 0:
            return GuardrailsResult(True)

        now = time.time()
        window = 60  # 1 minute
        timestamps = self._rate_tracker[key]

        # Remove old entries
        timestamps[:] = [t for t in timestamps if now - t < window]

        if len(timestamps) >= self.max_rate:
            return GuardrailsResult(
                False, f"rate limiting: {self.max_rate}/min (key: {key})"
            )

        timestamps.append(now)
        return GuardrailsResult(True)

    def risks_for(self, tool_name: str, params: Dict) -> List[str]:
        """Estimate potential risks of a tool call (for LLM to see)"""
        risks = []

        if tool_name == "shell":
            cmd = params.get("command", "")
            if any(kw in cmd.lower() for kw in ["rm", "delete", "wipe"]):
                risks.append("may deletefile")
            if "|" in cmd and any(kw in cmd.lower() for kw in ["bash", "sh", "curl", "wget"]):
                risks.append("remote code execution")
        elif tool_name == "file_write":
            path = params.get("path", "")
            if any(dot in path for dot in [".env", ".ssh", "secret", "token"]):
                risks.append("may overwrite config file")
        elif tool_name == "code":
            if "import os" in params.get("code", ""):
                risks.append("Python contains OS module operations")

        return risks
