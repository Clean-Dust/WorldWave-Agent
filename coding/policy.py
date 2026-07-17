"""coding/policy.py — Deny-first policy, causal verify gate, secret scan.

Enforces:
- Dangerous shell pattern denial (coding_exec / sandbox_exec)
- Soft causal default: after coding writes, block git commit until verify green
- Secret scan for sk- / api_key / PRIVATE KEY on commit/patch
- Capability mutex helper (default role = coder; architect cannot edit)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ── Dangerous command patterns ────────────────────────────────────────

_DANGEROUS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+)*(/|/\*|~|/home|/usr|/var|/etc)\b", re.I),
     "Denied: recursive force-remove targeting system or home root is catastrophic."),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/", re.I),
     "Denied: rm -rf / and variants wipe the filesystem root."),
    (re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/", re.I),
     "Denied: rm -fr / and variants wipe the filesystem root."),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I),
     "Denied: mkfs formats disks and destroys data."),
    (re.compile(r"\bdd\b.*\bof\s*=\s*/dev/", re.I),
     "Denied: dd writing to /dev/ can destroy block devices."),
    (re.compile(r"\b(curl|wget)\b.+\|\s*(ba)?sh\b", re.I),
     "Denied: piping remote content into a shell is a remote-code-execution vector."),
    (re.compile(r">\s*/dev/sd[a-z]", re.I),
     "Denied: redirecting to raw block devices destroys data."),
    (re.compile(r"\bchmod\s+(-R\s+)?777\s+/", re.I),
     "Denied: chmod 777 on system roots is unsafe."),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", re.I),
     "Denied: fork-bomb pattern."),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt)\b", re.I),
     "Denied: host power commands are out of scope for coding agents."),
]

# Secret patterns that must not land in commits/patches
_SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
     "Secret scan: looks like an API key starting with 'sk-'."),
    (re.compile(r"(?i)\bapi[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{8,}"),
     "Secret scan: api_key assignment detected."),
    (re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----"),
     "Secret scan: PRIVATE KEY block detected."),
    (re.compile(r"(?i)\b(aws_secret_access_key|secret_access_key)\s*[=:]\s*\S+"),
     "Secret scan: cloud secret key assignment detected."),
]


def _extra_deny_patterns() -> List[Tuple[re.Pattern, str]]:
    """Parse WW_CODING_DENY_EXTRA as comma-separated regexes."""
    raw = os.environ.get("WW_CODING_DENY_EXTRA", "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append((re.compile(part, re.I), f"Denied by WW_CODING_DENY_EXTRA: /{part}/"))
        except re.error:
            out.append((re.compile(re.escape(part), re.I), f"Denied by WW_CODING_DENY_EXTRA: {part}"))
    return out


def check_command_allowed(command: str) -> Dict:
    """Deny-first check for shell commands.

    Returns {"allowed": bool, "reason": str, "pattern": optional}.
    """
    if not command or not str(command).strip():
        return {"allowed": False, "reason": "Empty command denied."}

    cmd = str(command)
    for pattern, reason in _DANGEROUS_PATTERNS + _extra_deny_patterns():
        if pattern.search(cmd):
            return {
                "allowed": False,
                "reason": reason,
                "pattern": pattern.pattern,
                "command_preview": cmd[:200],
            }
    return {"allowed": True, "reason": ""}


def scan_secrets(text: str) -> Dict:
    """Scan text for secrets. Returns {"clean": bool, "findings": [...]}."""
    findings = []
    if not text:
        return {"clean": True, "findings": []}
    for pattern, reason in _SECRET_PATTERNS:
        m = pattern.search(text)
        if m:
            findings.append({
                "reason": reason,
                "match_preview": m.group(0)[:40] + ("..." if len(m.group(0)) > 40 else ""),
            })
    return {"clean": len(findings) == 0, "findings": findings}


def check_content_secrets(text: str) -> Dict:
    """Block if secrets found. API used by patch/commit paths."""
    scan = scan_secrets(text)
    if scan["clean"]:
        return {"allowed": True, "reason": ""}
    reasons = "; ".join(f["reason"] for f in scan["findings"])
    return {
        "allowed": False,
        "reason": reasons,
        "findings": scan["findings"],
    }


# ── Causal gate (soft default ON) ─────────────────────────────────────

class CausalState:
    """Track pending unverified writes that block git commit."""

    def __init__(self):
        self._pending_writes: List[Dict] = []
        self._last_verify: Optional[Dict] = None
        self._last_verify_ok: bool = False

    def causal_enabled(self) -> bool:
        """WW_CODING_CAUSAL=0 disables; default ON."""
        val = os.environ.get("WW_CODING_CAUSAL", "1").strip().lower()
        return val not in ("0", "false", "off", "no")

    def record_write(self, path: str, kind: str = "edit") -> None:
        self._pending_writes.append({
            "path": path,
            "kind": kind,
            "ts": time.time(),
        })
        # A new write invalidates prior green verify for commit gating
        self._last_verify_ok = False

    def record_verify(self, result: Dict) -> None:
        self._last_verify = result
        self._last_verify_ok = bool(result.get("success") or result.get("passed", 0) > 0) and not result.get("failed", 0)
        # Also accept explicit success flag with exit 0
        if result.get("success") is True and result.get("exit_code", 0) == 0:
            self._last_verify_ok = True
        if result.get("success") is True and result.get("failed", 0) == 0:
            self._last_verify_ok = True
        if self._last_verify_ok:
            self._pending_writes.clear()

    def has_pending_writes(self) -> bool:
        return len(self._pending_writes) > 0

    def last_verify_green(self) -> bool:
        return self._last_verify_ok

    def check_git_commit_allowed(self) -> Dict:
        """API: block commit after coding writes until verify/tests green."""
        if not self.causal_enabled():
            return {
                "allowed": True,
                "reason": "Causal gate disabled (WW_CODING_CAUSAL=0).",
                "causal": False,
            }
        if not self._pending_writes:
            return {
                "allowed": True,
                "reason": "No pending coding writes.",
                "causal": True,
            }
        if self._last_verify_ok:
            return {
                "allowed": True,
                "reason": "Last verify was green.",
                "causal": True,
            }
        paths = [w["path"] for w in self._pending_writes[-5:]]
        return {
            "allowed": False,
            "reason": (
                "Causal policy: coding edits were made without a green verify/test run. "
                "Run coding_verify (or tests) successfully before git commit. "
                f"Pending files: {paths}"
            ),
            "causal": True,
            "pending_writes": list(self._pending_writes),
            "last_verify": self._last_verify,
        }

    def require_test_for_ticket(self) -> bool:
        val = os.environ.get("WW_CODING_REQUIRE_TEST", "0").strip().lower()
        return val in ("1", "true", "yes", "on")

    def check_mark_ticket_done_allowed(self, ticket: Optional[Dict] = None) -> Dict:
        """When WW_CODING_REQUIRE_TEST=1 or ticket looks like a test ticket, require green verify."""
        need = self.require_test_for_ticket()
        if ticket:
            title = (ticket.get("title") or "") + " " + (ticket.get("description") or "")
            if re.search(r"\b(test|verify|pytest|unittest)\b", title, re.I):
                need = True
        if not need:
            return {"allowed": True, "reason": ""}
        if self._last_verify_ok:
            return {"allowed": True, "reason": "Last verify green."}
        return {
            "allowed": False,
            "reason": (
                "mark_ticket_done blocked: WW_CODING_REQUIRE_TEST or test ticket requires "
                "a green coding_verify result first."
            ),
            "last_verify": self._last_verify,
        }

    def reset(self) -> None:
        self._pending_writes.clear()
        self._last_verify = None
        self._last_verify_ok = False

    def to_dict(self) -> Dict:
        return {
            "causal_enabled": self.causal_enabled(),
            "pending_writes": list(self._pending_writes),
            "last_verify_ok": self._last_verify_ok,
            "last_verify": self._last_verify,
        }


_causal: Optional[CausalState] = None


def get_causal_state() -> CausalState:
    global _causal
    if _causal is None:
        _causal = CausalState()
    return _causal


def check_git_commit_allowed() -> Dict:
    """Public API used by git_commit tool and tests."""
    return get_causal_state().check_git_commit_allowed()


def record_coding_write(path: str, kind: str = "edit") -> None:
    get_causal_state().record_write(path, kind)


def record_verify_result(result: Dict) -> None:
    get_causal_state().record_verify(result)


# ── Edit log ──────────────────────────────────────────────────────────

def append_edit_log(project_root: str, entry: Dict) -> str:
    """Append a successful edit record to <project>/.ww/edit_log.jsonl."""
    ww_dir = os.path.join(project_root, ".ww")
    os.makedirs(ww_dir, exist_ok=True)
    path = os.path.join(ww_dir, "edit_log.jsonl")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return path


def find_project_root(start: str = None) -> str:
    """Walk up for .git or .ww; fall back to cwd."""
    path = os.path.abspath(start or os.getcwd())
    for _ in range(20):
        if os.path.isdir(os.path.join(path, ".git")) or os.path.isdir(os.path.join(path, ".ww")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return os.path.abspath(start or os.getcwd())


# ── Capability role default ───────────────────────────────────────────

def default_coding_role() -> str:
    """Default role for coding tasks is coder (can edit)."""
    return os.environ.get("WW_CODING_ROLE", "coder").strip().lower() or "coder"


def architect_cannot_edit(role: str, tool_name: str) -> Dict:
    """Quick check: architect cannot use edit tools."""
    edit_prefixes = (
        "coding_edit", "coding_write", "coding_apply_patch",
        "coding_ast_rewrite",
    )
    if role == "architect" and any(tool_name.startswith(p) or tool_name == p for p in edit_prefixes):
        return {
            "allowed": False,
            "reason": f"Role 'architect' cannot edit (tool={tool_name}). Switch to coder.",
        }
    return {"allowed": True, "reason": ""}
