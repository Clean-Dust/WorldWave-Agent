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
- Single-user (default): local surfaces + owner Telegram share one primary entity
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from threading import Lock
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger("wavegate.identity")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
IDENTITY_DB = os.path.join(_WW_CFG, "identity.db")

# Local entry surfaces that should share the owner entity in single-user mode
LOCAL_PLATFORMS = frozenset({"http", "terminal", "cli", "api"})
LOCAL_USER_IDS = frozenset({"", "default", "local", "user", "owner"})

# Meta key for the node primary (owner) entity
META_PRIMARY_ENTITY = "primary_entity_id"


def _env_truthy(name: str, default: bool = False) -> bool:
    """Parse common env bool forms. Empty/unset uses default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = str(raw).strip().lower()
    if val in ("",):
        return False
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    return True


def is_single_user_mode() -> bool:
    """Whether this node should share one primary entity across local surfaces.

    WW_SINGLE_USER defaults true for typical personal installs.
    Explicit multi-tenant mode turns single-user off unless WW_SINGLE_USER=1.
    """
    raw = os.environ.get("WW_SINGLE_USER")
    if raw is not None and str(raw).strip() != "":
        return _env_truthy("WW_SINGLE_USER", default=True)
    # Explicit multi-tenant → strict per-platform resolve
    if _env_truthy("WW_MULTI_TENANT", default=False):
        return False
    return True


def configured_owner_telegram_ids() -> Set[str]:
    """Owner Telegram user ids from env (explicit config only)."""
    ids: Set[str] = set()
    owner = os.environ.get("WW_OWNER_TELEGRAM_ID", "").strip()
    if owner:
        ids.add(owner)
    # Positive DM user id in workspace (groups are negative chat ids)
    ws = os.environ.get("TELEGRAM_WW_WORKSPACE", "").strip()
    if ws and re.fullmatch(r"[1-9]\d*", ws):
        ids.add(ws)
    return ids


class IdentityResolver:
    """Unified identity layer across all messaging platforms.

    Maps: (platform, user_id, chat_id) → entity_id
    All platforms for the same human share one entity_id.

    Usage:
        resolver = IdentityResolver()
        entity_id = resolver.resolve("telegram", "123456789", "-1003841986648")
        # → "ent_a1b2c3d4"

        # Later, same user from terminal (single-user mode):
        entity_id = resolver.resolve("terminal", "default", "")
        # → "ent_a1b2c3d4"  (same primary entity)
    """

    def __init__(self, db_path: str = ""):
        # Resolve path at init so WW_CONFIG changes after import are honored
        if db_path:
            self._db_path = db_path
        else:
            cfg = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
            self._db_path = os.path.join(cfg, "identity.db")
        self._lock = Lock()
        self._cache: Dict[Tuple[str, str, str], str] = {}  # (platform, user_id, chat_id) → entity_id
        self._entity_cache: Dict[str, dict] = {}  # entity_id → {display_name, created_at, ...}
        self._init_db()

    # ── Database ─────────────────────────────────────────────────

    def _init_db(self):
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()

    # ── Mode helpers ─────────────────────────────────────────────

    @property
    def single_user(self) -> bool:
        return is_single_user_mode()

    @staticmethod
    def is_local_surface(platform: str, user_id: str = "") -> bool:
        """True for terminal/http (and aliases) with default/empty user."""
        p = (platform or "").strip().lower()
        u = str(user_id or "").strip().lower()
        return p in LOCAL_PLATFORMS and u in LOCAL_USER_IDS

    def owner_telegram_ids(self) -> Set[str]:
        """Owner telegram ids: env + single whitelist entry (if any)."""
        ids = set(configured_owner_telegram_ids())
        # If exactly one approved Telegram DM, treat as owner
        try:
            wl = self._telegram_whitelist_user_ids()
            if len(wl) == 1:
                ids.add(next(iter(wl)))
        except Exception:
            pass
        return ids

    def is_owner_telegram(self, user_id: str) -> bool:
        """Whether this Telegram user_id is the node owner.

        Explicit config (WW_OWNER_TELEGRAM_ID / positive TELEGRAM_WW_WORKSPACE /
        sole whitelist entry) always wins.

        When no owner is configured, the first Telegram user ever linked on
        this node is treated as the implicit owner so single-user installs
        still share a timeline. Additional Telegram users stay separate.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return False
        owners = self.owner_telegram_ids()
        if owners:
            return uid in owners
        first = self._first_telegram_user_id()
        if first is None:
            return True  # will become the first / implicit owner
        return first == uid

    def _telegram_whitelist_user_ids(self) -> Set[str]:
        """Read approved Telegram user ids from pairing store (best-effort)."""
        store = os.environ.get("WW_PAIRING_STORE", "") or os.path.join(
            os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww")), "pairing.json"
        )
        if not os.path.isfile(store):
            return set()
        with open(store, "r", encoding="utf-8") as f:
            data = json.load(f)
        out: Set[str] = set()
        whitelist = data.get("whitelist") or {}
        if isinstance(whitelist, dict):
            for entry in whitelist.values():
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("platform", "")).lower() != "telegram":
                    continue
                uid = str(entry.get("user_id", "")).strip()
                if uid:
                    out.add(uid)
        return out

    def _first_telegram_user_id(self) -> Optional[str]:
        """Earliest Telegram user_id linked in this DB (implicit owner)."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT user_id FROM platform_links "
                "WHERE platform = 'telegram' "
                "ORDER BY linked_at ASC LIMIT 1"
            ).fetchone()
        return str(row[0]) if row else None

    # ── Primary entity ───────────────────────────────────────────

    def get_primary_entity_id(self) -> Optional[str]:
        """Return the node primary (owner) entity_id, if set."""
        with self._lock:
            return self._get_primary_unlocked()

    def set_primary_entity_id(self, entity_id: str) -> None:
        """Persist the node primary entity_id."""
        if not entity_id:
            return
        with self._lock:
            self._set_primary_unlocked(entity_id)

    def _get_primary_unlocked(self) -> Optional[str]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (META_PRIMARY_ENTITY,),
            ).fetchone()
        if row and row[0]:
            return str(row[0])
        # Bootstrap from existing data (upgrades with pre-primary DBs)
        boot = self._bootstrap_primary_unlocked()
        return boot

    def _set_primary_unlocked(self, entity_id: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (META_PRIMARY_ENTITY, entity_id),
            )
            conn.commit()
        log.info("Primary entity set: %s", entity_id)

    def _bootstrap_primary_unlocked(self) -> Optional[str]:
        """Pick a sensible primary from existing links when meta is empty."""
        with sqlite3.connect(self._db_path) as conn:
            # Prefer local surface entity
            for platform in ("http", "terminal", "cli", "api"):
                row = conn.execute(
                    "SELECT entity_id FROM platform_links "
                    "WHERE platform = ? AND user_id IN ('default', 'local', 'user', '') "
                    "ORDER BY linked_at ASC LIMIT 1",
                    (platform,),
                ).fetchone()
                if row:
                    self._set_primary_unlocked(row[0])
                    return row[0]
            # Prefer configured owner telegram
            owners = configured_owner_telegram_ids()
            if owners:
                for oid in owners:
                    row = conn.execute(
                        "SELECT entity_id FROM platform_links "
                        "WHERE platform = 'telegram' AND user_id = ? "
                        "ORDER BY linked_at ASC LIMIT 1",
                        (oid,),
                    ).fetchone()
                    if row:
                        self._set_primary_unlocked(row[0])
                        return row[0]
            # Oldest entity overall
            row = conn.execute(
                "SELECT entity_id FROM entities ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row:
                self._set_primary_unlocked(row[0])
                return row[0]
        return None

    def _ensure_primary(self, entity_id: str) -> str:
        """If no primary yet, set this entity as primary. Return primary id."""
        primary = self._get_primary_unlocked()
        if primary:
            return primary
        self._set_primary_unlocked(entity_id)
        return entity_id

    def _link_local_defaults(self, entity_id: str) -> None:
        """Ensure http/default and terminal/default point at entity_id."""
        for platform in ("http", "terminal"):
            self._link(platform, "default", "", entity_id)
            self._cache[(platform, "default", "")] = entity_id

    def _relink_user_to_entity(
        self, platform: str, user_id: str, entity_id: str, chat_id: str = ""
    ) -> None:
        """Point all links for platform+user_id at entity_id (and ensure chat_id)."""
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE platform_links SET entity_id = ?, linked_at = ? "
                "WHERE platform = ? AND user_id = ?",
                (entity_id, now, platform, user_id),
            )
            # Ensure the specific (platform, user_id, chat_id) row exists
            conn.execute(
                "INSERT OR REPLACE INTO platform_links "
                "(platform, user_id, chat_id, entity_id, linked_at, is_primary) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (platform, user_id, chat_id, entity_id, now),
            )
            conn.commit()
        # Drop stale cache entries for this user
        stale = [k for k in self._cache if k[0] == platform and k[1] == user_id]
        for k in stale:
            del self._cache[k]
        self._cache[(platform, user_id, chat_id)] = entity_id

    # ── Resolution ───────────────────────────────────────────────

    def resolve(
        self,
        platform: str,
        user_id: str,
        chat_id: str = "",
        display_name: str = "",
    ) -> str:
        """Resolve a platform identity to a unified entity_id.

        If the platform identity is new, auto-creates an entity.
        In single-user mode, local surfaces and the owner Telegram user
        share the primary entity so working memory continues across entries.
        """
        platform = (platform or "http").strip().lower()
        user_id = str(user_id if user_id is not None else "default").strip() or "default"
        chat_id = str(chat_id or "")

        if not self.single_user:
            return self._resolve_strict(platform, user_id, chat_id, display_name)

        with self._lock:
            return self._resolve_single_user(platform, user_id, chat_id, display_name)

    def resolve_local(
        self,
        platform: str = "http",
        user_id: str = "default",
        display_name: str = "User",
    ) -> str:
        """Resolve a local surface (http/terminal) to the primary entity."""
        p = (platform or "http").strip().lower()
        if p not in LOCAL_PLATFORMS:
            p = "http"
        uid = str(user_id or "default").strip() or "default"
        return self.resolve(p, uid, "", display_name=display_name)

    def ensure_owner_link(
        self,
        platform: str,
        user_id: str,
        chat_id: str = "",
        entity_id: str = "",
        display_name: str = "",
    ) -> str:
        """After a resolve, ensure owner Telegram is linked to primary.

        Safe to call for any platform; no-op when not single-user or not owner.
        Returns the entity_id that should be used for the task.
        """
        if not self.single_user:
            return entity_id or self.resolve(platform, user_id, chat_id, display_name)

        platform = (platform or "").strip().lower()
        user_id = str(user_id or "").strip()
        chat_id = str(chat_id or "")

        if self.is_local_surface(platform, user_id):
            return self.resolve(platform, user_id, chat_id, display_name)

        if platform == "telegram" and self.is_owner_telegram(user_id):
            return self.resolve(platform, user_id, chat_id, display_name)

        return entity_id or self.resolve(platform, user_id, chat_id, display_name)

    def _resolve_single_user(
        self,
        platform: str,
        user_id: str,
        chat_id: str,
        display_name: str,
    ) -> str:
        """Single-user resolve: local + owner Telegram share primary."""
        cache_key = (platform, user_id, chat_id)

        # ── Local surfaces → always primary ──
        if self.is_local_surface(platform, user_id):
            primary = self._get_primary_unlocked()
            if primary:
                existing = self._lookup(platform, user_id, chat_id) or self._lookup_by_user(
                    platform, user_id
                )
                if existing != primary:
                    self._link(platform, user_id, chat_id, primary)
                self._cache[cache_key] = primary
                self._touch(primary)
                return primary

            # No primary yet — create or promote existing local link
            entity_id = self._lookup(platform, user_id, chat_id) or self._lookup_by_user(
                platform, user_id
            )
            if not entity_id:
                entity_id = self._create_entity(display_name)
                self._link(platform, user_id, chat_id, entity_id)
            self._set_primary_unlocked(entity_id)
            self._link_local_defaults(entity_id)
            self._cache[cache_key] = entity_id
            self._touch(entity_id)
            log.info(
                "Identity resolved (local/primary): %s:%s → %s",
                platform,
                user_id,
                entity_id,
            )
            return entity_id

        # ── Owner Telegram → share primary ──
        if platform == "telegram" and self.is_owner_telegram(user_id):
            primary = self._get_primary_unlocked()
            if primary:
                existing = self._lookup(platform, user_id, chat_id) or self._lookup_by_user(
                    platform, user_id
                )
                if existing != primary:
                    self._relink_user_to_entity(platform, user_id, primary, chat_id)
                    log.info(
                        "Owner Telegram linked to primary: %s → %s (was %s)",
                        user_id,
                        primary,
                        existing or "new",
                    )
                else:
                    self._link(platform, user_id, chat_id, primary)
                self._cache[cache_key] = primary
                self._touch(primary)
                return primary

            # Telegram first — this entity becomes primary; link local defaults
            entity_id = self._lookup(platform, user_id, chat_id) or self._lookup_by_user(
                platform, user_id
            )
            if not entity_id:
                entity_id = self._create_entity(display_name)
            self._link(platform, user_id, chat_id, entity_id)
            self._set_primary_unlocked(entity_id)
            self._link_local_defaults(entity_id)
            self._cache[cache_key] = entity_id
            self._touch(entity_id)
            log.info(
                "Identity resolved (telegram/primary): %s → %s",
                user_id,
                entity_id,
            )
            return entity_id

        # ── Non-owner / other platforms — strict per-user ──
        return self._resolve_strict_unlocked(platform, user_id, chat_id, display_name)

    def _resolve_strict(
        self,
        platform: str,
        user_id: str,
        chat_id: str,
        display_name: str,
    ) -> str:
        with self._lock:
            return self._resolve_strict_unlocked(platform, user_id, chat_id, display_name)

    def _resolve_strict_unlocked(
        self,
        platform: str,
        user_id: str,
        chat_id: str,
        display_name: str,
    ) -> str:
        """Classic resolve: no cross-platform auto-merge."""
        cache_key = (platform, user_id, chat_id)
        if cache_key in self._cache:
            entity_id = self._cache[cache_key]
            self._touch(entity_id)
            return entity_id

        entity_id = self._lookup(platform, user_id, chat_id)
        if entity_id:
            self._cache[cache_key] = entity_id
            self._touch(entity_id)
            return entity_id

        # Same user_id on this platform (different chat) → same entity
        entity_id = self._lookup_by_user(platform, user_id)
        created = False
        if not entity_id:
            entity_id = self._create_entity(display_name)
            created = True

        self._link(platform, user_id, chat_id, entity_id)
        self._cache[cache_key] = entity_id
        # Non-owner / foreign platforms do not claim primary — only local
        # surfaces and owner Telegram establish the primary entity.
        log.info(
            "Identity resolved: %s:%s:%s → %s (new=%s)",
            platform,
            user_id,
            chat_id[:10] if chat_id else "",
            entity_id,
            "created" if created else "linked",
        )
        return entity_id

    def link(self, entity_id: str, platform: str, user_id: str, chat_id: str = ""):
        """Explicitly link a platform identity to an existing entity."""
        platform = (platform or "").strip().lower()
        user_id = str(user_id)
        chat_id = str(chat_id or "")
        with self._lock:
            self._link(platform, user_id, chat_id, entity_id)
            cache_key = (platform, user_id, chat_id)
            self._cache[cache_key] = entity_id
            # Explicit link of local/owner can establish primary
            if self.single_user and not self._get_primary_unlocked():
                self._set_primary_unlocked(entity_id)
            log.info(
                "Explicitly linked %s:%s:%s → %s", platform, user_id, chat_id, entity_id
            )

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
                (entity_id,),
            ).fetchall()
        return [
            {
                "platform": r[0],
                "user_id": r[1],
                "chat_id": r[2],
                "linked_at": r[3],
                "is_primary": bool(r[4]),
            }
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
            {
                "entity_id": r[0],
                "display_name": r[1],
                "created_at": r[2],
                "last_active": r[3],
                "total_interactions": r[4],
            }
            for r in rows
        ]

    def set_display_name(self, entity_id: str, name: str):
        """Update entity display name."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "UPDATE entities SET display_name = ? WHERE entity_id = ?",
                    (name, entity_id),
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
                (platform, user_id, chat_id),
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
                (platform, user_id),
            ).fetchone()
        return row[0] if row else None

    def _create_entity(self, display_name: str = "") -> str:
        entity_id = f"ent_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO entities (entity_id, display_name, created_at, last_active, total_interactions) "
                "VALUES (?, ?, ?, ?, 0)",
                (entity_id, display_name or entity_id[:16], now, now),
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
                (platform, user_id, chat_id, entity_id, now),
            )
            conn.commit()

    def _touch(self, entity_id: str):
        """Update last_active timestamp."""
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE entities SET last_active = ?, total_interactions = total_interactions + 1 "
                "WHERE entity_id = ?",
                (now, entity_id),
            )
            conn.commit()

    def _load_entity(self, entity_id: str) -> Optional[dict]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT entity_id, display_name, created_at, last_active, total_interactions "
                "FROM entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
        if row:
            data = {
                "entity_id": row[0],
                "display_name": row[1],
                "created_at": row[2],
                "last_active": row[3],
                "total_interactions": row[4],
            }
            self._entity_cache[entity_id] = data
            return data
        return None
