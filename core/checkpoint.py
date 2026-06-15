"""
ww/core/checkpoint.py — Deep Checkpoint persistence v0.1

SQLite supports breakpoint resume system.

Why deep checkpoint:
1. System crash/power outage can precisely recover to step N
2. User can manually pause long task, resume seamlessly tomorrow
3. Supports multiple sessions in parallel (same DB different session_id)

each checkpoint contains :
- when   inferencecontext (scratchpad) 
- Task progress tree (completed steps, partial results)
- Tool call history (with input/output)
- Spiral state snapshot
"""

from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.diff import DiffEngine, get_diff_engine


# ── Schema ──

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    goal TEXT,
    created_at TEXT,
    updated_at TEXT,
    status TEXT DEFAULT 'running',
    spirals_completed INTEGER DEFAULT 0,
    total_steps INTEGER DEFAULT 0,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    spiral_number INTEGER NOT NULL,
    phase TEXT NOT NULL,
    step_number INTEGER DEFAULT 0,
    step_total INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    scratchpad TEXT DEFAULT '',
    tool_history TEXT DEFAULT '[]',
    plan_tree TEXT DEFAULT '{}',
    partial_results TEXT DEFAULT '{}',
    context_snapshot TEXT DEFAULT '{}',
    is_interrupted INTEGER DEFAULT 0,
    interrupt_reason TEXT DEFAULT '',
    resume_data TEXT DEFAULT '{}',
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_session 
    ON checkpoints(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_interrupted
    ON checkpoints(is_interrupted, session_id);
"""


class CheckpointDB:
    """SQLite-backed checkpoint storage.
    
    Uses WAL mode + thread-safe writes for crash resilience.
    Auto-snapshots files via DiffEngine before edits for visual diff + rollback.
    """

    def __init__(self, db_path: str = ""):
        if not db_path:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "data", "checkpoints.db"
            )
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._diff = get_diff_engine()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
            conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ── Session management ──

    def create_session(self, goal: str, session_id: str = "") -> str:
        """Create new session, return session_id."""
        sid = session_id or uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, goal, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (sid, goal, now, now),
            )
            conn.commit()
            conn.close()
        return sid

    def update_session(self, session_id: str, **kwargs):
        """update session metadata. """
        fields = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(session_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"UPDATE sessions SET {fields}, updated_at=? WHERE session_id=?",
                (*vals, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            conn.close()

    def get_session(self, session_id: str) -> Optional[Dict]:
        """Get session info."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        conn.close()
        if row:
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata", "{}"))
            return d
        return None

    def list_sessions(self, limit: int = 20, status: str = "") -> List[Dict]:
        """List recent sessions."""
        conn = self._conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Checkpoint management ──

    def save_checkpoint(
        self,
        session_id: str,
        spiral_number: int,
        phase: str,
        step_number: int = 0,
        step_total: int = 0,
        scratchpad: str = "",
        tool_history: List[Dict] = None,
        plan_tree: Dict = None,
        partial_results: Dict = None,
        context_snapshot: Dict = None,
        interrupted: bool = False,
        interrupt_reason: str = "",
        resume_data: Dict = None,
    ) -> str:
        """Save a checkpoint, return ID."""
        cp_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            conn.execute(
                """INSERT INTO checkpoints 
                (id, session_id, spiral_number, phase, step_number, step_total,
                 created_at, scratchpad, tool_history, plan_tree, partial_results,
                 context_snapshot, is_interrupted, interrupt_reason, resume_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cp_id,
                    session_id,
                    spiral_number,
                    phase,
                    step_number,
                    step_total,
                    now,
                    scratchpad,
                    json.dumps(tool_history or [], ensure_ascii=False),
                    json.dumps(plan_tree or {}, ensure_ascii=False),
                    json.dumps(partial_results or {}, ensure_ascii=False),
                    json.dumps(context_snapshot or {}, ensure_ascii=False),
                    1 if interrupted else 0,
                    interrupt_reason,
                    json.dumps(resume_data or {}, ensure_ascii=False),
                ),
            )
            # Same as update session state
            conn.execute(
                "UPDATE sessions SET updated_at=?, spirals_completed=MAX(spirals_completed, ?) WHERE session_id=?",
                (now, spiral_number, session_id),
            )
            conn.commit()
            conn.close()
        return cp_id

    def get_checkpoint(self, cp_id: str) -> Optional[Dict]:
        """Get specific checkpoint."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE id=?", (cp_id,)
        ).fetchone()
        conn.close()
        if row:
            return self._deserialize_row(row)
        return None

    def get_last_checkpoint(self, session_id: str) -> Optional[Dict]:
        """Get session's latest checkpoint."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        conn.close()
        if row:
            return self._deserialize_row(row)
        return None

    def get_last_interrupted(self, session_id: str) -> Optional[Dict]:
        """Get session's latest breakpoint (if any)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id=? AND is_interrupted=1 ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        conn.close()
        if row:
            return self._deserialize_row(row)
        return None

    def get_checkpoints(self, session_id: str, limit: int = 50) -> List[Dict]:
        """Get session's checkpoint history."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        conn.close()
        return [self._deserialize_row(r) for r in rows]

    def mark_resolved(self, cp_id: str):
        """Clear break marker (for recovery)."""
        with self._lock:
            conn = self._conn()
            conn.execute(
                "UPDATE checkpoints SET is_interrupted=0 WHERE id=?", (cp_id,)
            )
            conn.commit()
            conn.close()

    def get_checkpoint_by_spiral(self, session_id: str, spiral: int, phase: str) -> Optional[Dict]:
        """Get specific spiral+phase checkpoint (precise recovery position)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id=? AND spiral_number=? AND phase=? ORDER BY created_at DESC LIMIT 1",
            (session_id, spiral, phase),
        ).fetchone()
        conn.close()
        if row:
            return self._deserialize_row(row)
        return None

    # ── Cleanup ──

    def delete_session(self, session_id: str):
        """delete session  and all  checkpoint. """
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            conn.commit()
            conn.close()

    def cleanup_old(self, max_age_days: int = 30):
        """Clean checkpoints older than specified days."""
        cutoff = datetime.now(timezone.utc).isoformat()
        # Calculate cutoff date
        import datetime as dt
        cutoff_dt = dt.datetime.now(timezone.utc) - dt.timedelta(days=max_age_days)
        cutoff = cutoff_dt.isoformat()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "DELETE FROM sessions WHERE updated_at < ? AND status != 'running'",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM checkpoints WHERE created_at < ?",
                (cutoff,),
            )
            conn.commit()
            conn.close()

    # ── Diff integration ─────────────────────────────────────────

    def snapshot_file(self, path: str) -> str:
        """Snapshot a file before editing. Returns content hash."""
        return self._diff.snapshot(path)

    def get_diff(self, path: str):
        """Get diff for a snapshotted file. Returns DiffResult or None."""
        return self._diff.diff(path)

    def get_all_diffs(self) -> list:
        """Get diffs for all snapshotted files."""
        return self._diff.diff_all()

    def preview_diff(self, path: str, new_content: str):
        """Preview what the diff would look like before actually writing."""
        if not self._diff.has_snapshot(path):
            self._diff.snapshot(path)
        return self._diff.diff(path, new_content=new_content)

    def commit_edit(self, path: str, new_content: str):
        """Record an edit after snapshot — generate and return the diff."""
        result = self._diff.diff(path, new_content=new_content)
        self._diff.clear_snapshot(path)
        return result

    # ── Helper ──

    def _deserialize_row(self, row) -> Dict:
        d = dict(row)
        for field in ("tool_history", "plan_tree", "partial_results", "context_snapshot", "resume_data"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        d["is_interrupted"] = bool(d.get("is_interrupted", 0))
        return d


# ── Get execution context snapshot ──

def build_context_snapshot(
    goal: str = "",
    spiral_number: int = 0,
    phase: str = "",
    steps_completed: int = 0,
    steps_total: int = 0,
    tool_history: List[Dict] = None,
    partial_results: Dict = None,
    extra: Dict = None,
) -> Dict:
    """Create standardized context snapshot."""
    return {
        "goal": goal,
        "spiral": spiral_number,
        "phase": phase,
        "progress": f"{steps_completed}/{steps_total}",
        "steps_completed": steps_completed,
        "steps_total": steps_total,
        "tool_calls": len(tool_history or []),
        "tool_history": (tool_history or [])[-20:],  # Keep only the last 20
        "partial_results": partial_results or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    }
