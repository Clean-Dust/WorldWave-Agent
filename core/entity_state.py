"""
core/entity_state.py — Entity State Machine

Each cognitive entity (human user) has a persistent state that survives
server restarts. This replaces the old "session" concept — there is no
"new session", only a continuous entity timeline.

Working Memory (entity RAM):
  Fixed-capacity online fact buffer (default 32). Full buffer → evict
  by role-weighted score then oldest keys (numeric only; no keyword
  importance). Memory roles (kind): commitment (decisions, highest
  protect), outcome (results, high), rationale (process, easiest to
  squeeze). Kind is set only via explicit remember(kind=...)/set_working_memory;
  never inferred from content keywords. Important evictions promote via
  on_wm_evict (MemorySystem) and/or archive to
  ~/.ww/entities/<id>/wm_evicted.jsonl. Does not promise an infinite LLM
  prompt — only the current RAM set is injected into context.

Memory stack (three layers):
  Working Memory (entity RAM, this module)
    → Hippocampus (episodic capacity + GC/protect)
    → sleep / promote (long-term memory)
  Subconscious is referee/gating only (BG safe gate + optional WM
  tie-break score); it never replaces WM or hippocampus storage.

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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core.config import ConfigManager

log = logging.getLogger("ww.entity_state")

# Default idle threshold (seconds) — overrideable via env or config
DEFAULT_IDLE_UNLOAD_SECONDS = 1800  # 30 min

# Working memory (entity RAM) — fixed capacity, no infinite prompt promise
DEFAULT_WORKING_MEMORY_CAPACITY = 32
# Promote on evict when access_count >= this (numeric only)
WM_PROMOTE_MIN_ACCESS = int(os.environ.get("WW_WM_PROMOTE_MIN_ACCESS", "2"))
# Or when value is long and has been accessed at least once
WM_PROMOTE_LONG_VALUE_LEN = int(os.environ.get("WW_WM_PROMOTE_LONG_LEN", "80"))

# Memory roles (kind) — explicit only; illegal/missing → DEFAULT_WM_KIND
WM_KINDS = frozenset({"commitment", "rationale", "outcome"})
DEFAULT_WM_KIND = "outcome"
# Eviction protection weights: higher score stays longer.
# score = ROLE_WEIGHT[kind] * (1 + access_count); lowest score evicted first.
ROLE_WEIGHT: Dict[str, float] = {
    "commitment": float(os.environ.get("WW_WM_WEIGHT_COMMITMENT", "3.0")),
    "outcome": float(os.environ.get("WW_WM_WEIGHT_OUTCOME", "2.0")),
    "rationale": float(os.environ.get("WW_WM_WEIGHT_RATIONALE", "1.0")),
}

# Callback: (entity_id, key, value) -> None; optional 4th meta dict may be passed
OnWmEvict = Callable[..., None]
# Optional WM eviction tie-break: (entity_id, key, meta) -> float.
# Higher = more protect (evicted later). Used only when primary score ties.
WmTiebreakFn = Callable[[str, str, Dict[str, Any]], float]


def normalize_wm_kind(kind: Optional[str] = None) -> str:
    """Normalize WM kind to commitment | rationale | outcome.

    Empty, missing, unknown, or 'unknown' → outcome (backward compatible).
    No keyword inference from fact content — only explicit kind values.
    """
    if kind is None:
        return DEFAULT_WM_KIND
    k = str(kind).strip().lower()
    if not k or k == "unknown":
        return DEFAULT_WM_KIND
    if k in WM_KINDS:
        return k
    return DEFAULT_WM_KIND


def wm_role_weight(kind: Optional[str] = None) -> float:
    """Protection weight for a kind (commitment > outcome > rationale)."""
    return float(ROLE_WEIGHT.get(normalize_wm_kind(kind), ROLE_WEIGHT[DEFAULT_WM_KIND]))


def wm_eviction_score(kind: Optional[str] = None, access_count: int = 0) -> float:
    """Pure numeric eviction score; lower is evicted first.

    score = role_weight * (1 + access_count)
    """
    return wm_role_weight(kind) * (1.0 + max(0, int(access_count or 0)))


def resolve_working_memory_capacity(config: Optional["ConfigManager"] = None) -> int:
    """Resolve WM capacity: env WW_WORKING_MEMORY_CAPACITY, then config keys, else 32."""
    env = os.environ.get("WW_WORKING_MEMORY_CAPACITY")
    if env is not None and str(env).strip() != "":
        try:
            return max(1, int(env))
        except (TypeError, ValueError):
            pass
    if config is not None:
        for key in (
            "memory.working_memory_capacity",
            "working_memory_capacity",
        ):
            try:
                v = config.get(key)
            except Exception:
                v = None
            if v is not None and str(v).strip() != "":
                try:
                    return max(1, int(v))
                except (TypeError, ValueError):
                    pass
        # Nested user config: {"memory": {"working_memory_capacity": N}}
        try:
            mem = config.get("memory")
            if isinstance(mem, dict) and mem.get("working_memory_capacity") is not None:
                return max(1, int(mem["working_memory_capacity"]))
        except Exception:
            pass
    return DEFAULT_WORKING_MEMORY_CAPACITY


class EntityState(BaseModel):
    """Persistent state for a single cognitive entity (human user).

    This is what makes WW feel like "the same agent" across platforms and
    time — the entity's working memory, preferences, and context summary
    are always restored.

    Working memory is a fixed-capacity RAM buffer. Overflow evicts by
    role-weighted score (kind) then older keys; core keys
    (working_memory_core) are retained. Meta (updated_at, access_count,
    kind) is persisted with the state. Missing kind defaults to outcome.
    """

    entity_id: str
    display_name: str = "unknown"

    # ── Working memory (agent can self-edit via remember/forget tools) ──
    # Bounded RAM facts — capacity enforced by EntityStateManager on write.
    working_memory: Dict[str, str] = Field(default_factory=dict)
    # Per-key meta: updated_at, access_count, kind (commitment|rationale|outcome).
    working_memory_meta: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Keys that must not be auto-evicted (is_core path; no keyword lists).
    working_memory_core: Set[str] = Field(default_factory=set)
    # Lifetime eviction counter (for status / observability).
    wm_evicted_total: int = 0

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
        d = self.model_dump()
        # JSON-friendly set
        d["working_memory_core"] = sorted(self.working_memory_core)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EntityState":
        data = dict(d)
        core = data.get("working_memory_core")
        if isinstance(core, list):
            data["working_memory_core"] = set(core)
        elif core is None:
            data["working_memory_core"] = set()
        # Backward compat: older states lack meta / counters
        data.setdefault("working_memory_meta", {})
        data.setdefault("wm_evicted_total", 0)
        return cls.model_validate(data)

    def bump_wm_access(self, keys: Optional[List[str]] = None) -> None:
        """Increment access_count for keys (default: all current WM keys)."""
        now = time.time()
        targets = keys if keys is not None else list(self.working_memory.keys())
        for key in targets:
            if key not in self.working_memory:
                continue
            meta = self.working_memory_meta.setdefault(
                key,
                {
                    "updated_at": now,
                    "access_count": 0,
                    "kind": DEFAULT_WM_KIND,
                },
            )
            meta.setdefault("kind", DEFAULT_WM_KIND)
            meta["access_count"] = int(meta.get("access_count", 0)) + 1

    def get_context_injection(self, bump_access: bool = True) -> str:
        """Build a human-readable context block for LLM system prompt.

        Only injects facts currently in the working-memory RAM buffer
        (capacity-bounded). Title: "Working memory (online facts)".
        When bump_access is True, increments access_count for injected keys.
        Fact lines include a kind tag: ``- [commitment] key: value``.
        """
        parts: List[str] = []

        if self.display_name and self.display_name != "unknown":
            parts.append(f"You are speaking with {self.display_name}.")

        if self.working_memory:
            if bump_access:
                self.bump_wm_access()
            fact_lines: List[str] = []
            for k, v in self.working_memory.items():
                meta = self.working_memory_meta.get(k) or {}
                kind = normalize_wm_kind(meta.get("kind"))
                fact_lines.append(f"- [{kind}] {k}: {v}")
            facts = "\n".join(fact_lines)
            parts.append(f"Working memory (online facts):\n{facts}")

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

    def wm_status(self, capacity: int) -> dict:
        """Minimal status for identity / memory observability."""
        return {
            "working_memory_size": len(self.working_memory),
            "working_memory_capacity": capacity,
            "wm_evicted_total": int(self.wm_evicted_total),
            "working_memory_core_count": len(self.working_memory_core),
        }

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
            f"wm={len(self.working_memory)}, "
            f"last={last}, "
            f"goal={self.active_goal[:30] or '—'})"
        )

    def __repr__(self) -> str:
        return self.summary()


class EntityStateManager:
    """Manages entity states — hydrate, dehydrate, persist, load.

    Thread-safe. Uses SQLite (WAL mode) for persistence, in-memory dict for active entities.

    Working memory writes enforce a fixed capacity (default 32). Overflow
    evicts keys with lowest role-weighted score
    (ROLE_WEIGHT[kind] * (1 + access_count)), then optional subconscious
    tie-break (higher protect first when scores equal), then oldest
    updated_at. Core keys and preference keys are not auto-evicted.
    On promote-worthy eviction, optional on_wm_evict callback runs; all
    evictions are archived to entities/<id>/wm_evicted.jsonl for recovery.
    Without a tie-break hook, eviction matches kind+access+updated only.

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
        self.working_memory_capacity = resolve_working_memory_capacity(config)
        self._on_wm_evict: Optional[OnWmEvict] = None
        # Optional numeric tie-break only (never overrides kind/access score).
        self._wm_tiebreak_fn: Optional[WmTiebreakFn] = None
        self._promote_min_access = WM_PROMOTE_MIN_ACCESS
        self._promote_long_len = WM_PROMOTE_LONG_VALUE_LEN
        self._init_db()

    def set_on_wm_evict(self, callback: Optional[OnWmEvict]) -> None:
        """Register promote-on-evict handler (e.g. MemorySystem.store_fact)."""
        self._on_wm_evict = callback

    def set_wm_tiebreak_fn(self, fn: Optional[WmTiebreakFn]) -> None:
        """Optional eviction tie-break: (entity_id, key, meta) -> float.

        Higher return value = more protect (evicted later). Compared only
        when primary score (kind weight × access) is equal. Pass None to
        clear (same behavior as no hook / 640846e).
        Alias: set_on_wm_score.
        """
        self._wm_tiebreak_fn = fn

    def set_on_wm_score(self, fn: Optional[WmTiebreakFn]) -> None:
        """Alias for set_wm_tiebreak_fn (contract naming)."""
        self.set_wm_tiebreak_fn(fn)

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
        - working_memory (any facts the agent stored; capacity-enforced)
        - interaction counters
        """
        state = self.get(entity_id)
        state.last_context = context_summary
        state.last_interaction_at = time.time()
        state.total_interactions += 1
        if platform:
            state.last_platform = platform
        if updates:
            now = time.time()
            for key, value in updates.items():
                state.working_memory[key] = value
                meta = state.working_memory_meta.setdefault(
                    key,
                    {
                        "updated_at": now,
                        "access_count": 0,
                        "kind": DEFAULT_WM_KIND,
                    },
                )
                meta["updated_at"] = now
                meta.setdefault("kind", DEFAULT_WM_KIND)
            self._enforce_wm_capacity(state)
        self.save(state)

    def set_working_memory(
        self,
        entity_id: str,
        key: str,
        value: str,
        kind: Optional[str] = None,
        is_core: bool = False,
    ):
        """Set a working memory key (called by remember tool).

        Enforces capacity: if over capacity, evict lowest role-weighted score
        then oldest non-core keys. is_core marks the key so it is not
        auto-evicted.

        kind: optional memory role — commitment | rationale | outcome.
        Explicit only (no keyword inference). Empty/illegal/unknown → outcome.
        When kind is None and the key already has meta.kind, existing kind
        is preserved; new keys default to outcome.
        """
        state = self.get(entity_id)
        now = time.time()
        state.working_memory[key] = value
        meta = state.working_memory_meta.setdefault(
            key,
            {
                "updated_at": now,
                "access_count": 0,
                "kind": DEFAULT_WM_KIND,
            },
        )
        meta["updated_at"] = now
        if kind is not None and str(kind).strip() != "":
            meta["kind"] = normalize_wm_kind(kind)
        else:
            meta["kind"] = normalize_wm_kind(meta.get("kind"))
        if is_core:
            state.working_memory_core.add(key)
        self._enforce_wm_capacity(state)
        self.save(state)

    def delete_working_memory(self, entity_id: str, key: str):
        """Delete a working memory key (called by forget tool)."""
        state = self.get(entity_id)
        state.working_memory.pop(key, None)
        state.working_memory_meta.pop(key, None)
        state.working_memory_core.discard(key)
        self.save(state)

    def get_context_for(self, entity_id: str) -> str:
        """Get the context injection string for the LLM system prompt.

        Bumps access counts for injected WM keys (persisted on next save;
        also saved here so ranking stays current across restarts).
        """
        state = self.get(entity_id)
        text = state.get_context_injection(bump_access=True)
        # Persist access bumps without bumping version thrashing every read:
        # cheap write so eviction scores remain meaningful after restart.
        if state.working_memory:
            with self._lock:
                self._persist(state)
        return text

    def get_wm_status(self, entity_id: str) -> dict:
        """Working-memory size / capacity / eviction counters."""
        state = self.get(entity_id)
        return state.wm_status(self.working_memory_capacity)

    def list_active(self) -> List[str]:
        """List currently loaded entity IDs."""
        with self._lock:
            return list(self._states.keys())

    def count_active(self) -> int:
        with self._lock:
            return len(self._states)

    # ── Working memory capacity / eviction ───────────────────────

    def _is_wm_protected(self, state: EntityState, key: str) -> bool:
        """True if key must not be auto-evicted (core set or preference key)."""
        if key in state.working_memory_core:
            return True
        if key in state.preferences:
            return True
        return False

    def _wm_eviction_key(self, state: EntityState, key: str):
        """Sort key for eviction: lower tuple = evicted first (min victim).

        1) score = ROLE_WEIGHT[kind] * (1 + access_count)  (primary)
        2) tiebreak  (optional; higher = more protect = larger = later)
        3) updated_at (oldest first when still tied)

        When primary scores differ (e.g. commitment vs rationale), tiebreak
        cannot reorder them. Missing kind → outcome.

        Note: second component is +tiebreak (not negated) so higher protect
        sorts after lower protect under min().
        """
        meta = state.working_memory_meta.get(key) or {}
        access = int(meta.get("access_count", 0) or 0)
        updated = float(meta.get("updated_at", 0.0) or 0.0)
        score = wm_eviction_score(meta.get("kind"), access)
        tiebreak = 0.0
        if self._wm_tiebreak_fn is not None:
            try:
                tiebreak = float(
                    self._wm_tiebreak_fn(state.entity_id, key, meta) or 0.0
                )
            except Exception as e:
                log.debug("wm tiebreak fn failed for %s: %s", key, e)
                tiebreak = 0.0
        # Higher protect → larger second element → min() keeps it longer
        return (score, tiebreak, updated)

    def _should_promote(self, value: str, meta: Dict[str, Any]) -> bool:
        """Numeric promote criteria only (no keyword lists)."""
        access = int(meta.get("access_count", 0) or 0)
        if access >= self._promote_min_access:
            return True
        if access >= 1 and len(value or "") >= self._promote_long_len:
            return True
        return False

    def _archive_evicted(
        self, entity_id: str, key: str, value: str, meta: Dict[str, Any]
    ) -> None:
        """Append eviction record to entities/<id>/wm_evicted.jsonl."""
        try:
            archive_dir = self._data_dir / entity_id
            archive_dir.mkdir(parents=True, exist_ok=True)
            path = archive_dir / "wm_evicted.jsonl"
            # Ensure kind present in archived meta for recovery / analytics
            meta_out = dict(meta or {})
            meta_out["kind"] = normalize_wm_kind(meta_out.get("kind"))
            record = {
                "ts": time.time(),
                "entity_id": entity_id,
                "key": key,
                "value": value,
                "meta": meta_out,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("Failed to archive WM eviction for %s/%s: %s", entity_id, key, e)

    def _promote_or_archive(
        self, entity_id: str, key: str, value: str, meta: Dict[str, Any]
    ) -> None:
        """Always archive; promote via callback when criteria met."""
        self._archive_evicted(entity_id, key, value, meta)
        if not self._should_promote(value, meta):
            return
        if self._on_wm_evict is None:
            return
        try:
            # Prefer 4-arg (entity_id, key, value, meta) when accepted;
            # fall back to 3-arg for older callbacks.
            try:
                self._on_wm_evict(entity_id, key, value, meta)
            except TypeError:
                self._on_wm_evict(entity_id, key, value)
        except Exception as e:
            log.warning(
                "on_wm_evict failed for %s key=%s: %s", entity_id[:12], key, e
            )

    def _enforce_wm_capacity(self, state: EntityState) -> List[str]:
        """Evict until len(working_memory) <= capacity. Returns evicted keys.

        Strategy (numeric only): among non-protected keys, prefer lowest
        score = ROLE_WEIGHT[kind]*(1+access_count), then lowest optional
        subconscious tie-break protect, then oldest updated_at.
        If all remaining are protected, stop (may exceed capacity — same
        spirit as hippocampus protect).
        """
        cap = self.working_memory_capacity
        evicted: List[str] = []
        while len(state.working_memory) > cap:
            candidates = [
                k for k in state.working_memory if not self._is_wm_protected(state, k)
            ]
            if not candidates:
                log.debug(
                    "WM over capacity but all keys protected (size=%d cap=%d)",
                    len(state.working_memory),
                    cap,
                )
                break
            victim = min(candidates, key=lambda k: self._wm_eviction_key(state, k))
            value = state.working_memory.pop(victim)
            meta = dict(state.working_memory_meta.pop(victim, {}) or {})
            state.wm_evicted_total = int(state.wm_evicted_total) + 1
            evicted.append(victim)
            log.info(
                "WM evict entity=%s key=%s kind=%s access=%s score=%.2f "
                "(size→%d cap=%d)",
                state.entity_id[:12],
                victim,
                normalize_wm_kind(meta.get("kind")),
                meta.get("access_count", 0),
                wm_eviction_score(meta.get("kind"), int(meta.get("access_count", 0) or 0)),
                len(state.working_memory),
                cap,
            )
            self._promote_or_archive(state.entity_id, victim, value, meta)
        return evicted

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
            f"wm_cap={self.working_memory_capacity}, "
            f"db={self._db_path})"
        )
