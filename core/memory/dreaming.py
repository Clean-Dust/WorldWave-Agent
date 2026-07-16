"""
core/memory/dreaming.py — Async dreaming over atom store (v-next)

- Does not block chat turn (queue + background worker)
- Crawl atoms → fill gaps → reorganize → deeper inference → summaries / peer cards
- Outputs to agent/memories/dreaming/ + may update atoms
- Kill switch: WW_DREAMING_ENABLED (default ON; cheap no-op if empty)
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ww.memory.dreaming")


def dreaming_enabled() -> bool:
    """WW_DREAMING_ENABLED default ON (1/true/yes/on). Off: 0/false/no/off."""
    raw = os.environ.get("WW_DREAMING_ENABLED")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disabled")


@dataclass
class DreamJob:
    kind: str = "full"  # full | crawl | peer
    payload: Dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.time)
    job_id: str = ""


class DreamingWorker:
    """Background dreaming worker. enqueue() never blocks on dream work."""

    def __init__(
        self,
        atom_store: Any = None,
        ltm: Any = None,
        *,
        idle_poll_s: float = 0.05,
        auto_start: bool = True,
    ):
        self.atom_store = atom_store
        self.ltm = ltm
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._idle_poll_s = idle_poll_s
        self._runs_completed = 0
        self._last_result: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self.on_complete: Optional[Callable[[dict], None]] = None
        if auto_start and dreaming_enabled():
            self.start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="ww-dreaming",
            daemon=True,
        )
        self._thread.start()
        logger.info("Dreaming worker started (enabled=%s)", dreaming_enabled())

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        # Wake queue
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def enqueue(self, kind: str = "full", payload: Optional[dict] = None) -> dict:
        """Non-blocking enqueue. Returns immediately with job meta.

        If dreaming disabled, returns {skipped: True} without queueing heavy work.
        """
        if not dreaming_enabled():
            return {"queued": False, "skipped": True, "reason": "WW_DREAMING_ENABLED=off"}
        job = DreamJob(kind=kind, payload=payload or {})
        job.job_id = f"dream-{int(time.time() * 1000)}-{self._q.qsize()}"
        self._q.put(job)
        if not self._thread or not self._thread.is_alive():
            self.start()
        return {"queued": True, "job_id": job.job_id, "queue_size": self._q.qsize()}

    def enqueue_idle(self) -> dict:
        """Timer/idle path: only run if there is atom work."""
        if not dreaming_enabled():
            return {"queued": False, "skipped": True}
        n = 0
        if self.atom_store is not None:
            try:
                n = len(self.atom_store.all())
            except Exception:
                n = 0
        if n == 0:
            return {"queued": False, "skipped": True, "reason": "empty_atoms"}
        return self.enqueue("full", {"trigger": "idle", "atom_count": n})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=self._idle_poll_s)
            except queue.Empty:
                continue
            if job is None:
                continue
            if not isinstance(job, DreamJob):
                continue
            try:
                result = self._run(job)
                with self._lock:
                    self._runs_completed += 1
                    self._last_result = result
                if self.on_complete:
                    try:
                        self.on_complete(result)
                    except Exception as e:
                        logger.warning("dream on_complete failed: %s", e)
            except Exception as e:
                logger.error("Dreaming job failed: %s", e)
                with self._lock:
                    self._last_result = {"ok": False, "error": str(e)}

    def _run(self, job: DreamJob) -> dict:
        """Synchronous dream pipeline (runs on worker thread only)."""
        t0 = time.time()
        atoms = []
        if self.atom_store is not None:
            try:
                atoms = list(self.atom_store.all())
            except Exception as e:
                logger.warning("dream crawl atoms failed: %s", e)

        # Cheap no-op if empty
        if not atoms:
            return {
                "ok": True,
                "noop": True,
                "reason": "empty",
                "job_id": job.job_id,
                "duration_ms": int((time.time() - t0) * 1000),
            }

        # 1) Crawl / reorganize by net + entity
        by_net: Dict[str, int] = {}
        by_entity: Dict[str, List[Any]] = {}
        gaps: List[str] = []
        for a in atoms:
            net = getattr(a, "logical_net", "experience")
            by_net[net] = by_net.get(net, 0) + 1
            for ent in getattr(a, "entities", None) or []:
                by_entity.setdefault(ent, []).append(a)

        # 2) Fill gaps: entities with single fact → note thin coverage
        for ent, lst in by_entity.items():
            if len(lst) == 1:
                gaps.append(f"thin_entity:{ent}")

        # 3) Deeper inference: co-occurrence → Observation atom (no LLM)
        new_observations = []
        if self.atom_store is not None and len(by_entity) >= 2:
            # Pair entities that share topic_id
            topic_ents: Dict[str, set] = {}
            for a in atoms:
                tid = getattr(a, "topic_id", "") or ""
                if not tid:
                    continue
                for ent in getattr(a, "entities", None) or []:
                    topic_ents.setdefault(tid, set()).add(ent)
            for tid, ents in topic_ents.items():
                if len(ents) < 2:
                    continue
                ent_list = sorted(ents)[:6]
                content = (
                    f"Observation: entities {', '.join(ent_list)} co-occur "
                    f"in topic {tid[:8]} (dream inference)."
                )
                try:
                    from .atom_nets import MemoryAtomV2

                    obs = MemoryAtomV2(
                        content=content,
                        logical_net="observation",
                        source="dreaming",
                        topic_id=tid,
                        entities=ent_list,
                        confidence=0.55,
                        evidence=[
                            getattr(a, "atom_id", "")
                            for a in atoms
                            if getattr(a, "topic_id", "") == tid
                        ][:8],
                    )
                    self.atom_store.add(obs)
                    new_observations.append(obs.atom_id)
                except Exception as e:
                    logger.debug("dream observation create failed: %s", e)

        # 4) Summaries + Peer cards → LTM dreaming/
        peer_cards = []
        summary_uri = ""
        if self.ltm is not None:
            # Summary of crawl
            lines = [
                f"# Dream summary @ {time.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                f"Atoms crawled: {len(atoms)}",
                f"By net: {by_net}",
                f"Entities: {len(by_entity)}",
                f"Gaps: {len(gaps)}",
                f"New observations: {len(new_observations)}",
            ]
            if gaps[:10]:
                lines.append("## Gaps")
                lines.extend(f"- {g}" for g in gaps[:10])
            try:
                summary_uri = self.ltm.write(
                    "dreaming",
                    "\n".join(lines),
                    title=f"dream-{int(time.time())}",
                    name=f"dream-summary-{int(time.time())}",
                    tags=["dreaming", "summary"],
                    meta={"job_id": job.job_id},
                )
            except Exception as e:
                logger.warning("dream LTM summary write failed: %s", e)

            # Peer cards for top entities
            ranked = sorted(by_entity.items(), key=lambda x: -len(x[1]))[:5]
            for ent, lst in ranked:
                facts = []
                for a in lst[:8]:
                    valid = getattr(a, "is_currently_valid", True)
                    mark = "current" if valid else "historical"
                    facts.append(f"- ({mark}) {getattr(a, 'content', '')[:200]}")
                card = f"# Peer Card: {ent}\n\n" + "\n".join(facts)
                try:
                    uri = self.ltm.write(
                        "dreaming",
                        card,
                        title=f"peer-{ent}",
                        name=f"peer-{ent}",
                        tags=["dreaming", "peer_card", ent],
                        meta={"entity": ent, "job_id": job.job_id},
                    )
                    peer_cards.append(uri)
                except Exception as e:
                    logger.debug("peer card write failed: %s", e)

        result = {
            "ok": True,
            "noop": False,
            "job_id": job.job_id,
            "atoms_crawled": len(atoms),
            "by_net": by_net,
            "gaps": gaps[:20],
            "observations": new_observations,
            "summary_uri": summary_uri,
            "peer_cards": peer_cards,
            "duration_ms": int((time.time() - t0) * 1000),
        }
        logger.info(
            "Dream complete job=%s atoms=%d peers=%d ms=%s",
            job.job_id,
            len(atoms),
            len(peer_cards),
            result["duration_ms"],
        )
        return result

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": dreaming_enabled(),
                "alive": bool(self._thread and self._thread.is_alive()),
                "queue_size": self._q.qsize(),
                "runs_completed": self._runs_completed,
                "last_result": dict(self._last_result) if self._last_result else {},
            }

    def wait_empty(self, timeout: float = 5.0) -> bool:
        """Test helper: wait until queue drained and last job finished."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty():
                # small settle for in-flight job
                time.sleep(0.05)
                if self._q.empty():
                    return True
            time.sleep(0.02)
        return self._q.empty()
