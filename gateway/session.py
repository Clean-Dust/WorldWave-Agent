"""Wavegate Session Manager.

Tracks active sessions across all platforms.
Each session is keyed by "{platform}:{user_id}:{chat_id}".

Responsible for:
- Session lifecycle (create, get, expire)
- Multi-tenancy namespace mapping
- Per-session metadata (current goal, last activity, etc.)

Backends:
- In-memory (default, fast, ephemeral)
- NATS KV Store (persistent, survives restarts, shared across instances)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger("gateway.session")

# Session TTL (24 hours)
SESSION_TTL = 86400


@dataclass
class Session:
    """A single user session."""

    session_key: str
    platform: str
    user_id: str
    chat_id: str
    display_name: str = "unknown"
    role: str = "operator"
    tenant_id: str = ""
    permission_level: int = 1
    current_goal: str = ""
    goal_task_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return time.time() - self.last_active > SESSION_TTL

    def touch(self):
        self.last_active = time.time()

    def to_dict(self) -> dict:
        return {
            "session_key": self.session_key,
            "platform": self.platform,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "display_name": self.display_name,
            "role": self.role,
            "tenant_id": self.tenant_id,
            "permission_level": self.permission_level,
            "current_goal": self.current_goal,
            "goal_task_id": self.goal_task_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            session_key=data.get("session_key", ""),
            platform=data.get("platform", ""),
            user_id=data.get("user_id", ""),
            chat_id=data.get("chat_id", ""),
            display_name=data.get("display_name", "unknown"),
            role=data.get("role", "operator"),
            tenant_id=data.get("tenant_id", ""),
            permission_level=data.get("permission_level", 1),
            current_goal=data.get("current_goal", ""),
            goal_task_id=data.get("goal_task_id", ""),
            created_at=data.get("created_at", time.time()),
            last_active=data.get("last_active", time.time()),
        )

    @classmethod
    def from_sender(cls, session_key: str, sender, platform: str) -> "Session":
        """Create a Session from a sender protobuf object.

        Parses the session_key to extract platform/user/chat components.
        Handles both legacy 3-part (platform:user:chat) and new 4-part
        (tenant:platform:user:chat) key formats.
        """
        parts = session_key.split(":")
        if len(parts) == 4:
            # New format: tenant:platform:user:chat
            return cls(
                session_key=session_key,
                platform=parts[1],
                user_id=parts[2],
                chat_id=parts[3],
                display_name=sender.display_name if sender else "unknown",
                role=sender.role if sender else "operator",
                tenant_id=parts[0],
                permission_level=sender.permission_level if sender else 1,
            )
        # Legacy format: platform:user:chat
        return cls(
            session_key=session_key,
            platform=parts[0] if parts else platform,
            user_id=parts[1] if len(parts) > 1 else "",
            chat_id=parts[2] if len(parts) > 2 else "",
            display_name=sender.display_name if sender else "unknown",
            role=sender.role if sender else "operator",
            tenant_id="default",
            permission_level=sender.permission_level if sender else 1,
        )


class SessionManager:
    """Manages active sessions with optional NATS KV persistence.

    Supports multi-tenancy via hierarchical session keys.

    Usage with NATS:
        nats = NatsLayer()
        await nats.connect()
        sm = SessionManager(nats=nats)
        session = await sm.get_or_create("default:telegram:123:456", sender, "telegram")
    """

    def __init__(self, nats=None, tenant_mgr=None):
        self._sessions: Dict[str, Session] = {}
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # 5 minutes
        self._nats = nats  # Optional NatsLayer for persistence
        self._tenant = tenant_mgr  # Optional TenantManager

    # ── CRUD ──────────────────────────────────────────────────

    def get(self, session_key: str) -> Optional[Session]:
        """Get session from memory (with TTL cleanup)."""
        self._maybe_cleanup()
        session = self._sessions.get(session_key)
        if session and session.is_expired:
            del self._sessions[session_key]
            return None
        return session

    async def get_or_create_async(self, session_key: str, sender=None, platform: str = "") -> Session:
        """Get or create a session, trying NATS KV first."""
        # Try NATS KV first (persisted sessions survive restarts)
        if self._nats and self._nats.is_connected:
            try:
                data = await self._nats.session_get(session_key)
                if data:
                    session = Session.from_dict(data)
                    if not session.is_expired:
                        self._sessions[session_key] = session
                        session.touch()
                        self._persist_session(session_key, session)
                        return session
            except Exception:
                log.debug("NATS session_get failed for %s", session_key)

        # Fall back to in-memory
        return self.get_or_create(session_key, sender, platform)

    def get_or_create(self, session_key: str, sender=None, platform: str = "") -> Session:
        """Get or create a session (in-memory only)."""
        session = self.get(session_key)
        if session is None:
            session = Session.from_sender(session_key, sender, platform)
            self._sessions[session_key] = session
            log.info("Session created: %s (%s)", session_key, session.display_name)
            self._persist_session(session_key, session)
        else:
            session.touch()
            self._persist_session(session_key, session)
        return session

    def count_active(self) -> int:
        self._maybe_cleanup()
        return len(self._sessions)

    def list_tenant(self, tenant_id: str) -> list:
        """List all sessions for a specific tenant."""
        self._maybe_cleanup()
        prefix = f"{tenant_id}:" if tenant_id else ""
        return [s for k, s in self._sessions.items() if k.startswith(prefix)]

    def set_goal(self, session_key: str, goal: str, task_id: str = ""):
        session = self.get(session_key)
        if session:
            session.current_goal = goal
            session.goal_task_id = task_id
            session.touch()
            self._persist_session(session_key, session)

    def expire(self, session_key: str):
        if session_key in self._sessions:
            del self._sessions[session_key]
            log.info("Session expired: %s", session_key)
        # Also remove from NATS KV
        if self._nats and self._nats.is_connected:
            try:
                asyncio.create_task(self._nats.session_delete(session_key))
            except Exception:
                log.debug("NATS session_delete failed for %s", session_key)

    # ── Distributed Lock ──────────────────────────────────────

    async def acquire_lock(self, session_key: str, ttl: int = 60) -> bool:
        """Acquire a distributed write lock for session state modification."""
        if self._nats and self._nats.is_connected:
            return await self._nats.session_lock(session_key, ttl)
        return True  # No NATS, no lock needed

    async def release_lock(self, session_key: str):
        """Release the distributed write lock."""
        if self._nats and self._nats.is_connected:
            await self._nats.session_unlock(session_key)

    # ── Internal ──────────────────────────────────────────────

    def _persist_session(self, session_key: str, session: Session):
        """Persist session to NATS KV if available."""
        if self._nats and self._nats.is_connected:
            try:
                asyncio.create_task(
                    self._nats.session_put(session_key, session.to_dict())
                )
            except Exception as e:
                log.debug("NATS session persist failed: %s", e)

    def _maybe_cleanup(self):
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        expired = [k for k, s in self._sessions.items() if s.is_expired]
        for k in expired:
            del self._sessions[k]
        if expired:
            log.info("Cleaned up %d expired sessions", len(expired))
