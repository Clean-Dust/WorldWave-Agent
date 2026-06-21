"""Wavegate Multi-Tenant Manager.

Provides tenant isolation across all Wavegate services.
Each tenant is a namespace with its own API keys, rate limits, and sessions.

Key format: {tenant_id}:{platform}:{user_id}:{chat_id}
Default tenant: "default" (backward compatible with single-tenant deployments)

Features:
- Tenant CRUD (create, get, list, delete)
- API key per tenant with scoped permissions
- Rate limiting per tenant
- Tenant-aware session routing
- Configurable via env or config file
- JSON persistence (survives restarts)

Usage:
    tm = TenantManager()
    tm.create("acme-corp", quota={"max_sessions": 100, "max_rpm": 60})
    tm.validate_api_key("acme-corp", "key-xxx")  # True/False
    key = tm.make_session_key("acme-corp", "telegram", "user1", "chat1")
    # → "acme-corp:telegram:user1:chat1"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("gateway.tenant")

# Default tenant ID (for single-tenant / backward compat)
DEFAULT_TENANT = "default"


@dataclass
class TenantQuota:
    """Per-tenant resource limits."""

    max_sessions: int = 50
    max_rpm: int = 30  # requests per minute
    max_concurrent_goals: int = 3
    max_history_days: int = 30

    @classmethod
    def from_dict(cls, d: dict) -> "TenantQuota":
        return cls(
            max_sessions=d.get("max_sessions", 50),
            max_rpm=d.get("max_rpm", 30),
            max_concurrent_goals=d.get("max_concurrent_goals", 3),
            max_history_days=d.get("max_history_days", 30),
        )

    def to_dict(self) -> dict:
        return {
            "max_sessions": self.max_sessions,
            "max_rpm": self.max_rpm,
            "max_concurrent_goals": self.max_concurrent_goals,
            "max_history_days": self.max_history_days,
        }


@dataclass
class Tenant:
    """A multi-tenant namespace."""

    tenant_id: str
    display_name: str = ""
    api_key_hash: str = ""  # SHA256 of the API key (never store plaintext)
    quota: TenantQuota = field(default_factory=TenantQuota)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    # Runtime counters (not persisted)
    _sessions: set = field(default_factory=set, repr=False)
    _request_timestamps: list = field(default_factory=list, repr=False)
    _rate_lock: object = field(default_factory=threading.Lock, repr=False)

    def check_rate_limit(self) -> bool:
        """Check if tenant is within rate limits. Returns True if allowed."""
        with self._rate_lock:
            now = time.time()
            # Prune old timestamps (>60s)
            cutoff = now - 60
            self._request_timestamps = [t for t in self._request_timestamps if t > cutoff]
            if len(self._request_timestamps) >= self.quota.max_rpm:
                return False
            self._request_timestamps.append(now)
            return True

    def add_session(self, session_key: str):
        self._sessions.add(session_key)

    def remove_session(self, session_key: str):
        self._sessions.discard(session_key)

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    def to_dict(self, include_sensitive: bool = False) -> dict:
        d = {
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "quota": self.quota.to_dict(),
            "created_at": self.created_at,
            "active_sessions": self.active_sessions,
            "metadata": self.metadata,
        }
        if include_sensitive:
            d["api_key_hash"] = self.api_key_hash
        return d


class TenantManager:
    """Manages multi-tenant namespaces and enforces isolation."""

    def __init__(self, tenants_file: str = None):
        self._tenants: Dict[str, Tenant] = {}
        self._keys: Dict[str, str] = {}  # key_hash → tenant_id reverse index
        self._lock = threading.Lock()
        self._tenants_file = tenants_file or os.path.join(
            os.environ.get("WW_HOME", os.path.expanduser("~/.ww")),
            "tenants.json",
        )

        # Restore from persisted file
        self._load()

        # Always create default tenant (won't overwrite if already loaded)
        self._ensure_default()
        self._save()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self):
        """Restore tenants from JSON file."""
        if not os.path.exists(self._tenants_file):
            return
        try:
            with open(self._tenants_file) as f:
                data = json.load(f)
            for tid, td in data.items():
                self._tenants[tid] = Tenant(
                    tenant_id=td["tenant_id"],
                    display_name=td.get("display_name", ""),
                    api_key_hash=td.get("api_key_hash", ""),
                    quota=TenantQuota.from_dict(td.get("quota", {})),
                    enabled=td.get("enabled", True),
                    created_at=td.get("created_at", time.time()),
                    metadata=td.get("metadata", {}),
                )
                if td.get("api_key_hash"):
                    self._keys[td["api_key_hash"]] = tid
            log.info("Loaded %d tenants from %s", len(self._tenants), self._tenants_file)
        except Exception as e:
            log.warning("Failed to load tenants from %s: %s", self._tenants_file, e)

    def _save(self):
        """Persist tenants to JSON file."""
        try:
            os.makedirs(os.path.dirname(self._tenants_file), exist_ok=True)
            data = {}
            for tid, t in self._tenants.items():
                data[tid] = {
                    "tenant_id": t.tenant_id,
                    "display_name": t.display_name,
                    "api_key_hash": t.api_key_hash,
                    "quota": t.quota.to_dict(),
                    "enabled": t.enabled,
                    "created_at": t.created_at,
                    "metadata": t.metadata,
                }
            with open(self._tenants_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning("Failed to save tenants: %s", e)

    def _ensure_default(self):
        """Create default tenant if it doesn't exist."""
        if DEFAULT_TENANT not in self._tenants:
            default_api_key = os.environ.get("WW_API_KEY", "")
            key_hash = (
                _hash_key(default_api_key) if default_api_key else ""
            )
            self._tenants[DEFAULT_TENANT] = Tenant(
                tenant_id=DEFAULT_TENANT,
                display_name="Default",
                api_key_hash=key_hash,
                quota=TenantQuota(max_sessions=200, max_rpm=120, max_concurrent_goals=10),
            )
            if key_hash:
                self._keys[key_hash] = DEFAULT_TENANT

    # ── CRUD ───────────────────────────────────────────────────

    def create(
        self,
        tenant_id: str,
        display_name: str = "",
        api_key: str = None,
        quota: dict = None,
        metadata: dict = None,
    ) -> Tenant:
        """Create a new tenant. Auto-generates API key if not provided."""
        with self._lock:
            if tenant_id in self._tenants:
                raise ValueError(f"Tenant already exists: {tenant_id}")

            key = api_key or _generate_api_key()
            key_hash = _hash_key(key)
            tenant = Tenant(
                tenant_id=tenant_id,
                display_name=display_name or tenant_id,
                api_key_hash=key_hash,
                quota=TenantQuota.from_dict(quota or {}),
                metadata=metadata or {},
            )
            self._tenants[tenant_id] = tenant
            self._keys[key_hash] = tenant_id
            self._save()
            log.info("Tenant created: %s", tenant_id)

            # Return with plaintext key (caller must store it)
            tenant._plaintext_key = key
            return tenant

    def get(self, tenant_id: str) -> Optional[Tenant]:
        with self._lock:
            return self._tenants.get(tenant_id)

    def list_all(self) -> List[dict]:
        with self._lock:
            return [t.to_dict() for t in self._tenants.values()]

    def delete(self, tenant_id: str) -> bool:
        if tenant_id == DEFAULT_TENANT:
            raise ValueError("Cannot delete default tenant")
        with self._lock:
            if tenant_id in self._tenants:
                tenant = self._tenants[tenant_id]
                if tenant.api_key_hash and tenant.api_key_hash in self._keys:
                    del self._keys[tenant.api_key_hash]
                del self._tenants[tenant_id]
                self._save()
                log.info("Tenant deleted: %s", tenant_id)
                return True
        return False

    def enable(self, tenant_id: str) -> bool:
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant:
                tenant.enabled = True
                self._save()
                return True
        return False

    def disable(self, tenant_id: str) -> bool:
        if tenant_id == DEFAULT_TENANT:
            raise ValueError("Cannot disable default tenant")
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant:
                tenant.enabled = False
                self._save()
                return True
        return False

    # ── Auth ────────────────────────────────────────────────────

    def validate_api_key(self, tenant_id: str, api_key: str) -> bool:
        """Validate an API key against a tenant."""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if not tenant or not tenant.enabled:
                return False
            if not tenant.api_key_hash:
                return True  # No key set — open access
            return hmac.compare_digest(
                tenant.api_key_hash.encode(), _hash_key(api_key).encode()
            )

    def authenticate(self, api_key: str) -> Optional[str]:
        """Find which tenant (if any) an API key belongs to. Returns tenant_id."""
        key_hash = _hash_key(api_key)
        with self._lock:
            # O(1) reverse-index lookup
            tid = self._keys.get(key_hash)
            if tid:
                tenant = self._tenants.get(tid)
                if tenant and tenant.enabled and tenant.api_key_hash:
                    # Constant-time comparison as defense-in-depth
                    if hmac.compare_digest(
                        tenant.api_key_hash.encode(), key_hash.encode()
                    ):
                        return tid
            return None

    def rotate_key(self, tenant_id: str) -> Optional[str]:
        """Generate a new API key for a tenant."""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if not tenant:
                return None
            # Remove old key from reverse index
            if tenant.api_key_hash and tenant.api_key_hash in self._keys:
                del self._keys[tenant.api_key_hash]
            new_key = _generate_api_key()
            tenant.api_key_hash = _hash_key(new_key)
            self._keys[tenant.api_key_hash] = tenant_id
            self._save()
            log.info("API key rotated for tenant: %s", tenant_id)
            return new_key

    # ── Session Keys ───────────────────────────────────────────

    @staticmethod
    def make_session_key(
        tenant_id: str, platform: str, user_id: str, chat_id: str
    ) -> str:
        """Build a tenant-aware session key."""
        return f"{tenant_id}:{platform}:{user_id}:{chat_id}"

    @staticmethod
    def parse_session_key(session_key: str) -> dict:
        """Parse a session key into its components."""
        parts = session_key.split(":", 3)
        if len(parts) == 4:
            return {
                "tenant_id": parts[0],
                "platform": parts[1],
                "user_id": parts[2],
                "chat_id": parts[3],
            }
        # Backward compat: old 3-part keys get default tenant
        if len(parts) == 3:
            return {
                "tenant_id": DEFAULT_TENANT,
                "platform": parts[0],
                "user_id": parts[1],
                "chat_id": parts[2],
            }
        return {
            "tenant_id": DEFAULT_TENANT,
            "platform": "unknown",
            "user_id": session_key,
            "chat_id": "",
        }

    # ── Rate Limiting ──────────────────────────────────────────

    def check_rate_limit(self, tenant_id: str) -> bool:
        """Check if tenant is within rate limits."""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if not tenant or not tenant.enabled:
                return False
            return tenant.check_rate_limit()

    def record_request(self, tenant_id: str):
        """Record a request for rate limiting."""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant:
                tenant.check_rate_limit()  # Prunes + appends timestamp

    # ── Health ─────────────────────────────────────────────────

    def health(self) -> dict:
        with self._lock:
            return {
                "total_tenants": len(self._tenants),
                "active_tenants": sum(1 for t in self._tenants.values() if t.enabled),
                "total_sessions": sum(t.active_sessions for t in self._tenants.values()),
                "default_api_key_set": bool(self._tenants[DEFAULT_TENANT].api_key_hash) if DEFAULT_TENANT in self._tenants else False,
            }


# ── Helpers ────────────────────────────────────────────────────

def _hash_key(key: str) -> str:
    """SHA256 hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def _generate_api_key(prefix: str = "ww") -> str:
    """Generate a secure API key: ww_<32 random hex chars>."""
    return f"{prefix}_{secrets.token_hex(16)}"
