"""Wavegate Multi-Tenancy Manager.

Implements hierarchical namespace isolation per the WW architecture blueprint:
  {tenant_id}:{platform}:{user_id}:{chat_id}

Each tenant has isolated:
- Session keys
- Whitelists
- Queue state

Defaults:
- Default tenant: "default" (backward compatible with existing single-tenant installs)
- Tenants are created implicitly on first use
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("gateway.tenant")

DEFAULT_TENANT = "default"
TENANTS_STORE = os.path.expanduser("~/.ww/tenants.json")


@dataclass
class Tenant:
    """A tenant (organization/project) in the WW system."""

    tenant_id: str
    display_name: str = ""
    created_at: float = field(default_factory=time.time)
    is_active: bool = True
    max_sessions: int = 100
    max_users: int = 50

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "is_active": self.is_active,
            "max_sessions": self.max_sessions,
            "max_users": self.max_users,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Tenant":
        return cls(
            tenant_id=data["tenant_id"],
            display_name=data.get("display_name", ""),
            created_at=data.get("created_at", time.time()),
            is_active=data.get("is_active", True),
            max_sessions=data.get("max_sessions", 100),
            max_users=data.get("max_users", 50),
        )


class TenantManager:
    """Manages tenants and provides tenant-scoped key generation.

    Usage:
        tm = TenantManager()
        session_key = tm.build_key("org_123", "telegram", "456", "789")
        # → "org_123:telegram:456:789"

        # Default tenant (backward compatible)
        session_key = tm.build_key(tenant_id="", ...)
        # → "default:telegram:456:789"
    """

    def __init__(self, store_path: str = TENANTS_STORE):
        self._store_path = store_path
        self._tenants: Dict[str, Tenant] = {}
        self._load()
        # Ensure default tenant always exists
        if DEFAULT_TENANT not in self._tenants:
            self._tenants[DEFAULT_TENANT] = Tenant(
                tenant_id=DEFAULT_TENANT,
                display_name="Default",
            )
            self._save()

    # ── Key Generation ─────────────────────────────────────────

    def build_key(
        self,
        tenant_id: str,
        platform: str,
        user_id: str,
        chat_id: str = "",
    ) -> str:
        """Build a hierarchical session key.

        Format: {tenant}:{platform}:{user}:{chat}
        If tenant_id is empty, uses DEFAULT_TENANT.
        """
        tenant = tenant_id or DEFAULT_TENANT
        return f"{tenant}:{platform}:{user_id}:{chat_id}"

    def parse_key(self, session_key: str) -> dict:
        """Parse a session key into its components.

        Returns dict with keys: tenant_id, platform, user_id, chat_id.
        Handles both 3-part (old) and 4-part (new) keys.
        """
        parts = session_key.split(":")
        if len(parts) == 4:
            return {
                "tenant_id": parts[0],
                "platform": parts[1],
                "user_id": parts[2],
                "chat_id": parts[3],
            }
        elif len(parts) == 3:
            # Legacy key format: platform:user:chat
            return {
                "tenant_id": DEFAULT_TENANT,
                "platform": parts[0],
                "user_id": parts[1],
                "chat_id": parts[2],
            }
        else:
            return {
                "tenant_id": DEFAULT_TENANT,
                "platform": "",
                "user_id": session_key,
                "chat_id": "",
            }

    def get_tenant_for_key(self, session_key: str) -> str:
        """Extract the tenant_id from a session key."""
        return self.parse_key(session_key)["tenant_id"]

    # ── Tenant CRUD ────────────────────────────────────────────

    def get(self, tenant_id: str) -> Optional[Tenant]:
        """Get a tenant by ID."""
        return self._tenants.get(tenant_id)

    def get_or_create(self, tenant_id: str, display_name: str = "") -> Tenant:
        """Get or create a tenant."""
        if tenant_id in self._tenants:
            return self._tenants[tenant_id]
        tenant = Tenant(
            tenant_id=tenant_id,
            display_name=display_name or tenant_id,
        )
        self._tenants[tenant_id] = tenant
        self._save()
        log.info("Tenant created: %s", tenant_id)
        return tenant

    def list_active(self) -> List[Tenant]:
        """List all active tenants."""
        return [t for t in self._tenants.values() if t.is_active]

    def deactivate(self, tenant_id: str) -> bool:
        """Deactivate a tenant. Cannot deactivate default."""
        if tenant_id == DEFAULT_TENANT:
            log.warning("Cannot deactivate default tenant")
            return False
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        tenant.is_active = False
        self._save()
        log.info("Tenant deactivated: %s", tenant_id)
        return True

    def activate(self, tenant_id: str) -> bool:
        """Reactivate a tenant."""
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        tenant.is_active = True
        self._save()
        return True

    def is_active(self, tenant_id: str) -> bool:
        """Check if a tenant is active. Default tenant is always active."""
        if tenant_id == DEFAULT_TENANT or not tenant_id:
            return True
        tenant = self._tenants.get(tenant_id)
        return tenant is not None and tenant.is_active

    # ── Persistence ────────────────────────────────────────────

    def _save(self):
        try:
            Path(self._store_path).parent.mkdir(parents=True, exist_ok=True)
            data = {k: t.to_dict() for k, t in self._tenants.items()}
            with open(self._store_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error("Tenant save failed: %s", e)

    def _load(self):
        if not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
            for key, tdata in data.items():
                self._tenants[key] = Tenant.from_dict(tdata)
        except Exception as e:
            log.error("Tenant load failed: %s", e)
