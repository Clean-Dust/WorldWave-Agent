"""
ww/core/memory/hippocampus.py — Short-term buffer (SQLite persistence)

Hippocampus is the memory system's short-term buffer:
- capacity limit (default 100 items)
- SQLite persistence (single .db file)
- capacity monitor: full load triggers Event → forced Consolidation
- FIFO eviction, with importance protection
"""

from __future__ import annotations
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from .atom import MemoryAtom

logger = logging.getLogger("ww.memory.hippocampus")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
MEMORY_DIR = os.path.join(_WW_CFG, "memory")


class CapacityEvent:
    """
    capacity event: Hippocampus full load broadcast.

    Listen callback signature:
        on_capacity(hippocampus, stats: dict) -> None

    stats contains : 
        count, capacity, protect_count, oldest_age,
        top_salience_atoms (recommend keeping 5 items)
    """

    def __init__(self):
        self._listeners: List[Callable] = []

    def connect(self, callback: Callable):
        self._listeners.append(callback)

    def disconnect(self, callback: Callable):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def emit(self, hippocampus, stats: dict):
        for cb in self._listeners:
            try:
                cb(hippocampus, stats)
            except Exception as e:
                logger.error(f"CapacityEvent listener error: {e}")


class Hippocampus:
    """
    Short-term memory buffer (hippocampus).

    Persistence: SQLite (auto-create .db)
    capacity limit: cap (default 100)
    Eviction strategy: FIFO + importance protection (protect_threshold)
    capacity event: full load auto emit CapacityEvent
    """

    def __init__(
        self,
        cap: int = 100,
        protect_threshold: float = 0.8,
        data_dir: str = "",
    ):
        self.cap = cap
        self.protect_threshold = protect_threshold
        self.data_dir = data_dir or MEMORY_DIR
        self._lock = threading.Lock()

        # ── capacity event ──
        self.on_capacity = CapacityEvent()
        self._sleep_counter: int = 0

        # ── Memory cache (all() read cache, save() write back) ──
        self._cache: Dict[str, MemoryAtom] = {}
        self._cache_dirty = False

        # ── SQLite ──
        self._db_path = os.path.join(self.data_dir, "hippocampus.db")
        self._ensure_dir()
        self._init_db()

    # ══════════════════════════════════════════
    # SQLite
    # ══════════════════════════════════════════

    def _ensure_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)

    def _init_db(self):
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS atoms (
                    atom_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    atom_type TEXT DEFAULT 'episodic',
                    entities TEXT DEFAULT '[]',
                    emotion REAL DEFAULT 0.0,
                    importance REAL DEFAULT 0.5,
                    timestamp REAL NOT NULL,
                    source TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    context_id TEXT DEFAULT '',
                    links TEXT DEFAULT '{}',
                    recall_count INTEGER DEFAULT 0,
                    last_recalled REAL DEFAULT 0,
                    stability REAL DEFAULT 1.0,
                    context_trace TEXT DEFAULT '[]'
                )
            """)
            # backward compatible migration (old DB no is_core / is_archived / is_immutable fields)
            for col in ["is_core", "is_archived", "is_immutable"]:
                try:
                    c.execute(f"ALTER TABLE atoms ADD COLUMN {col} INTEGER DEFAULT 0")
                except Exception:
                    pass  # field exists, ignore)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_atoms_timestamp
                ON atoms(timestamp)
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_atoms_importance
                ON atoms(importance)
            """)

    def _connect(self):
        """Return connection (auto enable WAL mode for concurrent safety)."""
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ── serialize helper ──

    @staticmethod
    def _ser(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def _deser(text: str):
        return json.loads(text) if text else []

    # ── CRUD ──

    def store(self, atom: MemoryAtom) -> Optional[dict]:
        # Cache invalidation
        self._cache.clear()
        self._cache_dirty = False
        """
        Save a memory atom.

        Full load logic:
          1. First check if there are low importance atoms to evict
          2. if all protected, emit on_capacity event
          3. Event listener (SleepConsolidation) optionally performs partial consolidation

        Returns:
            if consolidation triggered, return dict summary
        """
        with self._lock:
            count = self._count()
            consolidation_result = None

            if count >= self.cap:
                # Try to evict unimportant atoms
                evicted = self._fifo_evict()
                if not evicted:
                    # all protected → trigger capacity event
                    stats = self._capacity_stats(count)
                    self.on_capacity.emit(self, stats)
                    self._sleep_counter += 1
                    consolidation_result = stats

                    # Check capacity again
                    if self._count() >= self.cap:
                        # Force evict oldest (regardless of importance)
                        self._force_evict_oldest()

            # write
            self._insert(atom)

            # if event triggered, return summary
            if consolidation_result:
                return {
                    "trigger": "capacity_reached",
                    "action": "consolidation_requested",
                    "stats": consolidation_result,
                    "sleep_triggers": self._sleep_counter,
                }
            return None

    def get(self, atom_id: str) -> Optional[MemoryAtom]:
        with self._lock:
            row = self._fetch(atom_id)
            if not row:
                return None
            atom = self._row_to_atom(row)
            # Update recall count
            self._update_recall(atom_id)
            return atom

    def update(self, atom_id: str, **kwargs) -> bool:
        with self._lock:
            allowed = {"content", "emotion", "importance", "stability",
                       "links", "context_trace", "tags", "atom_type"}
            updates = {k: v for k, v in kwargs.items() if k in allowed}
            if not updates:
                return False
            self._update(atom_id, updates)
            return True

    def remove(self, atom_id: str) -> bool:
        with self._lock:
            c = self._connect()
            cur = c.execute("DELETE FROM atoms WHERE atom_id=?", (atom_id,))
            deleted = cur.rowcount > 0
            c.commit()
            c.close()
            return deleted

    delete = remove  # alias

    def all(self) -> List[MemoryAtom]:
        """Return all atoms (FIFO order), and cache to _cache for save() use."""
        with self._lock:
            c = self._connect()
            rows = c.execute(
                "SELECT * FROM atoms ORDER BY timestamp ASC"
            ).fetchall()
            c.close()
            atoms = [self._row_to_atom(r) for r in rows]
            # Update cache
            self._cache.clear()
            self._cache_dirty = True
            for a in atoms:
                self._cache[a.atom_id] = a
            return atoms

    def recent(self, n: int = 10) -> List[MemoryAtom]:
        with self._lock:
            c = self._connect()
            rows = c.execute(
                "SELECT * FROM atoms ORDER BY timestamp DESC LIMIT ?",
                (n,)
            ).fetchall()
            c.close()
            return [self._row_to_atom(r) for r in rows]

    def clear(self):
        with self._lock:
            c = self._connect()
            c.execute("DELETE FROM atoms")
            c.commit()
            c.close()

    def save(self):
        """
        Will write back all atom states in memory to SQLite.

        Sleep consolidation modified atom.links, atom.stability
        and other in-memory attributes, needs persistence via this method.
        """
        with self._lock:
            c = self._connect()
            for atom_id, atom in self._cache.items():
                c.execute(
                    """UPDATE atoms SET links=?, stability=?, context_trace=?
                    WHERE atom_id=?""",
                    (self._ser(atom.links), atom.stability,
                     self._ser(atom.context_trace), atom_id)
                )
            c.commit()
            c.close()

    def __len__(self) -> int:
        with self._lock:
            return self._count()

    def status(self) -> dict:
        with self._lock:
            count = self._count()
            protect_count = self._count_protected()
            oldest = self._oldest_age()
            return {
                "count": count,
                "capacity": self.cap,
                "usage_pct": round(count / max(1, self.cap) * 100, 1),
                "protected_count": protect_count,
                "oldest_age_days": round(oldest / 86400, 1) if oldest else 0,
                "sleep_triggers": self._sleep_counter,
                "db_path": self._db_path,
            }

    def capacity_reached(self) -> bool:
        """Quick check if full."""
        return self._count() >= self.cap

    # ══════════════════════════════════════════
    # SQLite internal method
    # ══════════════════════════════════════════

    def _count(self) -> int:
        c = self._connect()
        row = c.execute("SELECT COUNT(*) as n FROM atoms").fetchone()
        c.close()
        return row["n"]

    def _count_protected(self) -> int:
        c = self._connect()
        row = c.execute(
            "SELECT COUNT(*) as n FROM atoms WHERE importance >= ?",
            (self.protect_threshold,)
        ).fetchone()
        c.close()
        return row["n"]

    def _oldest_age(self) -> float:
        c = self._connect()
        row = c.execute(
            "SELECT MIN(timestamp) as t FROM atoms"
        ).fetchone()
        c.close()
        if row and row["t"]:
            return time.time() - row["t"]
        return 0.0

    def _capacity_stats(self, count: int) -> dict:
        """Full load statistics (for CapacityEvent use)."""
        c = self._connect()
        # Find the most important 5 items
        top = c.execute(
            "SELECT atom_id, content, importance, emotion "
            "FROM atoms ORDER BY importance DESC LIMIT 5"
        ).fetchall()
        c.close()
        return {
            "count": count,
            "capacity": self.cap,
            "protected_count": self._count_protected(),
            "oldest_age": self._oldest_age(),
            "top_atoms": [
                {"id": r["atom_id"], "content": r["content"][:60],
                 "importance": r["importance"], "emotion": r["emotion"]}
                for r in top
            ],
        }

    def _fifo_evict(self) -> bool:
        """
        FIFO eviction (skip high importance).

        Returns:
            True=evicted an atom, False=all protected
        """
        c = self._connect()
        # Find the first atom below importance threshold (sorted ASC)
        row = c.execute(
            "SELECT atom_id FROM atoms "
            "WHERE importance < ? "
            "ORDER BY timestamp ASC LIMIT 1",
            (self.protect_threshold,)
        ).fetchone()
        if row:
            c.execute("DELETE FROM atoms WHERE atom_id=?", (row["atom_id"],))
            c.commit()
            c.close()
            return True
        c.close()
        return False

    def _force_evict_oldest(self):
        """Force evict oldest (emergency case)."""
        c = self._connect()
        c.execute(
            "DELETE FROM atoms WHERE atom_id IN "
            "(SELECT atom_id FROM atoms ORDER BY timestamp ASC LIMIT 1)"
        )
        c.commit()
        c.close()

    def _insert(self, atom: MemoryAtom):
        c = self._connect()
        c.execute(
            """INSERT INTO atoms
            (atom_id, content, atom_type, entities, emotion,
             importance, timestamp, source, tags, context_id,
             links, recall_count, last_recalled, stability,
             context_trace, is_core, is_archived, is_immutable)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                atom.atom_id, atom.content, atom.atom_type,
                self._ser(atom.entities), atom.emotion,
                atom.importance, atom.timestamp, atom.source,
                self._ser(atom.tags), atom.context_id,
                self._ser(atom.links), atom.recall_count,
                atom.last_recalled, atom.stability,
                self._ser(atom.context_trace),
                int(atom.is_core), int(atom.is_archived),
                int(atom.is_immutable),
            )
        )
        c.commit()
        c.close()

    def _fetch(self, atom_id: str):
        c = self._connect()
        row = c.execute(
            "SELECT * FROM atoms WHERE atom_id=?", (atom_id,)
        ).fetchone()
        c.close()
        return row

    def _update_recall(self, atom_id: str):
        c = self._connect()
        c.execute(
            "UPDATE atoms SET recall_count=recall_count+1, "
            "last_recalled=? WHERE atom_id=?",
            (time.time(), atom_id)
        )
        c.commit()
        c.close()

    def _update(self, atom_id: str, updates: dict):
        if not updates:
            return
        sets = []
        vals = []
        for k, v in updates.items():
            col = k  # SQL column name matches attr name
            if isinstance(v, (dict, list)):
                v = self._ser(v)
            sets.append(f"{col}=?")
            vals.append(v)
        vals.append(atom_id)
        c = self._connect()
        c.execute(
            f"UPDATE atoms SET {', '.join(sets)} WHERE atom_id=?",
            vals
        )
        c.commit()
        c.close()

    def _row_to_atom(self, row) -> MemoryAtom:
        atom = MemoryAtom(
            content=row["content"],
            atom_id=row["atom_id"],
            atom_type=row["atom_type"],
            entities=self._deser(row["entities"]),
            emotion=row["emotion"],
            importance=row["importance"],
            timestamp=row["timestamp"],
            source=row["source"],
            tags=self._deser(row["tags"]),
            context_id=row["context_id"],
        )
        atom.links = self._deser(row["links"])
        atom.recall_count = row["recall_count"]
        atom.last_recalled = row["last_recalled"]
        atom.stability = row["stability"]
        atom.context_trace = self._deser(row["context_trace"])
        atom.is_core = bool(row["is_core"]) if "is_core" in row.keys() else False
        atom.is_archived = bool(row["is_archived"]) if "is_archived" in row.keys() else False
        atom.is_immutable = bool(row["is_immutable"]) if "is_immutable" in row.keys() else False
        return atom
