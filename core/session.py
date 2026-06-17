"""ww/core/session.py — Worldwave Session management v0.1

Manage conversation session persistence, break recovery, context retention.

feature:
  - Session create/save/load (JSON + SQLite)
  - Break auto-recovery to checkpoint
  - Context retention and search
  - Session cleanup (TTL expired delete)
"""

from __future__ import annotations
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


SESSION_DB = os.path.expanduser("~/.ww/sessions.db")
SESSION_TTL_DAYS = 7  # Sessions older than this get auto-cleaned

log = logging.getLogger(__name__)


class SessionManager:
    """Session management: persistence + recovery + cleanup."""

    def __init__(self, db_path: str = SESSION_DB):
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        self._cleanup_old()

    def _init_db(self):
        """Init SQLite schema."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    goal TEXT,
                    status TEXT DEFAULT 'active',
                    metadata TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    spiral_number INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                    ON checkpoints(session_id);
                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_updated
                    ON sessions(updated_at);
            """)

    # ── Session Lifecycle ────────────────────────────

    def create(self, goal: str = "", metadata: Dict = None) -> str:
        """Create a new session. Returns session ID."""
        session_id = f"session_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at, goal, metadata) VALUES (?, ?, ?, ?, ?)",
                (session_id, now, now, goal, json.dumps(metadata or {})),
            )
        return session_id

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session info."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT id, created_at, updated_at, goal, status, metadata FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "created_at": row[1],
            "updated_at": row[2],
            "goal": row[3],
            "status": row[4],
            "metadata": json.loads(row[5]),
        }

    def update_status(self, session_id: str, status: str, goal: str = ""):
        """Update session status."""
        now = time.time()
        with self._lock, sqlite3.connect(self._db_path) as conn:
            if goal:
                conn.execute(
                    "UPDATE sessions SET status = ?, goal = ?, updated_at = ? WHERE id = ?",
                    (status, goal, now, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, session_id),
                )

    def delete(self, session_id: str):
        """Delete a session and all its data."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def list_active(self, limit: int = 20) -> List[Dict[str, Any]]:
        """List active sessions, newest first."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, created_at, updated_at, goal, status FROM sessions "
                "WHERE status = 'active' ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "updated_at": r[2],
                "goal": r[3],
                "status": r[4],
            }
            for r in rows
        ]

    # ── Messages ────────────────────────────────────

    def add_message(self, session_id: str, role: str, content: str):
        """Add a message to a session."""
        now = time.time()
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def get_messages(self, session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get session messages, oldest first."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3]}
            for r in rows
        ]

    # ── Checkpoints ────────────────────────────────

    def save_checkpoint(self, session_id: str, spiral_number: int,
                        phase: str, data: Dict[str, Any]) -> str:
        """Save a checkpoint. Returns checkpoint ID."""
        ckpt_id = f"ckpt_{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO checkpoints (id, session_id, spiral_number, phase, data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ckpt_id, session_id, spiral_number, phase, json.dumps(data), now),
            )
        return ckpt_id

    def get_latest_checkpoint(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent checkpoint for a session."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT id, spiral_number, phase, data, created_at FROM checkpoints "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "spiral_number": row[1],
            "phase": row[2],
            "data": json.loads(row[3]),
            "created_at": row[4],
        }

    def get_checkpoints(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all checkpoints for a session."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, spiral_number, phase, data, created_at FROM checkpoints "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            {
                "id": r[0],
                "spiral_number": r[1],
                "phase": r[2],
                "data": json.loads(r[3]),
                "created_at": r[4],
            }
            for r in rows
        ]

    # ── Search ──────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search across sessions by message content."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT m.id, m.session_id, m.role, m.content, m.created_at, s.goal "
                "FROM messages m JOIN sessions s ON m.session_id = s.id "
                "WHERE m.content LIKE ? ORDER BY m.created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [
            {
                "message_id": r[0],
                "session_id": r[1],
                "role": r[2],
                "content": r[3][:500],
                "created_at": r[4],
                "session_goal": r[5],
            }
            for r in rows
        ]

    # ── Cleanup ─────────────────────────────────────

    def _cleanup_old(self):
        """Delete sessions older than TTL."""
        cutoff = time.time() - (SESSION_TTL_DAYS * 86400)
        try:
            with self._lock, sqlite3.connect(self._db_path) as conn:
                old = conn.execute(
                    "SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)
                ).fetchall()
                for (sid,) in old:
                    conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (sid,))
                    conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        except Exception as e:
            log.debug("SQLite cleanup error: %s", e)

    def cleanup(self):
        """Manual cleanup trigger."""
        self._cleanup_old()


def default_session_manager() -> SessionManager:
    return SessionManager()
