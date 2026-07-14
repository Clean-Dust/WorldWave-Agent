"""
core/entity_state.py — Entity State Machine

Each cognitive entity (human user) has a persistent state that survives
server restarts. This replaces the old "session" concept — there is no
"new session", only a continuous entity timeline.

Lifecycle:
1. Message arrives → entity_id resolved → state loaded from disk
2. State injected into spiral loop context (working memory, preferences)
3. Spiral runs → agent may modify state via self-editing tools
4. Response sent → updated state saved to disk
5. Entity idle > TTL → state unloaded from memory (persisted on disk)

Architecture:
- In-memory: active entity states (async-safe dict)
- On-disk: SQLite per-entity state store
- Auto-hydration: load on first access, keep in memory while active
- Auto-dehydration: persist and free memory after idle TTL
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Dict, List, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core.config import ConfigManager

log = logging.getLogger("ww.entity_state")

# Default idle threshold (seconds) — overrideable via env or config
DEFAULT_IDLE_UNLOAD_SECONDS = 1800  # 30 min


class EntityState(BaseModel):
    """Persistent state for a single cognitive entity (human user).

    This is what makes WW feel like "the same agent" across platforms and
    time — the entity's working memory, preferences, and context summary
    are always restored.
    """

    entity_id: str
    display_name: str = "unknown"

    # ── Working memory (agent can self-edit via remember/forget tools) ──
    working_memory: Dict[str, str] = Field(default_factory=dict)

    # ── Context continuity ──
    last_context: str = ""
    last_interaction_at: float = 0.0
    last_platform: str = ""
    total_interactions: int = 0

    # ── Preferences ──
    preferences: Dict[str, str] = Field(default_factory=dict)

    # ── Active tasks ──
    active_goal: str = ""
    active_task_id: str = ""

    # ── Metadata ──
    created_at: float = Field(default_factory=time.time)
    last_saved_at: float = 0.0
    version: int = 1

    # ── Serialization (backward-compatible) ──

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict) -> "EntityState":
        return cls.model_validate(d)

    def get_context_injection(self) -> str:
        """Build a human-readable context block for LLM system prompt.

        This is what makes the agent feel continuous — the context from
        the previous interaction is always present.
        """
        parts: List[str] = []

        if self.display_name and self.display_name != "unknown":
            parts.append(f"You are speaking with {self.display_name}.")

        if self.working_memory:
            facts = "\n".join(f"- {k}: {v}" for k, v in self.working_memory.items())
            parts.append(f"Known facts about this user:\n{facts}")

        if self.last_context:
            parts.append(f"Previous interaction context:\n{self.last_context}")

        if self.preferences:
            prefs = ", ".join(f"{k}={v}" for k, v in self.preferences.items())
            parts.append(f"User preferences: {prefs}")

        if self.active_goal:
            parts.append(f"Active goal: {self.active_goal}")

        if self.total_interactions > 0:
            parts.append(
                f"This is interaction #{self.total_interactions + 1} with this user."
            )

        return "\n\n".join(parts)

    def summary(self) -> str:
        """One-line summary for debugging."""
        last = (
            f"{int(time.time() - self.last_interaction_at)}s ago"
            if self.last_interaction_at
            else "never"
        )
        return (
            f"EntityState(id={self.entity_id[:12]}, "
            f"name={self.display_name}, "
            f"interactions={self.total_interactions}, "
            f"last={last}, "
            f"goal={self.active_goal[:30] or '—'})"
        )

    def __repr__(self) -> str:
        return self.summary()


class EntityStateManager:
    """Manages entity states — hydrate, dehydrate, persist, load.

    Thread-safe. Uses SQLite (WAL mode) for persistence, in-memory dict for active entities.

    Usage:
        esm = EntityStateManager(config=config)
        state = esm.get("ent_a1b2c3d4")
        state.last_context = "User was debugging a Python import error"
        esm.save(state)
    """

    def __init__(self, config: "ConfigManager", data_dir: str = ""):
        self._config = config
        self._data_dir = Path(data_dir) if data_dir else Path(
            config.expand_path("~/.ww/entities")
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._states: Dict[str, EntityState] = {}
        self._db_path = self._data_dir / "entity_states.db"
        self._idle_unload_seconds = int(
            os.environ.get("WW_ENTITY_IDLE_UNLOAD", str(DEFAULT_IDLE_UNLOAD_SECONDS))
        )
        self._init_db()

    # ── Database ─────────────────────────────────────────────────

    def _init_db(self):
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS entity_states (
                        entity_id TEXT PRIMARY KEY,
                        state_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                """)
                conn.commit()
        except sqlite3.Error as e:
            log.error("Failed to initialize entity state DB: %s", e)
            raise

    # ── Public API ───────────────────────────────────────────────

    def get(self, entity_id: str) -> EntityState:
        """Get or create entity state. Always returns a valid state."""
        with self._lock:
            if entity_id in self._states:
                return self._states[entity_id]

            state = self._load(entity_id)
            if state is None:
                state = EntityState(entity_id=entity_id)
                log.info("Created new entity state: %s", entity_id)
            else:
                log.debug(
                    "Loaded entity state: %s (last active %ss ago)",
                    entity_id,
                    int(time.time() - state.last_interaction_at),
                )

            self._states[entity_id] = state
            return state

    def save(self, state: EntityState):
        """Persist entity state to disk."""
        state.last_saved_at = time.time()
        state.version += 1
        with self._lock:
            self._states[state.entity_id] = state
            self._persist(state)

    def unload(self, entity_id: str):
        """Persist and remove from memory (free RAM)."""
        with self._lock:
            if entity_id in self._states:
                self._persist(self._states[entity_id])
                del self._states[entity_id]
                log.debug("Unloaded entity state: %s", entity_id)

    def unload_idle(self, max_idle_seconds: int = 0):
        """Unload entities that have been idle too long."""
        threshold = max_idle_seconds or self._idle_unload_seconds
        now = time.time()
        with self._lock:
            idle_ids = [
                eid
                for eid, state in self._states.items()
                if now - state.last_interaction_at > threshold
            ]
        for eid in idle_ids:
            self.unload(eid)
        if idle_ids:
            log.info("Unloaded %d idle entities", len(idle_ids))

    def record_interaction(
        self,
        entity_id: str,
        context_summary: str,
        platform: str = "",
        updates: Optional[Dict[str, str]] = None,
    ):
        """Record a completed interaction — update context and working memory.

        Called after each spiral loop completes. Updates:
        - last_context (summary of this interaction)
        - working_memory (any facts the agent stored)
        - interaction counters
        """
        state = self.get(entity_id)
        state.last_context = context_summary
        state.last_interaction_at = time.time()
        state.total_interactions += 1
        if platform:
            state.last_platform = platform
        if updates:
            state.working_memory.update(updates)
        self.save(state)

    def set_working_memory(self, entity_id: str, key: str, value: str):
        """Set a working memory key (called by remember tool)."""
        state = self.get(entity_id)
        state.working_memory[key] = value
        self.save(state)

    def delete_working_memory(self, entity_id: str, key: str):
        """Delete a working memory key (called by forget tool)."""
        state = self.get(entity_id)
        state.working_memory.pop(key, None)
        self.save(state)

    def get_context_for(self, entity_id: str) -> str:
        """Get the context injection string for the LLM system prompt."""
        state = self.get(entity_id)
        return state.get_context_injection()

    def list_active(self) -> List[str]:
        """List currently loaded entity IDs."""
        with self._lock:
            return list(self._states.keys())

    def count_active(self) -> int:
        with self._lock:
            return len(self._states)

    # ── Internal ─────────────────────────────────────────────────

    def _load(self, entity_id: str) -> Optional[EntityState]:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute(
                    "SELECT state_json FROM entity_states WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()
            if row:
                data = json.loads(row[0])
                return EntityState.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("Corrupted entity state for %s: %s", entity_id, e)
        except sqlite3.Error as e:
            log.error("SQLite error loading entity %s: %s", entity_id, e)
        return None

    def _persist(self, state: EntityState):
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO entity_states "
                    "(entity_id, state_json, updated_at) VALUES (?, ?, ?)",
                    (
                        state.entity_id,
                        json.dumps(state.to_dict(), ensure_ascii=False),
                        time.time(),
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            log.error("SQLite error persisting entity %s: %s", state.entity_id, e)

    def __repr__(self) -> str:
        return (
            f"EntityStateManager(active={len(self._states)}, "
            f"db={self._db_path})"
        )
