"""ww/contacts/roster.py — Contact List with SQLite Persistence

The roster is the agent's address book. It stores:
- DID + friend code (identity)
- Assigned permission level
- Human-friendly alias
- Connection info (last IP, port, relay)
- Trust state (handshake completed, pending, revoked)
- Metadata (last seen, notes)

Storage: single SQLite file for durability.
"""

from __future__ import annotations
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from .permissions import (
    PermissionLevel,
    LEVEL_LABELS,
    parse_level,
)

logger = logging.getLogger("ww.contacts.roster")

ROSTER_DIR = os.environ.get(
    "WW_CONTACTS_DIR",
    os.path.join(os.path.expanduser("~"), ".ww_data", "contacts"),
)
ROSTER_DB = os.path.join(ROSTER_DIR, "roster.db")

# ── Trust states ──

TRUST_PENDING = "pending"       # Invitation sent, awaiting acceptance
TRUST_ACTIVE = "active"         # Handshake complete, mutual trust
TRUST_BLOCKED = "blocked"       # Removed or rejected
TRUST_EXPIRED = "expired"       # Key rotated, needs re-handshake


class RosterEntry:
    """A single contact entry."""

    def __init__(
        self,
        did: str,
        friend_code: str = "",
        alias: str = "",
        level: PermissionLevel = PermissionLevel.CONTACT,
        trust_state: str = TRUST_PENDING,
        public_key_hex: str = "",
        last_seen_ip: str = "",
        last_seen_port: int = 0,
        relay_url: str = "",
        notes: str = "",
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
    ):
        self.did = did
        self.friend_code = friend_code or did[-8:]
        self.alias = alias or f"contact-{self.friend_code}"
        self.level = level
        self.trust_state = trust_state
        self.public_key_hex = public_key_hex
        self.last_seen_ip = last_seen_ip
        self.last_seen_port = last_seen_port
        self.relay_url = relay_url
        self.notes = notes
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()

    def to_dict(self) -> dict:
        return {
            "did": self.did,
            "friend_code": self.friend_code,
            "alias": self.alias,
            "level": int(self.level),
            "level_label": LEVEL_LABELS.get(self.level, "Unknown"),
            "trust_state": self.trust_state,
            "public_key_hex": self.public_key_hex,
            "last_seen_ip": self.last_seen_ip,
            "last_seen_port": self.last_seen_port,
            "relay_url": self.relay_url,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RosterEntry":
        return cls(
            did=data["did"],
            friend_code=data.get("friend_code", ""),
            alias=data.get("alias", ""),
            level=parse_level(data.get("level", 1)),
            trust_state=data.get("trust_state", TRUST_PENDING),
            public_key_hex=data.get("public_key_hex", ""),
            last_seen_ip=data.get("last_seen_ip", ""),
            last_seen_port=data.get("last_seen_port", 0),
            relay_url=data.get("relay_url", ""),
            notes=data.get("notes", ""),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class Roster:
    """Persistent contact list backed by SQLite.

    Thread-safe (single writer via lock).
    """

    def __init__(self, db_path: str = ROSTER_DB):
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    # ── CRUD ──

    def add(self, entry: RosterEntry) -> bool:
        """Add a new contact. Returns False if DID already exists."""
        with self._lock:
            existing = self._get(entry.did)
            if existing:
                return False
            self._insert(entry)
            logger.info("Contact added: %s (%s)", entry.alias, entry.friend_code)
            return True

    def get(self, did: str) -> Optional[RosterEntry]:
        """Get a contact by DID."""
        with self._lock:
            return self._get(did)

    def get_by_friend_code(self, code: str) -> Optional[RosterEntry]:
        """Find a contact by friend code."""
        with self._lock:
            row = self._fetchone(
                "SELECT data FROM contacts WHERE friend_code = ?", (code,)
            )
            if row:
                return RosterEntry.from_dict(json.loads(row[0]))
            return None

    def update(self, entry: RosterEntry) -> bool:
        """Update a contact. Returns False if not found."""
        with self._lock:
            existing = self._get(entry.did)
            if not existing:
                return False
            entry.updated_at = time.time()
            self._update(entry)
            return True

    def remove(self, did: str) -> bool:
        """Remove a contact by DID."""
        with self._lock:
            cursor = self._execute(
                "DELETE FROM contacts WHERE did = ?", (did,)
            )
            removed = cursor.rowcount > 0
            if removed:
                logger.info("Contact removed: %s", did[:16])
            return removed

    def list_active(self) -> List[RosterEntry]:
        """List all active contacts (not blocked/expired)."""
        with self._lock:
            all_rows = self._fetchall(
                "SELECT data FROM contacts"
            )
            result = []
            for r in all_rows:
                d = json.loads(r[0])
                state = d.get("trust_state", TRUST_PENDING)
                if state in (TRUST_PENDING, TRUST_ACTIVE):
                    result.append(RosterEntry.from_dict(d))
            return result

    def list_all(self) -> List[RosterEntry]:
        """List ALL contacts including blocked/expired."""
        with self._lock:
            rows = self._fetchall(
                "SELECT data FROM contacts ORDER BY updated_at DESC"
            )
            return [RosterEntry.from_dict(json.loads(r[0])) for r in rows]

    def count(self) -> Dict[str, int]:
        """Count contacts by trust state."""
        with self._lock:
            rows = []
            all_rows = self._fetchall(
                "SELECT data FROM contacts"
            )
            for r in all_rows:
                d = json.loads(r[0])
                state = d.get("trust_state", "pending")
                rows.append((state,))
            counts = {"active": 0, "pending": 0, "blocked": 0, "expired": 0, "total": 0}
            for state, in rows:
                counts[state] = counts.get(state, 0) + 1
                counts["total"] += 1
            return counts

    def search(self, query: str) -> List[RosterEntry]:
        """Search contacts by alias, friend_code, or DID."""
        with self._lock:
            pattern = f"%{query}%"
            rows = self._fetchall(
                """SELECT data FROM contacts
                   WHERE alias LIKE ? OR friend_code LIKE ? OR did LIKE ?
                   ORDER BY updated_at DESC LIMIT 20""",
                (pattern, pattern, pattern),
            )
            return [RosterEntry.from_dict(json.loads(r[0])) for r in rows]

    def update_last_seen(self, did: str, ip: str = "", port: int = 0):
        """Touch last_seen timestamp and optionally update address."""
        with self._lock:
            entry = self._get(did)
            if entry:
                entry.last_seen_ip = ip or entry.last_seen_ip
                entry.last_seen_port = port or entry.last_seen_port
                entry.updated_at = time.time()
                self._update(entry)

    # ── Internal DB methods ──

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS contacts (
                    did TEXT PRIMARY KEY,
                    friend_code TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_roster_friend_code ON contacts(friend_code)"
            )
            conn.commit()
        finally:
            conn.close()

    def _get(self, did: str) -> Optional[RosterEntry]:
        row = self._fetchone(
            "SELECT data FROM contacts WHERE did = ?", (did,)
        )
        return RosterEntry.from_dict(json.loads(row[0])) if row else None

    def _insert(self, entry: RosterEntry):
        self._execute(
            "INSERT OR IGNORE INTO contacts (did, friend_code, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                entry.did,
                entry.friend_code,
                json.dumps(entry.to_dict()),
                entry.created_at or time.time(),
                entry.updated_at or time.time(),
            ),
        )

    def _update(self, entry: RosterEntry):
        self._execute(
            "UPDATE contacts SET friend_code=?, data=?, updated_at=? WHERE did=?",
            (
                entry.friend_code,
                json.dumps(entry.to_dict()),
                entry.updated_at or time.time(),
                entry.did,
            ),
        )

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor
        finally:
            conn.close()

    def _fetchone(self, sql: str, params=()) -> Optional[tuple]:
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _fetchall(self, sql: str, params=()) -> List[tuple]:
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
