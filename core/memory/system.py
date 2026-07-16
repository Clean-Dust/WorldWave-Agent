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

from .atom import FactStore, MemoryAtom, maybe_promote_core
from .encoder import EncodingLayer
from .hippocampus import Hippocampus
from .amygdala import Amygdala
from .sleep import DailyScheduler, IdleDetector, SleepConsolidation, SleepDaemon
from .recall import RecallEngine
from .reconsolidation import Reconsolidation
from .edges import EdgeStore
from .vnext import MemoryVNext, memory_vnext_enabled

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

        # ── Edge store (knowledge graph relations) ──
        self.edges = EdgeStore(data_dir=self.data_dir)

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

        # ── Single memory system (v-next spine; default always-on) ──
        # Topic WM + labeled facts → STM → atoms → LTM → dreaming.
        # Hippocampus/sleep remain as cold-path implementation behind this API
        # (not a second product memory). WW_MEMORY_VNEXT=0 is emergency only.
        self.vnext: Optional[MemoryVNext] = None
        self._vnext_enabled = memory_vnext_enabled()
        if self._vnext_enabled:
            try:
                self.vnext = MemoryVNext(
                    data_dir=os.path.join(self.data_dir, "vnext"),
                    start_dreaming=True,
                )
                logger.info(
                    "Memory single-system (v-next) enabled (data_dir=%s/vnext)",
                    self.data_dir,
                )
            except Exception as e:
                # Kill switch residual: if init fails, keep sleep/hippocampus API
                # but do not ship dual inject of flat Entity WM as product brain.
                logger.warning(
                    "Memory v-next init failed (emergency path, no dual inject): %s",
                    e,
                )
                self.vnext = None
                self._vnext_enabled = False

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
        is_core: bool = False,
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
            is_core: if True, mark atom as core (never auto-deleted)

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
        if is_core:
            atom.is_core = True

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

    def _memory_atom_from_dict(self, d: dict) -> MemoryAtom:
        """Build a legacy MemoryAtom from a dict (v-next or API shaped)."""
        return MemoryAtom(
            content=str(d.get("content") or ""),
            atom_id=str(d.get("atom_id") or ""),
            atom_type=str(d.get("atom_type") or "semantic"),
            entities=list(d.get("entities") or []),
            source=str(d.get("source") or "vnext"),
            tags=list(d.get("tags") or []),
            is_core=bool(d.get("is_core")),
            timestamp=float(
                d.get("timestamp") or d.get("learned_at") or time.time()
            ),
            valid_from=float(d.get("valid_from") or 0.0),
            valid_until=float(d.get("valid_until") or 0.0),
            superseded_by=str(d.get("superseded_by") or ""),
            importance=float(
                d.get("importance")
                if d.get("importance") is not None
                else (d.get("confidence") if d.get("confidence") is not None else 0.5)
            ),
        )

    def _collect_vnext_result_rows(self, query: str, limit: int) -> List[dict]:
        """Recall-shaped rows from AtomNetStore + LabeledFactStore + topic STM.

        Product remember() lands in v-next only; POST /ww/memory search/recall
        must surface those hits (content includes raw values for harnesses).
        """
        if self.vnext is None:
            return []
        limit = limit if limit and limit > 0 else 5
        rows: List[dict] = []
        seen_ids: set = set()
        seen_content: set = set()

        def _push(atom_dict: dict, salience: float, source: str) -> None:
            aid = str(atom_dict.get("atom_id") or "")
            content = str(atom_dict.get("content") or "").strip()
            cl = content.lower()
            if aid and aid in seen_ids:
                return
            if cl and cl in seen_content:
                return
            if aid:
                seen_ids.add(aid)
            if cl:
                seen_content.add(cl)
            rows.append({
                "atom": atom_dict,
                "salience": round(float(salience), 3),
                "hops": 0,
                "source": source,
            })

        # 1. Atom nets — primary product store for remember()
        try:
            for a in self.vnext.atoms.current_truth(query, limit=limit):
                ad = a.to_dict()
                # Surface raw value from meta for json.dumps harnesses
                meta = ad.get("meta") if isinstance(ad.get("meta"), dict) else {}
                if meta.get("value") and meta["value"] not in (ad.get("content") or ""):
                    ad["content"] = f"{ad.get('content', '')}\n{meta['value']}".strip()
                _push(ad, float(a.confidence), "vnext_atom")
        except Exception as e:
            logger.debug("vnext atom recall failed: %s", e)

        # 2. Labeled facts (key / value match) for active entity
        try:
            eid = getattr(self.vnext, "entity_id", None) or "default"
            listed = self.vnext.list_facts(query, entity_id=eid, limit=limit)
            for k, info in (listed.get("facts") or {}).items():
                if isinstance(info, dict):
                    val = str(info.get("value", ""))
                    kind = str(info.get("kind") or "outcome")
                    is_core = bool(info.get("is_core"))
                else:
                    val = str(info)
                    kind = "outcome"
                    is_core = False
                content = f"{k}: {val}"
                _push(
                    {
                        "atom_id": f"fact:{eid}:{k}",
                        "content": content,
                        "atom_type": "semantic",
                        "entities": [k, eid],
                        "source": "vnext_fact",
                        "tags": [f"kind:{kind}"],
                        "is_core": is_core,
                        "meta": {
                            "key": k,
                            "value": val,
                            "entity_id": eid,
                            "kind": kind,
                        },
                    },
                    0.85 if is_core else 0.7,
                    "vnext_fact",
                )
        except Exception as e:
            logger.debug("vnext fact recall failed: %s", e)

        # 3. Topic STM previews (when query matches parked topics)
        try:
            for h in self.vnext.topic_stm.search(query, top_k=limit):
                preview = str(h.get("text_preview") or h.get("title") or "")
                if not preview:
                    continue
                tid = str(h.get("topic_id") or "")
                _push(
                    {
                        "atom_id": f"stm:{tid}" if tid else f"stm:{hash(preview) & 0xFFFFFFFF:08x}",
                        "content": preview,
                        "atom_type": "episodic",
                        "entities": [],
                        "source": "vnext_stm",
                        "tags": ["topic_stm"],
                        "title": h.get("title"),
                    },
                    float(h.get("composite") or h.get("bm25") or 0.5),
                    "vnext_stm",
                )
        except Exception as e:
            logger.debug("vnext stm recall failed: %s", e)

        return rows[:limit]

    def recall(self, query: str, top_k: int = 0,
               max_tokens: int = 0) -> dict:
        """Recall and query related memories.

        When v-next is enabled, merges AtomNetStore / LabeledFactStore / topic
        STM hits with the legacy hippocampus path so product remember() is
        visible via POST /ww/memory recall and search.

        Args:
            query: querytext
            top_k: returncount
            max_tokens: token budget limit (0=default, <0=unlimited)

        Returns:
            {"results": [...], "total": N, "compressed": bool, ...}
        """
        self.mark_active()
        limit = top_k if top_k > 0 else getattr(self.recall_engine, "top_k", 5)
        results = self.recall_engine.recall(query, top_k=limit,
                                            max_tokens=max_tokens)

        # Update recalled memory (reconsolidation) + opportunistic core promotion
        for r in results:
            atom_id = r.get("atom", {}).get("atom_id", "")
            if atom_id:
                atom = self.hippocampus.get(atom_id)
                if atom:
                    self.reconsolidation.on_recall(atom)
                    # Persist stability / recall bump from reconsolidation
                    self.hippocampus.update(
                        atom_id,
                        stability=atom.stability,
                        recall_count=atom.recall_count,
                        last_recalled=atom.last_recalled,
                    )
                    # Promote frequently recalled high-value atoms to is_core
                    if maybe_promote_core(
                        atom,
                        core_count=self.hippocampus.count_core(),
                        cap=self.hippocampus.cap,
                    ):
                        self.hippocampus.update(atom_id, is_core=True)
                        logger.info(
                            "Promoted atom %s to is_core (recall=%d, stab=%.2f, imp=%.2f)",
                            atom_id[:8], atom.recall_count, atom.stability, atom.importance,
                        )

        # Merge v-next product hits (prefer first so atom_hit harnesses succeed)
        vnext_rows = self._collect_vnext_result_rows(query, limit)
        if vnext_rows:
            seen_ids: set = set()
            seen_content: set = set()
            merged: List[dict] = []
            for r in vnext_rows + list(results):
                atom = r.get("atom") or {}
                aid = str(atom.get("atom_id") or "")
                content = str(atom.get("content") or "").strip().lower()
                if aid and aid in seen_ids:
                    continue
                if content and content in seen_content:
                    continue
                if aid:
                    seen_ids.add(aid)
                if content:
                    seen_content.add(content)
                merged.append(r)
            results = merged[:limit] if limit > 0 else merged

        return {
            "results": results,
            "total": len(results),
            "compressed": any(r.get("compressed") for r in results),
            "max_tokens": max_tokens,
            "vnext_hits": len(vnext_rows) if vnext_rows else 0,
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
        """Trigger cold-path consolidation (single MemorySystem API).

        Sleep consolidation remains an internal implementation behind this
        API; when v-next is active, also queues dreaming. Users never need
        two product memory stacks.
        """
        # First execute stability decay
        all_atoms = self.hippocampus.all()
        decay_result = self.reconsolidation.decay_stability(all_atoms)

        # execute consolidation (hippocampus cold path)
        result = self.sleep_engine.consolidate(self.hippocampus, self.amygdala)

        result["decay"] = decay_result

        if self.scheduler:
            self.scheduler.mark_sleep_done()

        # Map useful sleep into v-next dreaming cold path (same API surface)
        if self.vnext is not None:
            try:
                result["dream"] = self.vnext.request_dream("full")
            except Exception as e:
                result["dream"] = {"queued": False, "error": str(e)}

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
        """Search legacy hippocampus + v-next atoms/facts/STM when enabled.

        Returns MemoryAtom list whose to_dict() content includes raw stored
        values so ``value in json.dumps(search)`` works for product proves.
        """
        self.mark_active()
        atoms: List[MemoryAtom] = []
        seen_ids: set = set()
        seen_content: set = set()

        def _add(atom: Optional[MemoryAtom]) -> None:
            if atom is None:
                return
            content = (atom.content or "").strip()
            cl = content.lower()
            if atom.atom_id and atom.atom_id in seen_ids:
                return
            if cl and cl in seen_content:
                return
            if atom.atom_id:
                seen_ids.add(atom.atom_id)
            if cl:
                seen_content.add(cl)
            atoms.append(atom)

        # Product path first: AtomNetStore / labeled facts / topic STM
        for r in self._collect_vnext_result_rows(query, limit):
            _add(self._memory_atom_from_dict(r.get("atom") or {}))

        # Dual-include legacy buffer for migration
        try:
            legacy = self.recall_engine.recall(
                query, top_k=limit, max_tokens=-1
            )
        except Exception as e:
            logger.debug("legacy search recall failed: %s", e)
            legacy = []
        for r in legacy:
            atom_data = r.get("atom") or {}
            atom_id = atom_data.get("atom_id", "")
            atom = self.hippocampus.get(atom_id) if atom_id else None
            if atom is None and atom_data.get("content"):
                atom = self._memory_atom_from_dict(atom_data)
            _add(atom)

        return atoms[:limit]

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
        st = {
            "hippocampus": self.hippocampus.status(),
            "emotional": self.emotional_state(),
            "fact_store": self.fact_store.stats(),
            "sleep_cycles": self.sleep_engine._cycles_completed,
            "amygdala_weights": self.amygdala.weights(),
            "schedule": self.scheduler.status() if self.scheduler else None,
            "vnext_enabled": bool(self._vnext_enabled and self.vnext),
        }
        if self.vnext is not None:
            try:
                st["vnext"] = self.vnext.status()
            except Exception as e:
                st["vnext_error"] = str(e)
        return st

    # ── Memory v-next convenience API ──

    def ingest_turn(
        self,
        role: str,
        content: str,
        *,
        new_topic: bool = False,
        topic_title: str = "",
    ) -> dict:
        """Passive lossless track: land turn into topic WM + experience atom."""
        if self.vnext is None:
            # Fallback: store as episodic atom only
            return self._do_store(
                content=f"{role}: {content}",
                source="passive",
                atom_type="episodic",
            )
        return self.vnext.ingest_turn(
            role, content, new_topic=new_topic, topic_title=topic_title
        )

    def recall_vnext(self, query: str, top_k: int = 5) -> dict:
        """Topic STM + LTM + atom freshness recall (v-next)."""
        if self.vnext is None:
            return self.recall(query, top_k=top_k)
        return self.vnext.recall(query, top_k=top_k)

    def memory_context_block(self, query: str = "", entity_id: str = "") -> str:
        """Single non-system memory picture (labeled facts + topic + recall).

        Prompt isolation: persona stays in system; this is context only.
        """
        if self.vnext is None:
            return ""
        if entity_id:
            try:
                self.vnext.set_entity(entity_id)
            except Exception:
                pass
        return self.vnext.inject_for_turn(query, entity_id=entity_id or "")

    def request_dream(self) -> dict:
        if self.vnext is None:
            return {"queued": False, "skipped": True, "reason": "vnext_disabled"}
        return self.vnext.request_dream()

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
