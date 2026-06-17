"""
ww/core/memory/system.py — MemorySystem integration entry point

MemorySystem integrates all memory subsystems into a single API.

Replaces the old HTTP bridge (memory_integration.py),
allowing all components to directly Python import.

Usage:
    from core.memory import MemorySystem
    ms = MemorySystem()
    ms.store("Completed FastAPI dependency injection learning")
    ms.store("Discovered SQLAlchemy n+1 query problem")
    results = ms.recall("FastAPI")
    ms.sleep()  # manualconsolidation
"""

from __future__ import annotations
import logging
import os
import time
from typing import Dict, List, Optional

from .atom import FactStore, MemoryAtom
from .encoder import EncodingLayer
from .hippocampus import Hippocampus
from .amygdala import Amygdala
from .sleep import DailyScheduler, IdleDetector, SleepConsolidation, SleepDaemon
from .recall import RecallEngine
from .reconsolidation import Reconsolidation

logger = logging.getLogger("ww.memory.system")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
MEMORY_DIR = os.path.join(_WW_CFG, "memory")


class _CompatStore:
    """Backward compatible: old MemoryStore get/get_recent/get_top_scored API."""

    def __init__(self, ms: "MemorySystem"):
        self._ms = ms

    def __call__(self, content: str, source: str = "user",
                 context_id: str = "", urgency: float = 0.5,
                 tags: list = None) -> dict:
        """Allow direct call for backward compatibility."""
        return self._ms._do_store(
            content=content, source=source,
            context_id=context_id, urgency=urgency, tags=tags or [],
        )

    def get(self, key: str) -> Optional[Dict]:
        return self._ms.hippocampus.get(key)

    def get_recent(self, limit: int = 20) -> List[MemoryAtom]:
        atoms = self._ms.hippocampus.all()
        atoms.sort(key=lambda a: a.timestamp, reverse=True)
        return atoms[:limit]

    def get_top_scored(self, limit: int = 20) -> List[MemoryAtom]:
        atoms = self._ms.hippocampus.all()
        scored = sorted(atoms, key=lambda a: -self._ms.amygdala.score(a))
        return scored[:limit]

    def stats(self) -> dict:
        return self._ms.buffer_status()


