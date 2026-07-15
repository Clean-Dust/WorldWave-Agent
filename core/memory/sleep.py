"""ww/core/memory/sleep.py — sleep consolidation + dynamic idle + orphan GC + semantic fusion

sleep consolidation (Sleep Consolidation) is a key maintenance phase of the memory system:

Phase 1: Strengthen high-score memories (high salience, links become stronger)
Phase 2: Prune weak links (low score fades out, skip is_core)
Phase 3: Abstract common mode + Fact Hegemony + Semantic Synthesis
Phase 4: Hebbian co-occurrence learning
Phase 5: GC reclaims orphan nodes

Additionally contains dual trigger mechanism of DailyScheduler (daily fixed) and IdleDetector (dynamic idle).
"""

from __future__ import annotations
import json
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Callable, List, Optional, Set, Tuple

from .atom import FactStore, MemoryAtom
from .amygdala import Amygdala
from .hippocampus import Hippocampus

logger = logging.getLogger("ww.memory.sleep")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
MEMORY_DIR = os.path.join(_WW_CFG, "memory")

# archive.jsonl write lock (prevent race condition between SleepDaemon background thread and main thread)
_archive_lock = threading.Lock()


class SleepConsolidation:
    """sleepconsolidationengine. 

    each cycle executes five phases:
    Phase 1: Link intensity adjustment (skip is_core)
    Phase 2: Weak link pruning (skip is_core)
    Phase 3: Abstract mode + Fact Hegemony + Semantic Synthesis
    Phase 4: Hebbian learning
    Phase 5: GC orphan reclamation
    """

    def __init__(
        self,
        strengthen_threshold: float = 0.6,
        prune_threshold: float = 0.15,
        hebb_lr: float = 0.05,
        abstraction_min_similar: int = 3,
        similarity_threshold: float = 0.7,
        hegemony_trust_gap: float = 0.4,
        synthesis_trust_threshold: float = 0.7,  # Both trust values > this value trigger semantic fusion
        gc_salience_threshold: float = 0.1,      # Orphan reclamation salience threshold
        gc_age_days: float = 30.0,                # Orphan reclamation age threshold (days)
        data_dir: str = "",
        fact_store: Optional[FactStore] = None,
        on_synthesis_request: Optional[Callable[[str, str], str]] = None,  # LLM mediation
    ):
        self.strengthen_threshold = strengthen_threshold
        self.prune_threshold = prune_threshold
        self.hebb_lr = hebb_lr
        self.abstraction_min_similar = abstraction_min_similar
        self.similarity_threshold = similarity_threshold
        self.hegemony_trust_gap = hegemony_trust_gap
        self.synthesis_trust_threshold = synthesis_trust_threshold
        self.gc_salience_threshold = gc_salience_threshold
        self.gc_age_days = gc_age_days
        self.data_dir = data_dir or MEMORY_DIR
        self.fact_store = fact_store
        self.on_synthesis_request = on_synthesis_request

        self._cycles_completed: int = 0
        self._links_pruned: int = 0
        self._patterns_extracted: int = 0
        self._hegemony_downgraded: int = 0
        self._synthesis_count: int = 0
        self._gc_removed: int = 0

        self.on_pattern_extracted: Optional[Callable[[MemoryAtom], None]] = None

    # ── consolidate ──

    def consolidate(self, hippocampus: Hippocampus, amygdala: Amygdala) -> dict:
        atoms = hippocampus.all()
        if len(atoms) < 3:
            return {"status": "skipped", "reason": "insufficient_atoms"}

        stats = {
            "total_atoms": len(atoms),
            "links_before": sum(len(a.links) for a in atoms),
        }

        self._phase_strengthen(atoms, amygdala)
        pruned = self._phase_prune(atoms)
        stats["links_pruned"] = pruned

        new_facts, hegemony_count, synthesis_count = self._phase_abstract(atoms)
        stats["patterns_extracted"] = len(new_facts)
        stats["hegemony_downgraded"] = hegemony_count
        stats["synthesis_count"] = synthesis_count
        self._hegemony_downgraded += hegemony_count
        self._synthesis_count += synthesis_count

        self._phase_hebbian(atoms)
        stats["hebb_links_updated"] = sum(
            1 for a in atoms if a.timestamp > time.time() - 60
        )

        # Phase 5: GC (orphan + age + low salience; never core/immutable)
        gc_count = self._phase_gc(atoms, hippocampus, amygdala)
        stats["gc_removed"] = gc_count
        self._gc_removed += gc_count

        # Phase 6: REM Dreaming — synthetic data curriculum + variational rehearsal
        if self.on_synthesis_request:
            dream_stats = self._phase_rem_dream(atoms, hippocampus, amygdala)
            stats["dream_scenarios"] = dream_stats.get("scenarios_generated", 0)
            stats["dream_insights"] = dream_stats.get("insights_stored", 0)
        else:
            stats["dream_scenarios"] = 0
            stats["dream_insights"] = 0

        stats["links_after"] = sum(len(a.links) for a in atoms)
        hippocampus.save()

        for fact in new_facts:
            if self.on_pattern_extracted:
                self.on_pattern_extracted(fact)
            hippocampus.store(fact)

        self._cycles_completed += 1
        stats["cycle"] = self._cycles_completed
        stats["status"] = "completed"
        return stats

    # ── Phase 1: Strengthen (skip core) ──

    def _phase_strengthen(self, atoms: List[MemoryAtom], amygdala: Amygdala):
        for atom in atoms:
            if atom.is_core:  # Core is immune to strengthening (already stable enough)
                continue
            if atom.is_immutable:  # Code memory — never modify
                continue
            salience = amygdala.score(atom)
            if salience >= self.strengthen_threshold:
                boost = salience * 0.1
                for target_id in list(atom.links.keys()):
                    atom.links[target_id] = min(1.0, atom.links[target_id] + boost)

    # ── Phase 2: Prune (skip core links) ──

    def _phase_prune(self, atoms: List[MemoryAtom]) -> int:
        pruned = 0
        for atom in atoms:
            if atom.is_immutable:  # Code memory — never prune
                continue
            to_remove = []
            for target_id, strength in atom.links.items():
                if strength < self.prune_threshold:
                    # Check if goal is core (core links cannot be pruned)
                    # Use heuristic if goal is core: if goal is core, keep link
                    # We cannot directly know if target atom is_core, therefore skip
                    to_remove.append(target_id)
            for target_id in to_remove:
                del atom.links[target_id]
                pruned += 1
            # Skip link pruning for core itself
            if atom.is_core:
                pruned -= len(to_remove)  # Rollback
                for target_id in to_remove:
                    atom.links[target_id] = self.prune_threshold  # Reset to threshold position
        self._links_pruned += pruned
        return pruned

    # ── Phase 3: Abstract + Hegemony + Synthesis ──

    def _entities_overlap(self, a: MemoryAtom, b: MemoryAtom) -> float:
        if not a.entities or not b.entities:
            return 0.0
        set_a = set(a.entities)
        set_b = set(b.entities)
        union = set_a | set_b
        if not union:
            return 0.0
        return len(set_a & set_b) / len(union)

    def _phase_abstract(
        self, atoms: List[MemoryAtom]
    ) -> Tuple[List[MemoryAtom], int, int]:
        """Abstract mode retrieve + Fact Hegemony + Semantic Synthesis.

        Fact Hegemony: Large trust disparity contradiction → downgrade old fact
        Semantic Synthesis: Both trust values high contradiction → trigger LLM mediation to generate synthetic fact
        """
        entity_atoms = defaultdict(list)
        for atom in atoms:
            if atom.is_immutable:  # Code memory — never abstract
                continue
            for ent in atom.entities:
                entity_atoms[ent].append(atom)

        seen_pairs: Set[Tuple[str, str]] = set()
        clusters = []

        for atom in atoms:
            if atom.is_immutable:  # Code memory — never abstract
                continue
            for other in atoms:
                if atom.atom_id == other.atom_id:
                    continue
                pair = tuple(sorted([atom.atom_id, other.atom_id]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                overlap = self._entities_overlap(atom, other)
                if overlap >= self.similarity_threshold:
                    clusters.append((atom, other))

        if len(clusters) < self.abstraction_min_similar:
            return [], 0, 0

        entity_counts = defaultdict(int)
        for a, b in clusters:
            for ent in a.entities + b.entities:
                entity_counts[ent] += 1

        top_entities = sorted(entity_counts, key=entity_counts.get, reverse=True)[:3]
        if not top_entities:
            return [], 0, 0

        fact_content = f"[ABSTRACT] Common mode: about {'、'.join(top_entities)} multiple memories have high similarity"
        fact = MemoryAtom(
            content=fact_content,
            atom_type="semantic",
            entities=top_entities,
            importance=0.7,
            source="sleep",
            tags=["abstract", "consolidated"],
        )

        # ── Fact Hegemony + Semantic Synthesis ──
        hegemony_count = 0
        synthesis_count = 0

        if self.fact_store is not None:
            for entity in top_entities:
                existing = self.fact_store.probe(entity, min_trust=0.0,
                                                include_archived=True)
                # Collect high-trust facts for semantic fusion
                high_trust_facts = []

                for ef in existing:
                    if ef.get("category") != "memory_semantic":
                        continue

                    # Hegemony: Low trust → downgrade
                    if ef["trust_score"] < self.hegemony_trust_gap:
                        logger.info(
                            f"Fact Hegemony: Downgrading contradictory fact id={ef['id']} "
                            f"(trust={ef['trust_score']:.2f}) about  '{entity}'"
                        )
                        self.fact_store.update_trust(ef["id"], -0.3)
                        hegemony_count += 1

                    # Synthesis: High trust → collect for later use
                    if ef["trust_score"] >= self.synthesis_trust_threshold:
                        high_trust_facts.append(ef)

                # ── Semantic Synthesis ──
                # If there are multiple high-trust facts, and we have LLM callback
                if len(high_trust_facts) >= 2 and self.on_synthesis_request:
                    # Take the two highest trust facts for inline synthesis
                    high_trust_facts.sort(key=lambda x: -x["trust_score"])
                    fact_a = high_trust_facts[0]
                    fact_b = high_trust_facts[1]

                    # Only trigger fusion when content differs (avoid repeatedly synthesizing same fact)
                    if fact_a.get("content", "") != fact_b.get("content", ""):
                        try:
                            syn = self.on_synthesis_request(
                                fact_a.get("content", ""),
                                fact_b.get("content", ""),
                            )
                            if syn:
                                syn_fact = MemoryAtom(
                                    content=f"[SYNTHESIS] {syn}",
                                    atom_type="semantic",
                                    entities=[entity],
                                    importance=0.8,
                                    source="sleep",
                                    tags=["synthesis", "synthesized"],
                                )
                                # Mark fusion source
                                syn_fact._synthesis_sources = [
                                    fact_a["id"], fact_b["id"]
                                ]
                                fact._synthesis_facts = getattr(fact, "_synthesis_facts", [])
                                fact._synthesis_facts.append(syn_fact)

                                # ⚠️ Prevent infinite recursion: will reduce original fact trust to 0.1 (below both synthesis and hegemony thresholds)
                                # And mark is_archived, recall default does not retrieve
                                for src in [fact_a, fact_b]:
                                    self.fact_store.update_trust(src["id"], -0.99)
                                    self.fact_store.set_archived(src["id"], True)
                                synthesis_count += 1
                                logger.info(
                                    f"Semantic Synthesis: Fusing fact '{entity}' "
                                    f"({fact_a['id'][:8]} + {fact_b['id'][:8]})"
                                )
                        except Exception as e:
                            logger.error(f"Semantic Synthesis error: {e}")

        self._patterns_extracted += 1
        # Merge synthesis_facts into return list
        result_facts = [fact]
        if hasattr(fact, "_synthesis_facts") and fact._synthesis_facts:
            result_facts.extend(fact._synthesis_facts)
        return result_facts, hegemony_count, synthesis_count

    # ── Phase 4: Hebbian Learning ──

    def _phase_hebbian(self, atoms: List[MemoryAtom]):
        for i, a in enumerate(atoms):
            for j, b in enumerate(atoms):
                if i >= j:
                    continue
                if (a.last_recalled > 0 and b.last_recalled > 0 and
                        abs(a.last_recalled - b.last_recalled) < 3600):
                    boost_a = self.hebb_lr * (1.0 - a.links.get(b.atom_id, 0))
                    boost_b = self.hebb_lr * (1.0 - b.links.get(a.atom_id, 0))
                    if boost_a > 0:
                        a.links[b.atom_id] = min(1.0, a.links.get(b.atom_id, 0) + boost_a)
                    if boost_b > 0:
                        b.links[a.atom_id] = min(1.0, b.links.get(a.atom_id, 0) + boost_b)

    # ── Phase 5: orphan node GC ──

    def _phase_gc(
        self,
        atoms: List[MemoryAtom],
        hippocampus: Hippocampus,
        amygdala: Optional[Amygdala] = None,
        salience_fn: Optional[Callable[[MemoryAtom], float]] = None,
    ) -> int:
        """Reclaim orphan nodes.

        All conditions must be met before reclaim:
        1. not is_core, not is_immutable
        2. orphan (len(links) == 0)
        3. age > gc_age_days
        4. amygdala salience < gc_salience_threshold

        Always archive to archive.jsonl before remove.
        """
        now = time.time()
        removed = 0

        def _salience(atom: MemoryAtom) -> float:
            if salience_fn is not None:
                return float(salience_fn(atom))
            if amygdala is not None:
                return float(amygdala.score(atom))
            # Fallback when no scorer: use importance as proxy
            return float(atom.importance)

        orphans = []
        for atom in atoms:
            if atom.is_core:
                continue
            if atom.is_immutable:  # Code memory — never garbage collect
                continue
            if len(atom.links) > 0:
                continue
            age_days = (now - atom.timestamp) / 86400
            if age_days <= self.gc_age_days:
                continue
            sal = _salience(atom)
            if sal >= self.gc_salience_threshold:
                continue
            orphans.append((atom, age_days, sal))

        # Remove isolated nodes from hippocampus (write archive.jsonl first)
        # Also clean up associated multimodal files (screenshots, etc.)
        archive_path = os.path.join(self.data_dir, "archive.jsonl")
        for atom, age_days, sal in orphans:
            try:
                # Clean up screenshot file associated with visual memory
                if atom.visual_data:
                    _screenshot = atom.visual_data.get("screenshot_path")
                    if _screenshot and os.path.exists(_screenshot):
                        try:
                            os.remove(_screenshot)
                            logger.debug(
                                f"GC: Cleaning screenshot {_screenshot} "
                                f"(atom {atom.atom_id[:8]})"
                            )
                        except OSError as _e:
                            logger.warning(
                                f"GC: Cannot delete screenshot {_screenshot}: {_e}"
                            )

                entry = json.dumps(
                    {
                        "atom": atom.to_dict(),
                        "archived_at": time.time(),
                        "reason": "gc",
                        "salience": round(sal, 4),
                        "age_days": round(age_days, 2),
                    },
                    ensure_ascii=False,
                )
                # Append-only write to single file (JSON Lines), lock for races
                with _archive_lock:
                    with open(archive_path, "a", encoding="utf-8") as f:
                        f.write(entry + "\n")
                hippocampus.remove(atom.atom_id)
                removed += 1
                logger.info(
                    f"GC: Reclaiming orphan node {atom.atom_id[:8]} "
                    f"(age={round(age_days, 1)}d, sal={round(sal, 3)})"
                )
            except Exception as e:
                logger.error(f"GC archiving failed {atom.atom_id[:8]}: {e}")

        return removed

    # ── Phase 6: REM Dreaming — Synthetic Data Curriculum ──

    def _phase_rem_dream(
        self,
        atoms: List[MemoryAtom],
        hippocampus: Hippocampus,
        amygdala: Amygdala,
    ) -> dict:
        """REM sleep dreaming phase — synthetic data curriculum generation.

        During REM sleep, the brain rehearses and generalizes experiences.
        This phase identifies knowledge gaps from high-salience memories,
        generates synthetic edge-case scenarios, and stores generalized insights.
        """
        if not self.on_synthesis_request:
            return {"scenarios_generated": 0, "insights_stored": 0}

        scored = amygdala.rank(atoms, top_k=15)
        if len(scored) < 3:
            return {"scenarios_generated": 0, "insights_stored": 0}

        top_atoms = [a for a, _ in scored[:10]]
        failure_atoms = [a for a in top_atoms
                         if any(t in a.tags for t in ["error", "failure", "crash"])
                         or a.emotion < -0.3]
        isolated_atoms = [a for a in top_atoms
                          if len(a.links) <= 1 and a.is_core is not True]
        dream_atoms = failure_atoms[:3] + isolated_atoms[:2]
        if not dream_atoms:
            return {"scenarios_generated": 0, "insights_stored": 0}

        scenarios_generated = 0
        insights_stored = 0

        for dream_source in dream_atoms[:5]:
            try:
                prompt = (
                    "You are in REM sleep, rehearsing and generalizing from "
                    "a past experience.\n\n"
                    f"Original experience: {dream_source.content[:300]}\n"
                    f"Entities: {', '.join(dream_source.entities[:5])}\n"
                    f"Emotional tone: {'negative' if dream_source.emotion < 0 else 'positive'}\n\n"
                    "Generate 2-3 synthetic edge-case scenarios that test "
                    "the boundaries of this knowledge. These should be "
                    "situations NOT directly experienced, but which follow "
                    "logically from the pattern.\n\n"
                    "For each scenario, provide:\n"
                    "1. scenario_description: what happens\n"
                    "2. expected_approach: how to handle it\n"
                    "3. insight: what is learned\n\n"
                    "Respond in plain text, one scenario per paragraph."
                )
                dream_output = self.on_synthesis_request(prompt, "")
                if not dream_output:
                    continue
                scenarios_generated += 1

                for line in dream_output.split("\n"):
                    line = line.strip()
                    if line.startswith("3.") or "insight" in line.lower():
                        insight_text = line.split(".", 1)[-1].strip()[:300]
                        if insight_text:
                            dream_atom = MemoryAtom(
                                content=f"[REM_DREAM] {insight_text}",
                                atom_type="semantic",
                                entities=dream_source.entities[:3],
                                importance=0.5 + abs(dream_source.emotion) * 0.3,
                                source="sleep_rem",
                                tags=["dream", "synthetic", "generalized"],
                            )
                            hippocampus.store(dream_atom)
                            insights_stored += 1

                if insights_stored == 0 and len(dream_output) > 20:
                    dream_atom = MemoryAtom(
                        content=f"[REM_DREAM] Variational rehearsal: {dream_output[:400]}",
                        atom_type="semantic",
                        entities=dream_source.entities[:3],
                        importance=0.4 + abs(dream_source.emotion) * 0.2,
                        source="sleep_rem",
                        tags=["dream", "rehearsal"],
                    )
                    hippocampus.store(dream_atom)
                    insights_stored += 1

            except Exception as e:
                logger.warning(
                    f"REM dreaming failed for atom {dream_source.atom_id[:8]}: {e}"
                )
                continue

        return {
            "scenarios_generated": scenarios_generated,
            "insights_stored": insights_stored,
        }


class DailyScheduler:
    def __init__(
        self,
        scheduled_hour: int = 3,
        cooldown: float = 82800.0,
    ):
        assert 0 <= scheduled_hour <= 23
        self.scheduled_hour = scheduled_hour
        self.cooldown = cooldown
        self._last_sleep: float = 0.0

    def should_sleep(self, _hippocampus=None) -> bool:
        now = time.localtime()
        if now.tm_hour != self.scheduled_hour:
            return False
        if time.time() - self._last_sleep < self.cooldown:
            return False
        return True

    def mark_sleep_done(self):
        self._last_sleep = time.time()

    def status(self) -> dict:
        now = time.localtime()
        next_hour = (self.scheduled_hour - now.tm_hour) % 24
        return {
            "scheduled_hour": self.scheduled_hour,
            "current_hour": now.tm_hour,
            "hours_until_next": next_hour,
            "since_last_sleep": round((time.time() - self._last_sleep) / 3600, 1),
            "mode": "daily_scheduled",
        }


class IdleDetector:
    def __init__(
        self,
        idle_threshold_minutes: float = 30.0,
    ):
        self.idle_threshold_minutes = idle_threshold_minutes
        self._last_activity: float = time.time()
        self._last_sleep: float = 0.0

    def mark_active(self):
        self._last_activity = time.time()

    def should_sleep(self, _hippocampus=None) -> bool:
        if self.idle_threshold_minutes <= 0:
            return False
        elapsed = time.time() - self._last_activity
        threshold_sec = self.idle_threshold_minutes * 60.0
        if elapsed < threshold_sec:
            return False
        if time.time() - self._last_sleep < threshold_sec * 0.5:
            return False
        return True

    def mark_sleep_done(self):
        self._last_sleep = time.time()
        self._last_activity = time.time()

    def status(self) -> dict:
        elapsed = time.time() - self._last_activity
        threshold_sec = self.idle_threshold_minutes * 60.0 if self.idle_threshold_minutes > 0 else float('inf')
        return {
            "idle_threshold_minutes": self.idle_threshold_minutes,
            "idle_seconds": round(elapsed, 1),
            "idle_minutes": round(elapsed / 60, 1),
            "will_sleep_in_minutes": round(max(0, (threshold_sec - elapsed) / 60), 1) if self.idle_threshold_minutes > 0 else -1,
            "since_last_sleep_minutes": round((time.time() - self._last_sleep) / 60, 1),
            "mode": "idle_detection",
        }


class SleepDaemon:
    def __init__(
        self,
        sleep_engine: SleepConsolidation,
        hippocampus: Hippocampus,
        amygdala: Amygdala,
        scheduler: Optional[DailyScheduler] = None,
        idle_detector: Optional[IdleDetector] = None,
        poll_interval: float = 60.0,
        on_sleep_complete: Optional[Callable[[dict], None]] = None,
    ):
        self.sleep_engine = sleep_engine
        self.hippocampus = hippocampus
        self.amygdala = amygdala
        self.scheduler = scheduler
        self.idle_detector = idle_detector
        self.poll_interval = poll_interval
        self.on_sleep_complete = on_sleep_complete

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning("SleepDaemon is running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="sleep-daemon",
        )
        self._thread.start()
        triggers = []
        if self.scheduler:
            triggers.append(f"Daily {self.scheduler.scheduled_hour}:00")
        if self.idle_detector and self.idle_detector.idle_threshold_minutes > 0:
            triggers.append(f"Idle {self.idle_detector.idle_threshold_minutes} minutes")
        logger.info(f"SleepDaemon start (triggers: {', '.join(triggers) or 'none'})")

    def stop(self, timeout: float = 5.0):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            logger.info("SleepDaemon  stop")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"SleepDaemon tick error: {e}")
            self._stop_event.wait(self.poll_interval)

    def _tick(self):
        if self.scheduler and self.scheduler.should_sleep():
            logger.info(f"SleepDaemon: schedule {self.scheduler.scheduled_hour}:00 triggered")
            self._do_sleep(trigger="daily_scheduled")
            return

        if self.idle_detector and self.idle_detector.should_sleep():
            idle_min = round((time.time() - self.idle_detector._last_activity) / 60, 1)
            logger.info(f"SleepDaemon: idle {idle_min} minutes triggered")
            self._do_sleep(trigger="idle_detected")
            return

    def _do_sleep(self, trigger: str):
        """Execute one sleep consolidation (skip is_core stability decay)."""
        all_atoms = self.hippocampus.all()
        decay_count = 0
        for atom in all_atoms:
            if atom.is_core:  # Core memory is immune to decay
                continue
            age_days = (time.time() - atom.timestamp) / 86400
            decay = 0.02 * age_days
            if decay > 0 and atom.stability > 1.0:
                atom.stability = max(1.0, atom.stability - decay)
                decay_count += 1

        result = self.sleep_engine.consolidate(self.hippocampus, self.amygdala)
        result["trigger"] = trigger
        result["decay"] = {"decayed_count": decay_count}

        if self.scheduler:
            self.scheduler.mark_sleep_done()
        if self.idle_detector:
            self.idle_detector.mark_sleep_done()

        logger.info(
            f"SleepDaemon completed ({trigger}): {result.get('status', '?')} "
            f"pruned={result.get('links_pruned', 0)} "
            f"abstract={result.get('patterns_extracted', 0)} "
            f"hegemony={result.get('hegemony_downgraded', 0)} "
            f"synthesis={result.get('synthesis_count', 0)} "
            f"gc={result.get('gc_removed', 0)}"
        )

        if self.on_sleep_complete:
            try:
                self.on_sleep_complete(result)
            except Exception as e:
                logger.error(f"SleepDaemon callback error: {e}")
