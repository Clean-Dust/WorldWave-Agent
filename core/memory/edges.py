"""
core/memory/edges.py — Knowledge Graph Edge Relations

Models typed relationships between memory atoms, enabling multi-hop
reasoning without an external graph database.

Design:
- SQLite-backed, co-located with memory data
- Typed edges: SUPERSEDES, CONTRADICTS, CAUSED_BY, RELATED_TO, etc.
- CTE recursive queries for multi-hop traversal
- Zero external dependencies — pure Python + SQLite

This is the knowledge graph layer that replaces Neo4j for WW's use case.
The graph is not a separate system — it's integrated into the existing
memory storage, sharing the same SQLite database.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("ww.memory.edges")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
MEMORY_DIR = os.path.join(_WW_CFG, "memory")

# Built-in relation types
RELATION_TYPES = {
    "SUPERSEDES": "source supersedes target (source is newer/more correct)",
    "CONTRADICTS": "source contradicts target",
    "CAUSED_BY": "source was caused by target",
    "RELATED_TO": "source is semantically related to target",
    "DERIVED_FROM": "source was derived/inferred from target",
    "DEPENDS_ON": "source depends on target",
    "PART_OF": "source is part of target",
    "EXAMPLE_OF": "source is an example of target",
}


@dataclass
class Edge:
    """A directed, typed relationship between two memory atoms."""

    edge_id: str
    source_id: str   # atom_id of the source
    target_id: str   # atom_id of the target
    relation_type: str  # SUPERSEDES, CONTRADICTS, CAUSED_BY, etc.
    weight: float = 1.0
    created_at: float = 0.0
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation_type": self.relation_type,
            "weight": self.weight,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


class EdgeStore:
    """Persistent typed edge store with CTE multi-hop query support.

    Usage:
        store = EdgeStore()
        store.add_edge("atom_1", "atom_2", "SUPERSEDES")
        paths = store.traverse("atom_1", max_hops=3)
    """

    def __init__(self, data_dir: str = ""):
        self._data_dir = data_dir or MEMORY_DIR
        os.makedirs(self._data_dir, exist_ok=True)
        self._db_path = os.path.join(self._data_dir, "edges.db")
        self._lock = Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS edges (
                    edge_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    weight REAL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_edges_source
                ON edges(source_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_edges_target
                ON edges(target_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_edges_type
                ON edges(relation_type)
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique
                ON edges(source_id, target_id, relation_type)
            """)
            conn.commit()

    # ── CRUD ─────────────────────────────────────────────────────

    def add_edge(self, source_id: str, target_id: str, relation_type: str,
                 weight: float = 1.0, metadata: Optional[Dict] = None) -> str:
        """Create a typed edge between two atoms. Idempotent — same edge
        (source, target, type) is not duplicated."""
        import uuid
        edge_id = f"e_{uuid.uuid4().hex[:12]}"
        now = time.time()
        meta_json = __import__('json').dumps(metadata or {})

        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO edges "
                "(edge_id, source_id, target_id, relation_type, weight, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (edge_id, source_id, target_id, relation_type, weight, now, meta_json)
            )
            conn.commit()

        log.debug("Edge: %s -[%s]-> %s", source_id[:8], relation_type, target_id[:8])
        return edge_id

    def remove_edge(self, source_id: str, target_id: str, relation_type: str = ""):
        """Remove an edge. If relation_type is empty, removes all edges between source and target."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            if relation_type:
                conn.execute(
                    "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation_type = ?",
                    (source_id, target_id, relation_type)
                )
            else:
                conn.execute(
                    "DELETE FROM edges WHERE source_id = ? AND target_id = ?",
                    (source_id, target_id)
                )
            conn.commit()

    def get_edges(self, atom_id: str, direction: str = "both",
                  relation_type: str = "") -> List[Edge]:
        """Get all edges connected to an atom.

        Args:
            atom_id: The atom to query
            direction: "outgoing", "incoming", or "both"
            relation_type: Optional filter by relation type
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            if direction == "outgoing":
                query = "SELECT * FROM edges WHERE source_id = ?"
            elif direction == "incoming":
                query = "SELECT * FROM edges WHERE target_id = ?"
            else:
                query = "SELECT * FROM edges WHERE source_id = ? OR target_id = ?"

            params = [atom_id]
            if direction == "both":
                params = [atom_id, atom_id]

            if relation_type:
                query += " AND relation_type = ?"
                params.append(relation_type)

            rows = conn.execute(query, params).fetchall()

        return [self._row_to_edge(r) for r in rows]

    def get_superseded_by(self, atom_id: str) -> Optional[str]:
        """Get the atom that supersedes this one, if any."""
        edges = self.get_edges(atom_id, direction="incoming", relation_type="SUPERSEDES")
        return edges[0].source_id if edges else None

    def get_supersedes(self, atom_id: str) -> List[str]:
        """Get atoms that this one supersedes."""
        edges = self.get_edges(atom_id, direction="outgoing", relation_type="SUPERSEDES")
        return [e.target_id for e in edges]

    # ── Multi-hop traversal (CTE) ───────────────────────────────

    def traverse(self, start_id: str, max_hops: int = 3,
                 relation_types: List[str] = None,
                 direction: str = "outgoing") -> List[dict]:
        """Multi-hop graph traversal using recursive CTE.

        Args:
            start_id: Starting atom
            max_hops: Maximum hops (1-10, default 3)
            relation_types: Optional filter (e.g., ["CAUSED_BY", "DEPENDS_ON"])
            direction: "outgoing" (source→target), "incoming" (target→source), or "both"

        Returns:
            List of {atom_id, hop, path, relation_type} for each reached node
        """
        max_hops = min(max(max_hops, 1), 10)
        type_filter = ""
        type_params: list = []
        params: list = []

        if relation_types:
            placeholders = ",".join("?" * len(relation_types))
            type_filter = f"AND e.relation_type IN ({placeholders})"
            type_params = list(relation_types)

        with self._lock, sqlite3.connect(self._db_path) as conn:
            if direction == "incoming":
                # Traverse backwards: follow target → source
                query = f"""
                    WITH RECURSIVE traverse AS (
                        SELECT e.source_id AS node_id, e.target_id AS from_id,
                               e.relation_type, 1 AS hop,
                               e.source_id || ' <--[' || e.relation_type || ']-- ' || e.target_id AS path
                        FROM edges e
                        WHERE e.target_id = ? {type_filter}
                        UNION ALL
                        SELECT e.source_id AS node_id, e.target_id AS from_id,
                               e.relation_type, t.hop + 1,
                               t.path || ' <--[' || e.relation_type || ']-- ' || e.target_id
                        FROM edges e
                        JOIN traverse t ON e.target_id = t.node_id
                        WHERE t.hop < ? {type_filter}
                    )
                    SELECT DISTINCT node_id, relation_type, hop, path FROM traverse
                    ORDER BY hop, node_id
                """
            elif direction == "both":
                # Traverse both directions
                query = f"""
                    WITH RECURSIVE traverse AS (
                        SELECT e.target_id AS node_id, e.relation_type, 1 AS hop,
                               e.source_id || ' -[' || e.relation_type || ']-> ' || e.target_id AS path
                        FROM edges e
                        WHERE e.source_id = ? {type_filter}
                        UNION ALL
                        SELECT e.source_id AS node_id, e.relation_type, 1 AS hop,
                               e.target_id || ' <-[' || e.relation_type || ']- ' || e.source_id AS path
                        FROM edges e
                        WHERE e.target_id = ? {type_filter}
                        UNION ALL
                        SELECT e.target_id AS node_id, e.relation_type, t.hop + 1,
                               t.path || ' -[' || e.relation_type || ']-> ' || e.target_id
                        FROM edges e
                        JOIN traverse t ON e.source_id = t.node_id
                        WHERE t.hop < ?
                        UNION ALL
                        SELECT e.source_id AS node_id, e.relation_type, t.hop + 1,
                               t.path || ' <-[' || e.relation_type || ']- ' || e.source_id
                        FROM edges e
                        JOIN traverse t ON e.target_id = t.node_id
                        WHERE t.hop < ?
                    )
                    SELECT DISTINCT node_id, relation_type, hop, path FROM traverse
                    WHERE node_id != ?
                    ORDER BY hop, node_id
                """
                params = [start_id] + type_params + [start_id] + type_params + [max_hops, max_hops, start_id]
            else:
                # Default: outgoing (source → target)
                query = f"""
                    WITH RECURSIVE traverse AS (
                        SELECT e.target_id AS node_id, e.relation_type, 1 AS hop,
                               e.source_id || ' -[' || e.relation_type || ']-> ' || e.target_id AS path
                        FROM edges e
                        WHERE e.source_id = ? {type_filter}
                        UNION ALL
                        SELECT e.target_id AS node_id, e.relation_type, t.hop + 1,
                               t.path || ' -[' || e.relation_type || ']-> ' || e.target_id
                        FROM edges e
                        JOIN traverse t ON e.source_id = t.node_id
                        WHERE t.hop < ? {type_filter}
                    )
                    SELECT DISTINCT node_id, relation_type, hop, path FROM traverse
                    ORDER BY hop, node_id
                """
                params = [start_id] + type_params + [max_hops] + type_params

            rows = conn.execute(query, params).fetchall()

        return [
            {"atom_id": r[0], "relation_type": r[1], "hop": r[2], "path": r[3]}
            for r in rows
        ]

    def find_paths(self, from_id: str, to_id: str, max_hops: int = 5) -> List[dict]:
        """Find all paths between two atoms using bidirectional BFS via CTE.

        Returns paths with format: {path: str, hops: int, relations: [str]}
        """
        max_hops = min(max(max_hops, 1), 10)

        with self._lock, sqlite3.connect(self._db_path) as conn:
            query = """
                WITH RECURSIVE paths AS (
                    SELECT e.target_id AS node_id, e.relation_type, 1 AS hop,
                           e.source_id || ' -[' || e.relation_type || ']-> ' || e.target_id AS path
                    FROM edges e
                    WHERE e.source_id = ?
                    UNION ALL
                    SELECT e.target_id AS node_id, e.relation_type, p.hop + 1,
                           p.path || ' -[' || e.relation_type || ']-> ' || e.target_id
                    FROM edges e
                    JOIN paths p ON e.source_id = p.node_id
                    WHERE p.hop < ? AND p.node_id != ?
                )
                SELECT path, hop FROM paths
                WHERE node_id = ?
                ORDER BY hop
                LIMIT 10
            """
            rows = conn.execute(query, (from_id, max_hops, to_id, to_id)).fetchall()

        return [
            {"path": r[0], "hops": r[1]}
            for r in rows
        ]

    # ── Stats ────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return edge store statistics."""
        with sqlite3.connect(self._db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            by_type = conn.execute(
                "SELECT relation_type, COUNT(*) FROM edges GROUP BY relation_type"
            ).fetchall()
        return {
            "total_edges": total,
            "by_type": dict(by_type),
            "db_path": self._db_path,
        }

    # ── Internal ─────────────────────────────────────────────────

    def _row_to_edge(self, row: tuple) -> Edge:
        import json
        return Edge(
            edge_id=row[0],
            source_id=row[1],
            target_id=row[2],
            relation_type=row[3],
            weight=row[4],
            created_at=row[5],
            metadata=json.loads(row[6]) if row[6] else {},
        )