class MemorySystem:
    """
    Unified entry point for memory system.

    Simultaneously provides three memory layers:
    - Episodic: short-term Hippocampus, processed by sleep consolidation
    - Semantic: FactStore persistent knowledge
    - Procedural: workflow memory

    lifecycle: 
    1. store(content) → EncodingLayer → Hippocampus
    2. recall(query) → RecallEngine → direct matching + diffusion activation
    3. sleep() → Amygdala scoring → SleepConsolidation → consolidation / pruning / abstraction
    4. Each recall auto → Reconsolidation updates stability
    """

    def __init__(
        self,
        hippocampus_cap: int = int(os.environ.get("WW_HIPPOCAMPUS_CAP", "100")),
        data_dir: str = "",
        schedule_sleep_hour: int = int(os.environ.get("WW_MEMORY_SLEEP_HOUR", "3")),
        idle_threshold_minutes: float = float(os.environ.get("WW_MEMORY_IDLE_THRESHOLD", "0")),
        top_k: int = 5,
    ):
        self.data_dir = data_dir or MEMORY_DIR
        os.makedirs(self.data_dir, exist_ok=True)
        self._last_activity: float = time.time()

        # ── Subsystem initialization ──
        self.encoder = EncodingLayer()
        self.hippocampus = Hippocampus(
            cap=hippocampus_cap,
            data_dir=self.data_dir,
        )
        self.amygdala = Amygdala(data_dir=self.data_dir)
        self.fact_store = FactStore(data_dir=self.data_dir)
        self.sleep_engine = SleepConsolidation(
            data_dir=self.data_dir,
            fact_store=self.fact_store,  # Enable Fact Hegemony
        )
        self.recall_engine = RecallEngine(
            hippocampus=self.hippocampus,
            amygdala=self.amygdala,
            fact_store=self.fact_store,
            top_k=top_k,
        )
        self.reconsolidation = Reconsolidation(data_dir=self.data_dir)

        # ── Dual trigger: daily schedule + dynamic idle ──
        self.scheduler = DailyScheduler(
            scheduled_hour=schedule_sleep_hour,
        ) if schedule_sleep_hour >= 0 else None

        self.idle_detector = IdleDetector(
            idle_threshold_minutes=idle_threshold_minutes,
        ) if idle_threshold_minutes > 0 else None

        # ── startbackground SleepDaemon ──
        self._daemon: Optional[SleepDaemon] = None
        if self.scheduler or self.idle_detector:
            self._daemon = SleepDaemon(
                sleep_engine=self.sleep_engine,
                hippocampus=self.hippocampus,
                amygdala=self.amygdala,
                scheduler=self.scheduler,
                idle_detector=self.idle_detector,
                poll_interval=60.0,
            )
            self._daemon.start()

        # ── Callback connection ──
        # hippocampus full → trigger sleep
        self.hippocampus.on_capacity_reached = self._auto_sleep_handler

        # sleep abstraction mode → auto store into hippocampus
        self.sleep_engine.on_pattern_extracted = self._on_pattern_extracted

        # ── load ──
        self.fact_store.load()

        # ── Backward compatible: old MemoryStore API ──
        self.store = _CompatStore(self)

    # ── save ──

    def _do_store(
        self,
        content: str,
        source: str = "",
        atom_type: str = "",
        context_id: str = "",
        tags: Optional[List[str]] = None,
        urgency: float = 0.0,
        emotion_tag: str = "",  # LLM auxiliary emotion tag
    ) -> dict:
        """Save an experience/knowledge to memory system.

        Args:
            content: contenttext
            source: source (user/tool/system/error/inference) 
            atom_type: type (auto-detect if empty) 
            context_id: associated context ID
            tags: custom tags
            urgency: urgency [0,1]
            emotion_tag: LLM assisted emotion tag

        Returns:
            {"atom_id": "...", "atom_type": "...", "sleep_triggered": bool, ...}
        """
        # 1. encode
        atom = self.encoder.encode(
            content=content,
            source=source,
            atom_type=atom_type,
            context_id=context_id,
            tags=tags,
            urgency=urgency,
            emotion_tag=emotion_tag,
        )

        # 2. Store into hippocampus
        sleep_result = self.hippocampus.store(atom)

        # 3. if it is fact type, also store into fact_store
        if atom.atom_type == "semantic":
            self.fact_store.add(
                content=content,
                entities=atom.entities,
                category="memory_semantic",
                tags=tags,
            )

        result = {
            "atom_id": atom.atom_id,
            "atom_type": atom.atom_type,
            "source": atom.source,
            "emotion": atom.emotion,
            "importance": atom.importance,
            "entities": atom.entities,
            "buffer_usage": self.buffer_status(),
        }

        if sleep_result:
            result["sleep_triggered"] = True
            result["sleep_result"] = sleep_result

        # Mark system active (idle detection)
        self.mark_active()

        return result

    def mark_active(self):
        """Mark system has activity, for dynamic idle detection."""
        self._last_activity = time.time()
        if self.idle_detector:
            self.idle_detector.mark_active()

    def store_error(self, error_msg: str, context: str = "",
                     context_id: str = "") -> dict:
        return self._do_store(
            content=f"[ERROR] {error_msg}",
            source="error",
            context_id=context_id,
            urgency=1.0,
        )

    def store_success(self, summary: str, context_id: str = "") -> dict:
        return self._do_store(
            content=f"[SUCCESS] {summary}",
            source="tool",
            context_id=context_id,
        )

    def store_fact(self, fact: str, entities: List[str],
                    context_id: str = "") -> dict:
        return self._do_store(
            content=fact,
            source="inference",
            atom_type="semantic",
            tags=entities,
            context_id=context_id,
        )

    # ── recall ──

    def recall(self, query: str, top_k: int = 0,
               max_tokens: int = 0) -> dict:
        """Recall and query related memories.

        Args:
            query: querytext
            top_k: returncount
            max_tokens: token budget limit (0=default, <0=unlimited)

        Returns:
            {"results": [...], "total": N, "compressed": bool, ...}
        """
        self.mark_active()
        results = self.recall_engine.recall(query, top_k=top_k,
                                            max_tokens=max_tokens)

        # Update recalled memory (reconsolidation)
        for r in results:
            atom_id = r.get("atom", {}).get("atom_id", "")
            if atom_id:
                atom = self.hippocampus.get(atom_id)
                if atom:
                    self.reconsolidation.on_recall(atom)

        return {
            "results": results,
            "total": len(results),
            "compressed": any(r.get("compressed") for r in results),
            "max_tokens": max_tokens,
        }

    def reconstruct(self, fragment: str, top_k: int = 0) -> dict:
        """Mode complete: reconstruct complete memory from given fragments."""
        self.mark_active()
        results = self.recall_engine.reconstruct(fragment, top_k=top_k)
        return {"results": results, "total": len(results)}

    def query_fact(self, entity: str) -> dict:
        """Query FactStore about entity knowledge."""
        facts = self.recall_engine.query_knowledge(entity)
        return {"entity": entity, "facts": facts, "total": len(facts)}

    def reason_facts(self, entities: List[str]) -> dict:
        """Cross-entity inference."""
        facts = self.recall_engine.reason_knowledge(entities)
        return {"entities": entities, "facts": facts, "total": len(facts)}

    # ── sleepconsolidation ──

    def sleep(self) -> dict:
        """
        Manually trigger sleep consolidation.

        Returns:
            consolidation report
        """
        # First execute stability decay
        all_atoms = self.hippocampus.all()
        decay_result = self.reconsolidation.decay_stability(all_atoms)

        # executeconsolidation
        result = self.sleep_engine.consolidate(self.hippocampus, self.amygdala)

        result["decay"] = decay_result

        if self.scheduler:
            self.scheduler.mark_sleep_done()

        return result

    def check_auto_sleep(self) -> Optional[dict]:
        """
        Check schedule and auto trigger sleep (driven by SleepDaemon).

        Returns:
            consolidation report (if triggered), None (if not needed)
        """
        if not self.scheduler:
            return None
        if self.scheduler.should_sleep():
            return self.sleep()
        return None

    # ── statequery ──

    def buffer_status(self) -> dict:
        return self.hippocampus.status()

    # ── Backward compatible API (v0.2 → v0.3 transition) ──

    def store_text(self, content: str,
                   entities: Optional[List[str]] = None,
                   source: str = "api") -> str:
        """Backward compatible: old store_text interface, return memory_id."""
        result = self._do_store(
            content=content,
            source=source or "api",
            tags=entities,
        )
        return result.get("atom_id", "")

    def search(self, query: str, limit: int = 10) -> List[MemoryAtom]:
        """Backward compatible: return raw MemoryAtom list."""
        results = self.recall(query, top_k=limit)
        atoms = []
        for r in results.get("results", []):
            atom_data = r.get("atom", {})
            atom_id = atom_data.get("atom_id", "")
            if atom_id:
                atom = self.hippocampus.get(atom_id)
                if atom:
                    atoms.append(atom)
        return atoms

    def snapshot(self, limit: int = 10) -> dict:
        """Backward compatible: get most important recent memory snapshot."""
        top = self.store.get_top_scored(limit)
        recent = self.store.get_recent(limit)
        return {
            "top_scored": [a.to_dict() for a in top],
            "recent": [a.to_dict() for a in recent[:5]],
            "stats": self.store.stats(),
        }

    def consolidate(self) -> dict:
        """backward compatible: trigger sleep consolidation and return concise result."""
        return self.sleep()

    def get_stats(self) -> dict:
        """backward compatible: completesystemstatistics. """
        return self.overall_status()

    def emotional_state(self) -> dict:
        """when memory system emotion overview."""
        atoms = self.hippocampus.all()
        if not atoms:
            return {"avg_emotion": 0, "positive": 0, "negative": 0, "neutral": len(atoms)}
        emotions = [a.emotion for a in atoms]
        return {
            "avg_emotion": round(sum(emotions) / len(emotions), 3),
            "positive": sum(1 for e in emotions if e > 0.1),
            "negative": sum(1 for e in emotions if e < -0.1),
            "neutral": sum(1 for e in emotions if -0.1 <= e <= 0.1),
            "total": len(atoms),
        }

    def overall_status(self) -> dict:
        """completesystemstate. """
        return {
            "hippocampus": self.hippocampus.status(),
            "emotional": self.emotional_state(),
            "fact_store": self.fact_store.stats(),
            "sleep_cycles": self.sleep_engine._cycles_completed,
            "amygdala_weights": self.amygdala.weights(),
            "schedule": self.scheduler.status() if self.scheduler else None,
        }

    def get_context_pressure(self) -> float:
        """Return context window pressure (0.0–1.0).
        Base MemorySystem does not track context windows; returns 0.0.
        The integration layer (core/memory_integration.py) provides the real value.
        """
        return 0.0

    def get_memory_conflict_rate(self) -> float:
        """Memory conflict rate based on archived atom ratio 0.0-1.0."""
        try:
            atoms = self.hippocampus.all()
            total = len(atoms)
            if total == 0:
                return 0.0
            archived = sum(1 for a in atoms if getattr(a, 'is_archived', False))
            return min(1.0, archived / total)
        except Exception:
            return 0.0

    # ── Maintenance ──

    def clear_all(self):
        """Clear all memory (use with caution)."""
        self.hippocampus.clear()
        logger.warning("MemorySystem: all hippocampus data cleared")

    # ── internalcallback ──

    def _auto_sleep_handler(self) -> dict:
        """Hippocampus is full, auto trigger sleep."""
        logger.info("Hippocampus is full, triggering auto sleep consolidation...")
        result = self.sleep()
        result["trigger"] = "capacity_reached"
        return result

    def _on_pattern_extracted(self, fact: MemoryAtom):
        """Sleep generates abstract mode callback."""
        logger.info(f"Sleep generated new abstract mode: {fact.content[:80]}...")
