"""Wavegate DM Pairing Manager.

Implements the DM pairing protocol from the WW architecture blueprint:
- Unknown users are silently dropped
- A one-time 8-character uppercase pairing code is generated (1h TTL)
- Admin approves via CLI: `ww pairing approve <CODE>`
- Approved users are added to the whitelist

Storage:
- In-memory (default, fast)
- JSON file at ~/.ww/pairing.json (persistent across restarts)
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("gateway.pairing")

# ── Constants ──────────────────────────────────────────────────

CODE_LENGTH = 8
CODE_TTL = 3600  # 1 hour
PAIRING_STORE = os.path.expanduser("~/.ww/pairing.json")


# ════════════════════════════════════════════════════════════════
# Data Types
# ════════════════════════════════════════════════════════════════

@dataclass
class PendingPairing:
    """A pending pairing request awaiting admin approval."""

    code: str
    platform: str
    user_id: str
    display_name: str
    chat_id: str
    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > CODE_TTL

    @property
    def expires_in(self) -> float:
        return max(0, CODE_TTL - (time.time() - self.created_at))


@dataclass
class WhitelistEntry:
    """An approved user in the whitelist."""

    platform: str
    user_id: str
    display_name: str
    approved_at: float = field(default_factory=time.time)
    approved_by: str = "admin"

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WhitelistEntry":
        return cls(
            platform=data["platform"],
            user_id=data["user_id"],
            display_name=data.get("display_name", "unknown"),
            approved_at=data.get("approved_at", time.time()),
            approved_by=data.get("approved_by", "admin"),
        )


# ════════════════════════════════════════════════════════════════
# Pairing Manager
# ════════════════════════════════════════════════════════════════

class PairingManager:
    """Manages DM pairing codes and user whitelist.

    Usage:
        pm = PairingManager()

        # Unknown user contacts the bot
        code = pm.request_pairing("telegram", "123456", "Alice", "123")

        # Admin approves
        pm.approve(code)
        # or rejects
        pm.reject(code)

        # Check if user is allowed
        if pm.is_allowed("telegram", "123456"):
            process_message()
    """

    def __init__(self, store_path: str = PAIRING_STORE):
        self._store_path = store_path
        self._pending: Dict[str, PendingPairing] = {}
        self._whitelist: Dict[str, WhitelistEntry] = {}
        self._last_cleanup = time.time()
        self._load()

    # ── Public API ──────────────────────────────────────────────

    def is_allowed(self, platform: str, user_id: str) -> bool:
        """Check if a user is whitelisted."""
        key = self._whitelist_key(platform, user_id)
        return key in self._whitelist

    def add_to_whitelist(
        self,
        platform: str,
        user_id: str,
        display_name: str = "unknown",
    ) -> WhitelistEntry:
        """Directly whitelist a user (no pairing code needed)."""
        key = self._whitelist_key(platform, user_id)
        entry = WhitelistEntry(
            platform=platform,
            user_id=user_id,
            display_name=display_name,
            approved_by="auto-approve",
        )
        self._whitelist[key] = entry
        self._save()
        return entry

    def request_pairing(
        self,
        platform: str,
        user_id: str,
        display_name: str = "unknown",
        chat_id: str = "",
    ) -> str:
        """Generate a pairing code for an unknown user.

        Returns the 8-character code. If the user already has a pending
        code (not expired), returns the existing one.
        """
        self._cleanup()

        # Check if this user already has a pending code
        for code, pending in self._pending.items():
            if pending.platform == platform and pending.user_id == user_id:
                if not pending.is_expired:
                    log.info(
                        "Pairing: existing code %s for %s/%s",
                        code, platform, user_id,
                    )
                    return code

        # Generate a new code
        code = self._generate_code()
        self._pending[code] = PendingPairing(
            code=code,
            platform=platform,
            user_id=user_id,
            display_name=display_name,
            chat_id=chat_id,
        )
        self._save()
        log.info(
            "Pairing: new code %s for %s/%s (%s)",
            code, platform, user_id, display_name,
        )
        return code

    def approve(self, code: str) -> Optional[WhitelistEntry]:
        """Approve a pairing code. Adds the user to the whitelist.

        Returns the WhitelistEntry or None if code is invalid/expired.
        """
        self._cleanup()

        pending = self._pending.get(code.upper())
        if not pending:
            log.warning("Pairing: approve failed — invalid code %s", code)
            return None
        if pending.is_expired:
            log.warning("Pairing: approve failed — expired code %s", code)
            del self._pending[code]
            self._save()
            return None

        entry = WhitelistEntry(
            platform=pending.platform,
            user_id=pending.user_id,
            display_name=pending.display_name,
        )
        key = self._whitelist_key(pending.platform, pending.user_id)
        self._whitelist[key] = entry
        del self._pending[code]
        self._save()
        log.info(
            "Pairing: APPROVED %s/%s (%s) via code %s",
            pending.platform, pending.user_id, pending.display_name, code,
        )
        return entry

    def reject(self, code: str) -> Optional[PendingPairing]:
        """Reject a pairing code. Removes it from pending.

        Returns the rejected PendingPairing or None if not found.
        """
        self._cleanup()

        pending = self._pending.pop(code.upper(), None)
        if pending:
            self._save()
            log.info(
                "Pairing: REJECTED %s/%s (%s) code %s",
                pending.platform, pending.user_id, pending.display_name, code,
            )
        return pending

    def list_pending(self) -> List[PendingPairing]:
        """List all pending pairing requests."""
        self._cleanup()
        return list(self._pending.values())

    def list_whitelist(self) -> List[WhitelistEntry]:
        """List all whitelisted users."""
        return list(self._whitelist.values())

    def remove_from_whitelist(self, platform: str, user_id: str) -> bool:
        """Remove a user from the whitelist."""
        key = self._whitelist_key(platform, user_id)
        if key in self._whitelist:
            del self._whitelist[key]
            self._save()
            log.info("Pairing: removed %s/%s from whitelist", platform, user_id)
            return True
        return False

    def get_code_for_user(self, platform: str, user_id: str) -> Optional[str]:
        """Find the pairing code for a specific user, if pending."""
        self._cleanup()
        for code, pending in self._pending.items():
            if pending.platform == platform and pending.user_id == user_id:
                return code
        return None

    # ── Internal ────────────────────────────────────────────────

    def _whitelist_key(self, platform: str, user_id: str) -> str:
        return f"{platform}:{user_id}"

    def _generate_code(self) -> str:
        """Generate a unique 8-character uppercase code."""
        for _ in range(20):
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=CODE_LENGTH))
            if code not in self._pending:
                return code
        # Fallback with timestamp to guarantee uniqueness
        code = "".join(random.choices(string.ascii_uppercase, k=CODE_LENGTH))
        suffix = str(int(time.time()))[-3:]
        return code[:5] + suffix

    def _cleanup(self):
        """Remove expired pending codes."""
        now = time.time()
        if now - self._last_cleanup < 60:  # Cleanup at most once per minute
            return
        self._last_cleanup = now
        expired = [c for c, p in self._pending.items() if p.is_expired]
        for c in expired:
            del self._pending[c]
        if expired:
            self._save()
            log.info("Pairing: cleaned up %d expired codes", len(expired))

    def _save(self):
        """Persist to JSON file."""
        try:
            Path(self._store_path).parent.mkdir(parents=True, exist_ok=True)
            data = {
                "whitelist": {k: w.to_dict() for k, w in self._whitelist.items()},
                "pending": {
                    c: {
                        "code": p.code,
                        "platform": p.platform,
                        "user_id": p.user_id,
                        "display_name": p.display_name,
                        "chat_id": p.chat_id,
                        "created_at": p.created_at,
                    }
                    for c, p in self._pending.items()
                },
            }
            with open(self._store_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error("Pairing: failed to save state: %s", e)

    def _load(self):
        """Load from JSON file."""
        if not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
            for key, wdata in data.get("whitelist", {}).items():
                self._whitelist[key] = WhitelistEntry.from_dict(wdata)
            for code, pdata in data.get("pending", {}).items():
                self._pending[code] = PendingPairing(
                    code=pdata["code"],
                    platform=pdata["platform"],
                    user_id=pdata["user_id"],
                    display_name=pdata.get("display_name", "unknown"),
                    chat_id=pdata.get("chat_id", ""),
                    created_at=pdata.get("created_at", time.time()),
                )
            log.info(
                "Pairing: loaded %d whitelist entries, %d pending codes",
                len(self._whitelist), len(self._pending),
            )
        except Exception as e:
            log.error("Pairing: failed to load state: %s", e)
