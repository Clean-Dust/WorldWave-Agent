"""
core/memory/labeled_wm.py — Single-system labeled working-memory facts

Absorbed from legacy EntityState flat WM into the v-next spine:

  - Explicit labels (kind): constraint / commitment / outcome / rationale
    Set only via remember(kind=…) / set() — no keyword guessing
    Product term: 标签; API field stays ``kind``
  - Core / persona protection: is_core never auto-evicted
  - Recency + access scoring (B6-style): same pure functions as entity_state
  - Entity-scoped keys (Same Timeline coupling)

This is the **one source of truth** for online labeled facts under
``~/.ww/memory/vnext/facts/`` (or a configured data_dir).

EntityState dual-write is emergency-only (WW_ENTITY_WM_DUAL_WRITE=1).
Product inject reads this store via MemoryVNext only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.entity_state import (
    DEFAULT_WM_KIND,
    WM_KINDS,
    WM_LABEL_ZH,
    WM_PROMOTE_LONG_VALUE_LEN,
    WM_PROMOTE_MIN_ACCESS,
    normalize_wm_kind,
    resolve_wm_recency_enabled,
    resolve_working_memory_capacity,
    wm_eviction_score,
    wm_label_zh,
    wm_now,
    wm_recency_factor,
)

logger = logging.getLogger("ww.memory.labeled_wm")

# Optional promote-on-evict: (entity_id, key, value, meta) -> None
OnEvict = Callable[..., None]
# Optional tie-break: (entity_id, key, meta) -> float (higher = more protect)
TiebreakFn = Callable[[str, str, Dict[str, Any]], float]


class LabeledFactStore:
    """Entity-scoped labeled fact buffer with kind/core/recency eviction.

    Persistence layout::

        {data_dir}/{entity_id}.json

    Capacity defaults to WW_WORKING_MEMORY_CAPACITY / 32 (shared with legacy env).
    """

    def __init__(
        self,
        data_dir: str = "",
        capacity: Optional[int] = None,
        on_evict: Optional[OnEvict] = None,
    ):
        base = data_dir or os.path.join(
            os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww")),
            "memory",
            "vnext",
            "facts",
        )
        self.data_dir = Path(base)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.capacity = (
            int(capacity)
            if capacity is not None
            else resolve_working_memory_capacity(None)
        )
        self._on_evict = on_evict
        self._tiebreak_fn: Optional[TiebreakFn] = None
        self._lock = Lock()
        # entity_id -> {key: value}
        self._facts: Dict[str, Dict[str, str]] = {}
        # entity_id -> {key: meta}
        self._meta: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # entity_id -> set of core keys
        self._core: Dict[str, Set[str]] = {}
        # entity_id -> eviction counter
        self._evicted_total: Dict[str, int] = {}
        # protected preference keys (never auto-evict) — optional per entity
        self._pref_keys: Dict[str, Set[str]] = {}
        self._promote_min_access = WM_PROMOTE_MIN_ACCESS
        self._promote_long_len = WM_PROMOTE_LONG_VALUE_LEN
        self._loaded: Set[str] = set()

    # ── Wire hooks ────────────────────────────────────────────────

    def set_on_evict(self, callback: Optional[OnEvict]) -> None:
        self._on_evict = callback

    def set_tiebreak_fn(self, fn: Optional[TiebreakFn]) -> None:
        self._tiebreak_fn = fn

    def set_preference_keys(self, entity_id: str, keys: Set[str]) -> None:
        """Keys that shadow preferences — never auto-evicted (like legacy)."""
        self._pref_keys[entity_id] = set(keys or ())

    # ── Load / save ───────────────────────────────────────────────

    def _path(self, entity_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in entity_id) or "default"
        return self.data_dir / f"{safe}.json"

    def _ensure_loaded(self, entity_id: str) -> None:
        if entity_id in self._loaded:
            return
        self._loaded.add(entity_id)
        path = self._path(entity_id)
        if not path.is_file():
            self._facts.setdefault(entity_id, {})
            self._meta.setdefault(entity_id, {})
            self._core.setdefault(entity_id, set())
            self._evicted_total.setdefault(entity_id, 0)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._facts[entity_id] = {
                str(k): str(v) for k, v in (data.get("facts") or {}).items()
            }
            meta_in = data.get("meta") or {}
            self._meta[entity_id] = {
                str(k): dict(m) for k, m in meta_in.items() if isinstance(m, dict)
            }
            # Normalize kinds
            for k, m in self._meta[entity_id].items():
                m["kind"] = normalize_wm_kind(m.get("kind"))
                m.setdefault("access_count", 0)
                m.setdefault("updated_at", time.time())
            self._core[entity_id] = set(data.get("core") or [])
            self._evicted_total[entity_id] = int(data.get("evicted_total") or 0)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("LabeledFactStore load failed for %s: %s", entity_id, e)
            self._facts.setdefault(entity_id, {})
            self._meta.setdefault(entity_id, {})
            self._core.setdefault(entity_id, set())
            self._evicted_total.setdefault(entity_id, 0)

    def _save(self, entity_id: str) -> None:
        path = self._path(entity_id)
        try:
            payload = {
                "entity_id": entity_id,
                "facts": dict(self._facts.get(entity_id) or {}),
                "meta": dict(self._meta.get(entity_id) or {}),
                "core": sorted(self._core.get(entity_id) or set()),
                "evicted_total": int(self._evicted_total.get(entity_id) or 0),
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("LabeledFactStore save failed for %s: %s", entity_id, e)

    # ── Public API ────────────────────────────────────────────────

    def get_facts(self, entity_id: str) -> Dict[str, str]:
        with self._lock:
            self._ensure_loaded(entity_id)
            return dict(self._facts.get(entity_id) or {})

    def get_meta(self, entity_id: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded(entity_id)
            return {k: dict(v) for k, v in (self._meta.get(entity_id) or {}).items()}

    def get_core(self, entity_id: str) -> Set[str]:
        with self._lock:
            self._ensure_loaded(entity_id)
            return set(self._core.get(entity_id) or set())

    def get(self, entity_id: str, key: str) -> Optional[str]:
        with self._lock:
            self._ensure_loaded(entity_id)
            return (self._facts.get(entity_id) or {}).get(key)

    def set(
        self,
        entity_id: str,
        key: str,
        value: str,
        *,
        kind: Optional[str] = None,
        is_core: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Store a labeled fact. Evicts under capacity using kind×access×recency.

        kind: explicit only; empty/illegal → outcome (or preserve existing).
        is_core: hard protect — never auto-evicted.
        """
        if not key:
            return {"status": "error", "message": "key required"}
        clock = float(now) if now is not None else wm_now()
        with self._lock:
            self._ensure_loaded(entity_id)
            facts = self._facts.setdefault(entity_id, {})
            meta_map = self._meta.setdefault(entity_id, {})
            core = self._core.setdefault(entity_id, set())

            previous = facts.get(key)
            facts[key] = value
            m = meta_map.setdefault(
                key,
                {
                    "updated_at": clock,
                    "access_count": 0,
                    "kind": DEFAULT_WM_KIND,
                },
            )
            m["updated_at"] = clock
            if kind is not None and str(kind).strip() != "":
                m["kind"] = normalize_wm_kind(kind)
            else:
                m["kind"] = normalize_wm_kind(m.get("kind"))
            if is_core:
                core.add(key)
            resolved = normalize_wm_kind(m.get("kind"))
            evicted = self._enforce_capacity(entity_id, now=clock)
            self._save(entity_id)
            return {
                "status": "stored",
                "key": key,
                "previous": previous,
                "kind": resolved,
                "is_core": key in core,
                "evicted": evicted,
            }

    def delete(self, entity_id: str, key: str) -> Optional[str]:
        with self._lock:
            self._ensure_loaded(entity_id)
            facts = self._facts.get(entity_id) or {}
            was = facts.pop(key, None)
            (self._meta.get(entity_id) or {}).pop(key, None)
            (self._core.get(entity_id) or set()).discard(key)
            self._save(entity_id)
            return was

    def clear_session(self, entity_id: str) -> Dict[str, int]:
        """Evict non-core keys (session reset). Core keys retained."""
        with self._lock:
            self._ensure_loaded(entity_id)
            facts = self._facts.get(entity_id) or {}
            meta_map = self._meta.get(entity_id) or {}
            core = self._core.get(entity_id) or set()
            cleared = 0
            promoted = 0
            kept_core = 0
            for key in list(facts.keys()):
                if self._is_protected(entity_id, key):
                    kept_core += 1
                    continue
                value = facts.get(key, "")
                meta = dict(meta_map.get(key) or {})
                will_promote = self._should_promote(value, meta) and self._on_evict
                self._promote_or_archive(entity_id, key, value, meta)
                if will_promote:
                    promoted += 1
                facts.pop(key, None)
                meta_map.pop(key, None)
                self._evicted_total[entity_id] = (
                    int(self._evicted_total.get(entity_id) or 0) + 1
                )
                cleared += 1
            self._save(entity_id)
            return {
                "wm_cleared": cleared,
                "promoted": promoted,
                "kept_core": kept_core,
            }

    def bump_access(
        self, entity_id: str, keys: Optional[List[str]] = None
    ) -> None:
        with self._lock:
            self._ensure_loaded(entity_id)
            facts = self._facts.get(entity_id) or {}
            meta_map = self._meta.setdefault(entity_id, {})
            targets = keys if keys is not None else list(facts.keys())
            now = wm_now()
            for key in targets:
                if key not in facts:
                    continue
                m = meta_map.setdefault(
                    key,
                    {
                        "updated_at": now,
                        "access_count": 0,
                        "kind": DEFAULT_WM_KIND,
                    },
                )
                m.setdefault("kind", DEFAULT_WM_KIND)
                m["access_count"] = int(m.get("access_count", 0)) + 1
            self._save(entity_id)

    def inject_block(
        self,
        entity_id: str,
        *,
        bump_access: bool = True,
        title: str = "Working memory (online facts)",
    ) -> str:
        """Chinese-label inject block (product contract).

        Format: ``- [约束] key: value`` — never English-only ``[constraint]``.
        """
        with self._lock:
            self._ensure_loaded(entity_id)
            facts = self._facts.get(entity_id) or {}
            if not facts:
                return ""
            meta_map = self._meta.get(entity_id) or {}
            if bump_access:
                now = wm_now()
                for key in facts:
                    m = meta_map.setdefault(
                        key,
                        {
                            "updated_at": now,
                            "access_count": 0,
                            "kind": DEFAULT_WM_KIND,
                        },
                    )
                    m.setdefault("kind", DEFAULT_WM_KIND)
                    m["access_count"] = int(m.get("access_count", 0)) + 1
                self._save(entity_id)
            lines = []
            for k, v in facts.items():
                m = meta_map.get(k) or {}
                zh = wm_label_zh(m.get("kind"))
                lines.append(f"- [{zh}] {k}: {v}")
            return f"{title}:\n" + "\n".join(lines)

    def status(self, entity_id: str = "") -> dict:
        with self._lock:
            if entity_id:
                self._ensure_loaded(entity_id)
                return {
                    "working_memory_size": len(self._facts.get(entity_id) or {}),
                    "working_memory_capacity": self.capacity,
                    "wm_evicted_total": int(self._evicted_total.get(entity_id) or 0),
                    "working_memory_core_count": len(self._core.get(entity_id) or set()),
                    "entity_id": entity_id,
                }
            return {
                "entities_loaded": len(self._loaded),
                "working_memory_capacity": self.capacity,
                "kinds": sorted(WM_KINDS),
            }

    def export_snapshot(self, entity_id: str) -> Dict[str, Any]:
        """Snapshot for EntityState mirror / debugging."""
        with self._lock:
            self._ensure_loaded(entity_id)
            return {
                "working_memory": dict(self._facts.get(entity_id) or {}),
                "working_memory_meta": {
                    k: dict(v)
                    for k, v in (self._meta.get(entity_id) or {}).items()
                },
                "working_memory_core": sorted(self._core.get(entity_id) or set()),
                "wm_evicted_total": int(self._evicted_total.get(entity_id) or 0),
            }

    def enforce_capacity(
        self, entity_id: str, now: Optional[float] = None
    ) -> List[str]:
        """Public capacity enforce (tests / fixed clock)."""
        with self._lock:
            self._ensure_loaded(entity_id)
            clock = float(now) if now is not None else wm_now()
            evicted = self._enforce_capacity(entity_id, now=clock)
            self._save(entity_id)
            return evicted

    # ── Eviction (absorbed B4–B7 scoring) ─────────────────────────

    def _is_protected(self, entity_id: str, key: str) -> bool:
        if key in (self._core.get(entity_id) or set()):
            return True
        if key in (self._pref_keys.get(entity_id) or set()):
            return True
        return False

    def _eviction_key(
        self, entity_id: str, key: str, now: float
    ) -> Tuple[float, float, float]:
        meta = (self._meta.get(entity_id) or {}).get(key) or {}
        access = int(meta.get("access_count", 0) or 0)
        updated = float(meta.get("updated_at", 0.0) or 0.0)
        age = max(0.0, now - updated)
        score = wm_eviction_score(meta.get("kind"), access, age_seconds=age)
        tiebreak = 0.0
        if self._tiebreak_fn is not None:
            try:
                tiebreak = float(self._tiebreak_fn(entity_id, key, meta) or 0.0)
            except Exception as e:
                logger.debug("wm tiebreak failed for %s: %s", key, e)
                tiebreak = 0.0
        return (score, tiebreak, updated)

    def _should_promote(self, value: str, meta: Dict[str, Any]) -> bool:
        access = int(meta.get("access_count", 0) or 0)
        if access >= self._promote_min_access:
            return True
        if access >= 1 and len(value or "") >= self._promote_long_len:
            return True
        return False

    def _promote_or_archive(
        self, entity_id: str, key: str, value: str, meta: Dict[str, Any]
    ) -> None:
        if not self._should_promote(value, meta):
            return
        if self._on_evict is None:
            return
        try:
            try:
                self._on_evict(entity_id, key, value, meta)
            except TypeError:
                self._on_evict(entity_id, key, value)
        except Exception as e:
            logger.warning("on_evict failed for %s key=%s: %s", entity_id[:12], key, e)

    def _enforce_capacity(self, entity_id: str, now: float) -> List[str]:
        facts = self._facts.setdefault(entity_id, {})
        meta_map = self._meta.setdefault(entity_id, {})
        cap = self.capacity
        evicted: List[str] = []
        while len(facts) > cap:
            candidates = [k for k in facts if not self._is_protected(entity_id, k)]
            if not candidates:
                break
            victim = min(
                candidates, key=lambda k: self._eviction_key(entity_id, k, now)
            )
            value = facts.pop(victim)
            meta = dict(meta_map.pop(victim, {}) or {})
            self._evicted_total[entity_id] = (
                int(self._evicted_total.get(entity_id) or 0) + 1
            )
            evicted.append(victim)
            access = int(meta.get("access_count", 0) or 0)
            updated = float(meta.get("updated_at", 0.0) or 0.0)
            age = max(0.0, now - updated)
            effective = wm_eviction_score(meta.get("kind"), access, age_seconds=age)
            factor = (
                wm_recency_factor(age) if resolve_wm_recency_enabled() else 1.0
            )
            logger.info(
                "LabeledWM evict entity=%s key=%s kind=%s access=%s score=%.4f "
                "age=%.1fs factor=%.4f (size→%d cap=%d)",
                entity_id[:12],
                victim,
                normalize_wm_kind(meta.get("kind")),
                access,
                effective,
                age,
                factor,
                len(facts),
                cap,
            )
            self._promote_or_archive(entity_id, victim, value, meta)
        return evicted
