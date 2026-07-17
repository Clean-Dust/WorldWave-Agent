"""
core/memory/vnext.py — Memory single-system orchestrator (v-next spine)

Product law: ONE memory system. Labeled facts + topic WM → TopicHippocampus
(STM/BM25) → Atoms (4 nets) → LTM VFS → Dreaming/sleep cold path.

Absorbed from legacy flat Entity WM:
  - Explicit kind labels (constraint/commitment/outcome/rationale)
  - is_core hard protect
  - Recency + access eviction scoring
  - Entity-scoped facts; tools write here only for product path

Write tracks:
  1. Hot agent tools — remember/forget/reflect (kind explicit, no dual LLM)
  2. Passive lossless — conversation turns as Experience raw
  3. Cold — dreaming / sleep consolidation behind MemorySystem API

Feature flag WW_MEMORY_VNEXT: default ON; emergency kill switch only
(deprecated as product mode — see docs/memory-vnext.md).
Optional modules default OFF: RRF, cross-encoder, HRR (fail-loud if partial).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from .atom_nets import (
    AtomNetStore,
    MemoryAtomV2,
    extract_atoms_from_text,
    extract_atoms_from_topic,
)
from .dreaming import DreamingWorker, dreaming_enabled
from .labeled_wm import LabeledFactStore
from .ltm_vfs import ContentTier, LTMVFS
from .topic import Topic, WorkingTopicStore, looks_like_topic_switch
from .topic_stm import TopicHippocampus, evaluate_topic

logger = logging.getLogger("ww.memory.vnext")

# Progressive inject: reserve for core/persona always
_CORE_RESERVE_CHARS = 400


def memory_vnext_enabled() -> bool:
    """WW_MEMORY_VNEXT default ON (single-system product path).

    Off values (0/false/no/off) remain an **emergency kill switch** for one
    release if init fails elsewhere — not a supported dual product mode.
    Prefer always-on; when init fails, MemorySystem falls back without
    shipping a parallel flat-WM inject path.
    """
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

        # Labeled fact WM — single SoT for kind/core/recency (absorbed legacy)
        self.facts = LabeledFactStore(
            data_dir=os.path.join(self.data_dir, "facts"),
        )
        # Active entity for tools / inject (Same Timeline coupling)
        self.entity_id: str = "default"

        self.dreaming: Optional[DreamingWorker] = None
        if start_dreaming and dreaming_enabled():
            self.dreaming = DreamingWorker(
                atom_store=self.atoms,
                ltm=self.ltm,
                auto_start=True,
            )

        # Core/persona reserved slice (mirrors facts.is_core for fast inject)
        self._core_facts: Dict[str, str] = {}

    def set_entity(self, entity_id: str) -> None:
        """Bind memory pipeline to a cognitive entity (Same Timeline)."""
        self.entity_id = entity_id or "default"
        # Hydrate core slice from labeled store
        snap = self.facts.export_snapshot(self.entity_id)
        core_keys = set(snap.get("working_memory_core") or [])
        wm = snap.get("working_memory") or {}
        self._core_facts = {k: wm[k] for k in core_keys if k in wm}

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
        entity_id: str = "",
    ) -> dict:
        """Hot-path remember: labeled WM + atom; no dual LLM.

        kind is explicit only (constraint/commitment/outcome/rationale).
        is_core hard-protects under capacity. Single store under facts/.
        """
        eid = entity_id or self.entity_id or "default"
        from core.entity_state import normalize_wm_kind

        resolved_kind = normalize_wm_kind(kind)
        fact_result = self.facts.set(
            eid,
            key,
            value,
            kind=resolved_kind,
            is_core=bool(is_core),
        )

        # Content must include raw value (and key:value) so API search/prove
        # can match with ``value in json.dumps(results)``.
        content = f"{key}: {value}"
        atom = MemoryAtomV2(
            content=content,
            logical_net=logical_net if logical_net in ("world", "experience", "observation", "opinion") else "world",
            source="remember",
            entities=[key, eid],
            tags=[f"kind:{resolved_kind}", category] if category else [f"kind:{resolved_kind}"],
            is_core=is_core,
            confidence=0.9 if is_core else 0.7,
            meta={
                "entity_id": eid,
                "kind": resolved_kind,
                "key": key,
                "value": value,
            },
        )
        # Supersede prior same-key current facts for THIS entity only
        prior = self.atoms.query(text=f"{key}:", current_only=True, limit=20)
        superseded = False
        for old in prior:
            if old.atom_id == atom.atom_id:
                continue
            if not old.content.startswith(f"{key}:"):
                continue
            old_meta = old.meta if isinstance(old.meta, dict) else {}
            old_eid = str(old_meta.get("entity_id") or "")
            old_ents = [str(e) for e in (old.entities or [])]
            if old_eid and old_eid != eid:
                continue
            if not old_eid and eid not in old_ents and eid != "default":
                continue
            self.atoms.updates(atom, old)
            superseded = True
            break
        if not superseded:
            self.atoms.add(atom)

        if is_core:
            self._core_facts[key] = value
        else:
            # Drop from core slice if re-remembered without is_core
            self._core_facts.pop(key, None)

        # Light topic annotate (does not force switch)
        try:
            self.wm.append_turn(
                "system", f"[remember:{resolved_kind}] {content}", is_core=is_core
            )
        except Exception:
            pass

        return {
            "status": "stored",
            "key": key,
            "atom_id": atom.atom_id,
            "kind": resolved_kind,
            "is_core": bool(is_core),
            "logical_net": atom.logical_net,
            "entity_id": eid,
            "previous": fact_result.get("previous"),
            "evicted": fact_result.get("evicted") or [],
        }

    def forget(self, key: str, *, entity_id: str = "") -> dict:
        eid = entity_id or self.entity_id or "default"
        was = self.facts.delete(eid, key)
        hits = self.atoms.query(text=f"{key}:", current_only=True, limit=20)
        n = 0
        for a in hits:
            if a.content.startswith(f"{key}:") or key in a.entities:
                a.mark_invalid()
                a.superseded_by = a.superseded_by or "forgotten"
                self.atoms.add(a)  # persist
                n += 1
        self._core_facts.pop(key, None)
        return {
            "status": "forgotten",
            "key": key,
            "was": was,
            "invalidated": n,
            "entity_id": eid,
        }

    def list_facts(
        self, query: str = "", *, entity_id: str = "", limit: int = 50
    ) -> dict:
        """List labeled online facts (single store)."""
        eid = entity_id or self.entity_id or "default"
        facts = self.facts.get_facts(eid)
        meta = self.facts.get_meta(eid)
        if query:
            ql = query.lower()
            facts = {
                k: v
                for k, v in facts.items()
                if ql in k.lower() or ql in v.lower()
            }
        items = list(facts.items())[:limit]
        out = {}
        for k, v in items:
            m = meta.get(k) or {}
            out[k] = {
                "value": v,
                "kind": m.get("kind", "outcome"),
                "access_count": int(m.get("access_count", 0) or 0),
                "is_core": k in self.facts.get_core(eid),
            }
        return {"facts": out, "total": len(items), "entity_id": eid}

    def reflect(self, query: str = "") -> dict:
        """Lightweight reflect over opinion/observation nets + labeled core."""
        opinions = self.atoms.query(logical_net="opinion", text=query, limit=10)
        observations = self.atoms.query(logical_net="observation", text=query, limit=10)
        eid = self.entity_id or "default"
        labeled = self.facts.get_facts(eid)
        return {
            "opinions": [a.to_dict() for a in opinions],
            "observations": [a.to_dict() for a in observations],
            "core": dict(self._core_facts),
            "labeled_facts": labeled,
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
        """Land conversation turn as Experience raw + topic body (no dual LLM).

        Auto topic split (user turns): explicit markers, long gap + subject
        change, or low lexical overlap → park current topic to STM fully,
        start new topic body.
        """
        switched = False
        switch_reason = ""
        if auto_switch and not new_topic and self.wm.active and role == "user":
            prev = self.wm.active.full_text()
            gap = 0.0
            try:
                gap = max(0.0, time.time() - float(self.wm.active.updated_at or 0))
            except (TypeError, ValueError):
                gap = 0.0
            if looks_like_topic_switch(prev, content, gap_seconds=gap):
                new_topic = True
                topic_title = topic_title or content[:80]
                switched = True
                switch_reason = "heuristic"

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
            "switch_reason": switch_reason if switched else ("forced" if new_topic else ""),
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
        Optional RRF (WW_MEMORY_RRF=1): fuse BM25 STM + atom text + labeled facts.
        """
        # Optional modules — cross-encoder / HRR fail-loud if enabled incomplete
        if optional_module_enabled("hrr"):
            raise HRRUnavailableError(
                "WW_MEMORY_HRR=1 but HRR backend is not fully configured. "
                "Fail-loud: disable WW_MEMORY_HRR or install complete HRR with "
                "full write verbs (add/replace/remove). No silent FTS fallback."
            )
        if optional_module_enabled("cross_encoder"):
            raise RuntimeError(
                "WW_MEMORY_CROSS_ENCODER=1 but cross-encoder backend is not "
                "configured. Fail-loud stub: disable the flag or install a "
                "complete reranker. Default retrieval path remains fast FTS/BM25."
            )

        stm_hits = self.topic_stm.search(query, top_k=top_k)
        atom_hits = self.atoms.current_truth(query, limit=top_k)
        ltm_tier = ContentTier.ABSTRACT if progressive else ContentTier.DETAIL
        ltm_hits = self.ltm.search(query, top_k=top_k, tier=ltm_tier)
        fact_hits = self._rank_labeled_facts(query, limit=top_k)

        # Optional RRF fusion (default OFF) — STM + atoms + labeled facts (+ LTM)
        fused = None
        if optional_module_enabled("rrf"):
            fused = self._rrf_fuse(stm_hits, atom_hits, ltm_hits, fact_hits)

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
            "facts": fact_hits,
            "fused": fused,
            "progressive": progressive,
            "tier": ltm_tier.value,
        }

    def _rank_labeled_facts(self, query: str, limit: int = 5) -> List[dict]:
        """Rank labeled facts by simple term overlap (for RRF / inject)."""
        eid = self.entity_id or "default"
        facts = self.facts.get_facts(eid)
        if not facts:
            return []
        q = (query or "").lower().strip()
        q_tokens = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", q)) if q else set()
        scored: List[tuple] = []
        for k, v in facts.items():
            blob = f"{k} {v}".lower()
            if not q_tokens:
                scored.append((0.0, k, v))
                continue
            hits = sum(1 for t in q_tokens if t in blob)
            if hits:
                scored.append((float(hits), k, v))
        scored.sort(key=lambda x: -x[0])
        return [
            {"key": k, "value": v, "score": s}
            for s, k, v in scored[:limit]
        ]

    def _rrf_fuse(self, stm, atoms, ltm, facts=None, k: int = 60) -> List[dict]:
        """Reciprocal rank fusion over STM BM25 + atom text + labeled facts (+ LTM)."""
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
        for rank, h in enumerate(ltm or []):
            key = f"ltm:{h['uri']}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            labels[key] = {"source": "ltm", "uri": h["uri"], "title": h.get("title")}
        for rank, f in enumerate(facts or []):
            key = f"fact:{f.get('key')}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            labels[key] = {
                "source": "fact",
                "key": f.get("key"),
                "value": str(f.get("value") or "")[:120],
            }
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

    def build_context_blocks(self, *, entity_id: str = "") -> Dict[str, str]:
        """Separate blocks: system stays persona-only; memory/peer are extra.

        Product law: retrieved memory / peer MUST NOT be dumped into system
        persona blob. Labeled facts live here (single picture) — not a second
        parallel EntityState flat dump.
        """
        eid = entity_id or self.entity_id or "default"
        # Bump access on inject so recency/access scoring stays meaningful
        labeled = self.facts.inject_block(eid, bump_access=True)
        core_lines = [f"- {k}: {v}" for k, v in self._core_facts.items()]
        # Also surface is_core keys from store if slice empty
        if not core_lines:
            snap = self.facts.export_snapshot(eid)
            for k in snap.get("working_memory_core") or []:
                v = (snap.get("working_memory") or {}).get(k)
                if v is not None:
                    core_lines.append(f"- {k}: {v}")
                    self._core_facts[k] = v
        return {
            "system_persona_only": "",  # caller keeps persona/hard rules here
            "core_identity": "\n".join(core_lines) if core_lines else "",
            "labeled_facts": labeled,
            "working_topic": self.wm.inject_block(),
            "memory_retrieved": "",  # filled by recall at turn time
            "peer_cards": "",
        }

    def inject_for_turn(
        self, query: str = "", max_chars: int = 4000, *, entity_id: str = ""
    ) -> str:
        """Build the single non-system memory context block for this turn.

        Progressive inject discipline:
          - Core / persona always included (reserved slice)
          - Labeled facts + working topic next
          - LTM hits: Abstract tier first; expand Overview only if budget allows
          - Soft truncate remaining retrieval under max_chars

        No parallel legacy flat-key dump.
        """
        budget = max(200, int(max_chars))
        parts: List[str] = []
        used = 0

        blocks = self.build_context_blocks(entity_id=entity_id)

        # 1) Core / persona — always
        if blocks["core_identity"]:
            core_block = "## Core identity (protected)\n" + blocks["core_identity"]
            parts.append(core_block)
            used += len(core_block)
        else:
            # Still reserve a little headroom for late core hydration
            used += min(_CORE_RESERVE_CHARS, budget // 10)

        # 2) Labeled facts (online)
        if blocks.get("labeled_facts"):
            lf = blocks["labeled_facts"]
            remain = budget - used
            if remain > 80:
                if len(lf) > remain:
                    lf = lf[: remain - 20] + "\n… [truncated]"
                parts.append(lf)
                used += len(lf)

        # 3) Active working topic (trim if tight)
        if blocks.get("working_topic"):
            wt = blocks["working_topic"]
            remain = budget - used
            if remain > 120:
                if len(wt) > remain:
                    wt = wt[: remain - 20] + "\n… [truncated]"
                parts.append(wt)
                used += len(wt)

        # 4) Progressive retrieval: Abstract first; Overview only if budget allows
        if query and (budget - used) > 100:
            rec = self.recall(query, top_k=3, progressive=True)
            mem_lines: List[str] = []
            for a in rec.get("atoms") or []:
                mem_lines.append(
                    f"- [atom/{a.get('logical_net')}] {a.get('content', '')[:200]}"
                )
            for h in rec.get("ltm") or []:
                # Prefer abstract field; expand overview only under remaining budget
                abstract = (h.get("abstract") or h.get("content") or "")[:160]
                line = f"- [ltm abstract] {h.get('uri')}: {abstract}"
                mem_lines.append(line)
                uri = h.get("uri") or ""
                # Overview expand if room
                if uri and (budget - used - sum(len(x) for x in mem_lines)) > 400:
                    try:
                        overview = self.expand_ltm(uri, tier="overview")
                        if overview and len(overview) > len(abstract) + 40:
                            # Cap overview snippet
                            snip = overview[:280].replace("\n", " ")
                            mem_lines.append(f"  · overview: {snip}")
                    except Exception:
                        pass
            for h in rec.get("stm") or []:
                mem_lines.append(
                    f"- [stm] {h.get('title')}: {h.get('preview', '')[:160]}"
                )
            # Optional RRF lines (when enabled) for transparency
            if rec.get("fused"):
                for item in (rec["fused"] or [])[:3]:
                    src = item.get("source")
                    if src == "fact":
                        mem_lines.append(
                            f"- [fact/rrf] {item.get('key')}: {item.get('value', '')[:120]}"
                        )
            if mem_lines:
                mem_block = "## Retrieved memory\n" + "\n".join(mem_lines)
                remain = budget - used
                if remain > 60:
                    if len(mem_block) > remain:
                        mem_block = mem_block[: remain - 20] + "\n… [truncated]"
                    parts.append(mem_block)
                    used += len(mem_block)

        text = "\n\n".join(parts)
        if len(text) > budget:
            # Never drop core identity block if present
            if parts and parts[0].startswith("## Core identity"):
                core = parts[0]
                rest_budget = max(0, budget - len(core) - 30)
                rest = "\n\n".join(parts[1:])
                if len(rest) > rest_budget:
                    rest = rest[:rest_budget] + "\n… [truncated]"
                text = core + ("\n\n" + rest if rest else "")
            else:
                text = text[: budget - 20] + "\n… [truncated]"
        return text

    # ── Status / maintenance ──

    def status(self) -> dict:
        eid = self.entity_id or "default"
        return {
            "vnext_enabled": memory_vnext_enabled(),
            "single_system": True,
            "entity_id": eid,
            "wm": self.wm.status(),
            "labeled_facts": self.facts.status(eid),
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
