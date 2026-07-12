"""
wavegate/identity.py — Identity Resolver

Maps all platform-specific identities (Telegram user_id, terminal username,
HTTP API key, Feishu open_id, etc.) to a single, permanent entity_id.

This is the foundation of "no new session" — every platform sees the same
cognitive entity. The entity_id never changes for a given human user.

Design:
- SQLite-backed for persistence across server restarts
- Auto-creates entity on first contact from any platform
- Supports linking multiple platform IDs to the same entity
- Returns a single, stable entity_id for any platform identity
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from threading import Lock
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("wavegate.identity")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
IDENTITY_DB = os.path.join(_WW_CFG, "identity.db")


class IdentityResolver:
    """Unified identity layer across all messaging platforms.

    Maps: (platform, user_id, chat_id) → entity_id
    All platforms for the same human share one entity_id.

    Usage:
        resolver = IdentityResolver()
        entity_id = resolver.resolve("telegram", "123456789", "-1003841986648")
        # → "ent_a1b2c3d4"

        # Later, same user from terminal:
        entity_id = resolver.resolve("terminal", "chung", "")
        # → "ent_a1b2c3d4"  (if linked)
    """

    def __init__(self, db_path: str = ""):
        self._db_path = db_path or IDENTITY_DB
        self._lock = Lock()
        self._cache: Dict[Tuple[str, str, str], str] = {}  # (platform, user_id, chat_id) → entity_id
        self._entity_cache: Dict[str, dict] = {}  # entity_id → {display_name, created_at, ...}
        self._init_db()

    # ── Database ─────────────────────────────────────────────────

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    display_name TEXT DEFAULT 'unknown',
                    created_at REAL NOT NULL,
                    last_active REAL NOT NULL,
                    total_interactions INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS platform_links (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL DEFAULT '',
                    entity_id TEXT NOT NULL,
                    linked_at REAL NOT NULL,
                    is_primary INTEGER DEFAULT 0,
                    FOREIGN KEY (entity_id) REFERENCES entities(entity_id),
                    PRIMARY KEY (platform, user_id, chat_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_links_entity
                ON platform_links(entity_id)
            """)
            conn.commit()

    # ── Resolution ───────────────────────────────────────────────

    def resolve(self, platform: str, user_id: str, chat_id: str = "",
                display_name: str = "") -> str:
        """Resolve a platform identity to a unified entity_id.

        If the platform identity is new, auto-creates an entity.
        Returns the stable entity_id.
        """
        cache_key = (platform, str(user_id), str(chat_id))
        with self._lock:
            # Check cache
            if cache_key in self._cache:
                entity_id = self._cache[cache_key]
                self._touch(entity_id)
                return entity_id

            # Check database
            entity_id = self._lookup(platform, user_id, chat_id)
            if entity_id:
                self._cache[cache_key] = entity_id
                self._touch(entity_id)
                return entity_id

            # New entity — check if same user_id on this platform already exists
            # (e.g., same Telegram user in different chats should map to same entity)
            entity_id = self._lookup_by_user(platform, user_id)
            if not entity_id:
                # Brand new entity
                entity_id = self._create_entity(display_name)

            # Link this specific platform+chat to the entity
            self._link(platform, user_id, chat_id, entity_id)
            self._cache[cache_key] = entity_id
            log.info("Identity resolved: %s:%s:%s → %s (new=%s)",
                     platform, user_id, chat_id[:10], entity_id,
                     "created" if not self._lookup_by_user(platform, user_id) else "linked")
            return entity_id

    def link(self, entity_id: str, platform: str, user_id: str,
             chat_id: str = ""):
        """Explicitly link a platform identity to an existing entity."""
        with self._lock:
            self._link(platform, str(user_id), str(chat_id), entity_id)
            cache_key = (platform, str(user_id), str(chat_id))
            self._cache[cache_key] = entity_id
            log.info("Explicitly linked %s:%s:%s → %s", platform, user_id, chat_id, entity_id)

    def get_entity(self, entity_id: str) -> Optional[dict]:
        """Get entity metadata."""
        with self._lock:
            if entity_id in self._entity_cache:
                return self._entity_cache[entity_id]
            return self._load_entity(entity_id)

    def get_platform_ids(self, entity_id: str) -> List[dict]:
        """Get all platform identities linked to an entity."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT platform, user_id, chat_id, linked_at, is_primary "
                "FROM platform_links WHERE entity_id = ?",
                (entity_id,)
            ).fetchall()
        return [
            {"platform": r[0], "user_id": r[1], "chat_id": r[2],
             "linked_at": r[3], "is_primary": bool(r[4])}
            for r in rows
        ]

    def get_all_entities(self) -> List[dict]:
        """List all known entities."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT entity_id, display_name, created_at, last_active, total_interactions "
                "FROM entities ORDER BY last_active DESC"
            ).fetchall()
        return [
            {"entity_id": r[0], "display_name": r[1], "created_at": r[2],
             "last_active": r[3], "total_interactions": r[4]}
            for r in rows
        ]

    def set_display_name(self, entity_id: str, name: str):
        """Update entity display name."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "UPDATE entities SET display_name = ? WHERE entity_id = ?",
                    (name, entity_id)
                )
                conn.commit()
            if entity_id in self._entity_cache:
                self._entity_cache[entity_id]["display_name"] = name

    # ── Internal ─────────────────────────────────────────────────

    def _lookup(self, platform: str, user_id: str, chat_id: str) -> Optional[str]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT entity_id FROM platform_links "
                "WHERE platform = ? AND user_id = ? AND chat_id = ?",
                (platform, user_id, chat_id)
            ).fetchone()
        return row[0] if row else None

    def _lookup_by_user(self, platform: str, user_id: str) -> Optional[str]:
        """Find entity by platform+user_id (ignoring chat_id).

        This ensures the same Telegram user in DMs and groups maps to one entity.
        """
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT entity_id FROM platform_links "
                "WHERE platform = ? AND user_id = ? "
                "ORDER BY linked_at ASC LIMIT 1",
                (platform, user_id)
            ).fetchone()
        return row[0] if row else None

    def _create_entity(self, display_name: str = "") -> str:
        entity_id = f"ent_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO entities (entity_id, display_name, created_at, last_active, total_interactions) "
                "VALUES (?, ?, ?, ?, 0)",
                (entity_id, display_name or entity_id[:16], now, now)
            )
            conn.commit()
        self._entity_cache[entity_id] = {
            "entity_id": entity_id,
            "display_name": display_name or entity_id[:16],
            "created_at": now,
            "last_active": now,
            "total_interactions": 0,
        }
        log.info("Created new entity: %s", entity_id)
        return entity_id

    def _link(self, platform: str, user_id: str, chat_id: str, entity_id: str):
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO platform_links "
                "(platform, user_id, chat_id, entity_id, linked_at, is_primary) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (platform, user_id, chat_id, entity_id, now)
            )
            conn.commit()

    def _touch(self, entity_id: str):
        """Update last_active timestamp."""
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE entities SET last_active = ?, total_interactions = total_interactions + 1 "
                "WHERE entity_id = ?",
                (now, entity_id)
            )
            conn.commit()

    def _load_entity(self, entity_id: str) -> Optional[dict]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT entity_id, display_name, created_at, last_active, total_interactions "
                "FROM entities WHERE entity_id = ?",
                (entity_id,)
            ).fetchone()
        if row:
            data = {
                "entity_id": row[0], "display_name": row[1],
                "created_at": row[2], "last_active": row[3],
                "total_interactions": row[4],
            }
            self._entity_cache[entity_id] = data
            return data
        return None
