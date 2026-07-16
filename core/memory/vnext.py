"""
core/memory/vnext.py — Memory v-next pipeline orchestrator

WM (single topic) → TopicHippocampus (STM/BM25) → Atoms (4 nets) → LTM VFS → Dreaming

Write tracks:
  1. Hot agent tools — remember/recall/reflect (kind explicit)
  2. Passive lossless — conversation turns as Experience raw (no dual-LLM)
  3. Cold — dreaming / dialectic safety net

Feature flag: WW_MEMORY_VNEXT (default ON).
Optional modules default OFF: RRF, cross-encoder, HRR (fail-loud if partial).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from .atom_nets import (
    AtomNetStore,
    MemoryAtomV2,
    extract_atoms_from_text,
    extract_atoms_from_topic,
)
from .dreaming import DreamingWorker, dreaming_enabled
from .ltm_vfs import ContentTier, LTMVFS
from .topic import Topic, WorkingTopicStore, looks_like_topic_switch
from .topic_stm import TopicHippocampus, evaluate_topic

logger = logging.getLogger("ww.memory.vnext")


def memory_vnext_enabled() -> bool:
    """WW_MEMORY_VNEXT default ON. Off: 0/false/no/off."""
    raw = os.environ.get("WW_MEMORY_VNEXT")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disabled")


def optional_module_enabled(name: str) -> bool:
    """Optional retrieval modules default OFF."""
    env_map = {
        "rrf": "WW_MEMORY_RRF",
        "cross_encoder": "WW_MEMORY_CROSS_ENCODER",
        "hrr": "WW_MEMORY_HRR",
    }
    key = env_map.get(name.lower(), f"WW_MEMORY_{name.upper()}")
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class HRRUnavailableError(RuntimeError):
    """Fail-loud when HRR requested but incomplete (no silent degrade)."""


class MemoryVNext:
    """Topic-centric memory pipeline (product slice)."""

    def __init__(
        self,
        data_dir: str = "",
        *,
        start_dreaming: bool = True,
        topic_cap: Optional[int] = None,
    ):
        base = data_dir or os.path.join(
            os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww")), "memory", "vnext"
        )
        self.data_dir = base
        os.makedirs(self.data_dir, exist_ok=True)

        self.atoms = AtomNetStore(data_dir=self.data_dir)
        self.ltm = LTMVFS(data_dir=self.data_dir)

        self.topic_stm = TopicHippocampus(
            data_dir=os.path.join(self.data_dir, "topic_stm"),
            cap=topic_cap,
            atom_extract=self._atom_extract_topic,
            on_promote=self._on_promote_topic,
        )

        self.wm = WorkingTopicStore(
            data_dir=os.path.join(self.data_dir, "wm"),
            on_switch=self._on_wm_switch,
            atom_extract=self._atom_extract_topic,
        )

        self.dreaming: Optional[DreamingWorker] = None
        if start_dreaming and dreaming_enabled():
            self.dreaming = DreamingWorker(
                atom_store=self.atoms,
                ltm=self.ltm,
                auto_start=True,
            )

        # Core/persona reserved slice (never topic-evicted from identity inject)
        self._core_facts: Dict[str, str] = {}

    # ── Atom extract (shared leave/overflow path) ──

    def _atom_extract_topic(self, topic: Topic) -> List[MemoryAtomV2]:
        atoms = extract_atoms_from_topic(topic, source="topic_leave")
        if atoms:
            self.atoms.add_many(atoms)
        return atoms

    def _on_promote_topic(self, topic: Topic, atoms: List[Any]) -> None:
        try:
            uri = self.ltm.promote_topic(topic, category="experiences")
            logger.info("Promoted topic %s → LTM %s atoms=%d", topic.topic_id[:8], uri, len(atoms))
        except Exception as e:
            logger.error("LTM promote write failed: %s", e)

    def _on_wm_switch(self, topic: Topic) -> None:
        """Park entire topic (body+digests) into hippocampus; re-evaluate score."""
        if topic.is_core and topic.topic_id in {t.topic_id for t in self.topic_stm.all() if t.is_core}:
            # Still re-admit to refresh score
            pass
        result = self.topic_stm.admit(topic, reevaluate=True)
        logger.debug("WM→STM switch admit: %s", result)

    # ── Write track 1: hot tools ──

    def remember(
        self,
        key: str,
        value: str,
        *,
        kind: str = "outcome",
        is_core: bool = False,
        logical_net: str = "world",
        category: str = "",
    ) -> dict:
        """Hot-path remember: atom + optional core slice; no dual LLM."""
        content = f"{key}: {value}"
        atom = MemoryAtomV2(
            content=content,
            logical_net=logical_net if logical_net in ("world", "experience", "observation", "opinion") else "world",
            source="remember",
            entities=[key],
            tags=[f"kind:{kind}", category] if category else [f"kind:{kind}"],
            is_core=is_core,
            confidence=0.9 if is_core else 0.7,
        )
        # Supersede prior same-key current facts
        prior = self.atoms.query(text=f"{key}:", current_only=True, limit=5)
        for old in prior:
            if old.content.startswith(f"{key}:") and old.atom_id != atom.atom_id:
                self.atoms.updates(atom, old)
                break
        else:
            self.atoms.add(atom)

        if is_core:
            self._core_facts[key] = value

        # Light WM inject as turn on active topic (does not force switch)
        try:
            self.wm.append_turn("system", f"[remember:{kind}] {content}", is_core=is_core)
        except Exception:
            pass

        return {
            "status": "stored",
            "key": key,
            "atom_id": atom.atom_id,
            "kind": kind,
            "is_core": is_core,
            "logical_net": atom.logical_net,
        }

    def forget(self, key: str) -> dict:
        hits = self.atoms.query(text=f"{key}:", current_only=True, limit=20)
        n = 0
        for a in hits:
            if a.content.startswith(f"{key}:") or key in a.entities:
                a.mark_invalid()
                a.superseded_by = a.superseded_by or "forgotten"
                self.atoms.add(a)  # persist
                n += 1
        self._core_facts.pop(key, None)
        return {"status": "forgotten", "key": key, "invalidated": n}

    def reflect(self, query: str = "") -> dict:
        """Lightweight reflect over opinion/observation nets (no LLM)."""
        opinions = self.atoms.query(logical_net="opinion", text=query, limit=10)
        observations = self.atoms.query(logical_net="observation", text=query, limit=10)
        return {
            "opinions": [a.to_dict() for a in opinions],
            "observations": [a.to_dict() for a in observations],
            "core": dict(self._core_facts),
        }

    # ── Write track 2: passive lossless ──

    def ingest_turn(
        self,
        role: str,
        content: str,
        *,
        new_topic: bool = False,
        topic_title: str = "",
        auto_switch: bool = True,
    ) -> dict:
        """Land conversation turn as Experience raw + topic body (no dual LLM)."""
        switched = False
        if auto_switch and not new_topic and self.wm.active and role == "user":
            prev = self.wm.active.full_text()
            if looks_like_topic_switch(prev, content):
                new_topic = True
                topic_title = topic_title or content[:80]
                switched = True

        topic = self.wm.append_turn(
            role,
            content,
            new_topic=new_topic,
            topic_title=topic_title,
        )

        # Passive experience atom (raw, no second LLM)
        exp = MemoryAtomV2(
            content=f"{role}: {content}"[:500],
            logical_net="experience",
            source="passive",
            topic_id=topic.topic_id,
        )
        self.atoms.add(exp)

        return {
            "topic_id": topic.topic_id,
            "title": topic.title,
            "switched": switched or new_topic,
            "turns": len(topic.turns),
            "digests": len(topic.digests),
            "tokens": topic.token_estimate(),
            "experience_atom": exp.atom_id,
        }

    def switch_topic(self, title: str = "", *, is_core: bool = False) -> dict:
        prev, new_t = self.wm.switch_topic(title=title, is_core=is_core)
        return {
            "previous_id": prev.topic_id if prev else None,
            "active_id": new_t.topic_id,
            "title": new_t.title,
            "stm_count": self.topic_stm.count(),
        }

    # ── Write track 3: cold dreaming ──

    def request_dream(self, kind: str = "full") -> dict:
        if not self.dreaming:
            if not dreaming_enabled():
                return {"queued": False, "skipped": True, "reason": "disabled"}
            self.dreaming = DreamingWorker(atom_store=self.atoms, ltm=self.ltm)
        return self.dreaming.enqueue(kind)

    # ── Retrieval ──

    def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        progressive: bool = True,
    ) -> dict:
        """Hippocampus BM25 + LTM index + atom freshness filter.

        Progressive: LTM returns Abstract first.
        Invalid atoms never win as current truth.
        """
        # Optional modules
        if optional_module_enabled("hrr"):
            raise HRRUnavailableError(
                "WW_MEMORY_HRR=1 but HRR backend is not fully configured. "
                "Fail-loud: disable WW_MEMORY_HRR or install complete HRR with "
                "full write verbs (add/replace/remove). No silent FTS fallback."
            )

        stm_hits = self.topic_stm.search(query, top_k=top_k)
        atom_hits = self.atoms.current_truth(query, limit=top_k)
        ltm_tier = ContentTier.ABSTRACT if progressive else ContentTier.DETAIL
        ltm_hits = self.ltm.search(query, top_k=top_k, tier=ltm_tier)

        # Optional RRF fusion (default OFF)
        fused = None
        if optional_module_enabled("rrf"):
            fused = self._rrf_fuse(stm_hits, atom_hits, ltm_hits)

        return {
            "query": query,
            "stm": [
                {
                    "topic_id": h["topic_id"],
                    "title": h["title"],
                    "bm25": h["bm25"],
                    "composite": h["composite"],
                    "preview": h["text_preview"],
                }
                for h in stm_hits
            ],
            "atoms": [a.to_dict() for a in atom_hits],
            "ltm": ltm_hits,
            "fused": fused,
            "progressive": progressive,
            "tier": ltm_tier.value,
        }

    def _rrf_fuse(self, stm, atoms, ltm, k: int = 60) -> List[dict]:
        scores: Dict[str, float] = {}
        labels: Dict[str, dict] = {}
        for rank, h in enumerate(stm):
            key = f"stm:{h['topic_id']}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            labels[key] = {"source": "stm", "id": h["topic_id"], "title": h.get("title")}
        for rank, a in enumerate(atoms):
            key = f"atom:{a.atom_id}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            labels[key] = {"source": "atom", "id": a.atom_id, "content": a.content[:120]}
        for rank, h in enumerate(ltm):
            key = f"ltm:{h['uri']}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            labels[key] = {"source": "ltm", "uri": h["uri"], "title": h.get("title")}
        ordered = sorted(scores.items(), key=lambda x: -x[1])
        return [{**labels[i], "rrf": s} for i, s in ordered]

    def expand_ltm(self, uri: str, tier: str = "overview") -> str:
        t = ContentTier.OVERVIEW
        if tier in ("detail", "full", "l2"):
            t = ContentTier.DETAIL
        elif tier in ("abstract", "l0"):
            t = ContentTier.ABSTRACT
        return self.ltm.read(uri, tier=t)

    # ── Prompt isolation ──

    def build_context_blocks(self) -> Dict[str, str]:
        """Separate blocks: system stays persona-only; memory/peer are extra.

        Product law: retrieved memory / peer MUST NOT be dumped into system
        persona blob.
        """
        core_lines = [f"- {k}: {v}" for k, v in self._core_facts.items()]
        return {
            "system_persona_only": "",  # caller keeps persona/hard rules here
            "core_identity": "\n".join(core_lines) if core_lines else "",
            "working_topic": self.wm.inject_block(),
            "memory_retrieved": "",  # filled by recall at turn time
            "peer_cards": "",
        }

    def inject_for_turn(self, query: str = "", max_chars: int = 4000) -> str:
        """Build non-system memory context block for this turn."""
        parts = []
        blocks = self.build_context_blocks()
        if blocks["core_identity"]:
            parts.append("## Core identity (protected)\n" + blocks["core_identity"])
        if blocks["working_topic"]:
            parts.append(blocks["working_topic"])
        if query:
            rec = self.recall(query, top_k=3, progressive=True)
            mem_lines = []
            for a in rec.get("atoms") or []:
                mem_lines.append(f"- [atom/{a.get('logical_net')}] {a.get('content', '')[:200]}")
            for h in rec.get("ltm") or []:
                mem_lines.append(f"- [ltm abstract] {h.get('uri')}: {h.get('abstract', '')[:160]}")
            for h in rec.get("stm") or []:
                mem_lines.append(f"- [stm] {h.get('title')}: {h.get('preview', '')[:160]}")
            if mem_lines:
                parts.append("## Retrieved memory\n" + "\n".join(mem_lines))
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[: max_chars - 20] + "\n… [truncated]"
        return text

    # ── Status / maintenance ──

    def status(self) -> dict:
        return {
            "vnext_enabled": memory_vnext_enabled(),
            "wm": self.wm.status(),
            "topic_stm": self.topic_stm.status(),
            "atoms": self.atoms.stats(),
            "ltm": self.ltm.stats(),
            "dreaming": self.dreaming.status() if self.dreaming else {"enabled": dreaming_enabled(), "alive": False},
            "core_facts": len(self._core_facts),
            "optional": {
                "rrf": optional_module_enabled("rrf"),
                "cross_encoder": optional_module_enabled("cross_encoder"),
                "hrr": optional_module_enabled("hrr"),
            },
        }

    def close(self) -> None:
        if self.dreaming:
            self.dreaming.stop()
