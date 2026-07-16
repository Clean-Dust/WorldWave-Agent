"""
core/memory/topic_stm.py — Hippocampus as STM for topics (v-next)

- Capacity ≫ WM; BM25 search over topics
- Every WM→Hippocampus transition re-evaluates composite score
- Digest + body = one topic unit for scoring / promotion
- Default weights: Rel 0.30, Freq 0.24, Div 0.15, Rec 0.15, Cons 0.10, Rich 0.06
- Full: hard-filter chatter/blobs/pronouns; promote if composite≥0.8 AND recall≥3
- CRITICAL: any leave (promote OR purge) MUST extract atoms first
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .topic import Topic

logger = logging.getLogger("ww.memory.topic_stm")

# Default composite weights (sum = 1.0)
DEFAULT_WEIGHTS = {
    "relevance": 0.30,
    "frequency": 0.24,
    "diversity": 0.15,
    "recency": 0.15,
    "consolidation": 0.10,
    "richness": 0.06,
}

DEFAULT_PROMOTE_MIN_SCORE = 0.8
DEFAULT_PROMOTE_MIN_RECALL = 3
DEFAULT_RECENCY_HALF_LIFE_DAYS = 14.0
DEFAULT_TOPIC_CAP = 200

# Chatter / quality hard filters
_CHATTER_RE = re.compile(
    r"^(hi|hello|hey|ok|okay|thanks|thank you|lol|haha|嗯|好的|谢谢)[\s!.]*$",
    re.I,
)
_PRONOUN_ONLY_RE = re.compile(
    r"\b(he|she|it|they|them|this|that|those|these)\b",
    re.I,
)
_NAMED_ENTITY_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b|[A-Za-z0-9_]{4,}"
)


def resolve_topic_hippo_cap() -> int:
    raw = os.environ.get("WW_TOPIC_HIPPO_CAP") or os.environ.get("WW_HIPPOCAMPUS_CAP")
    if raw and str(raw).strip():
        try:
            return max(8, int(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_TOPIC_CAP


def resolve_promote_min_score() -> float:
    raw = os.environ.get("WW_HIPPO_PROMOTE_MIN_SCORE")
    if raw and str(raw).strip():
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return DEFAULT_PROMOTE_MIN_SCORE


def resolve_promote_min_recall() -> int:
    raw = os.environ.get("WW_HIPPO_PROMOTE_MIN_RECALL")
    if raw and str(raw).strip():
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_PROMOTE_MIN_RECALL


# ── BM25 ───────────────────────────────────────────────────────────


class BM25Index:
    """Minimal BM25 (Okapi) over an in-memory corpus of documents."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: Dict[str, List[str]] = {}
        self._df: Counter = Counter()
        self._avgdl: float = 0.0

    @staticmethod
    def tokenize(text: str) -> List[str]:
        return re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", (text or "").lower())

    def add(self, doc_id: str, text: str) -> None:
        tokens = self.tokenize(text)
        if doc_id in self._docs:
            self.remove(doc_id)
        self._docs[doc_id] = tokens
        for term in set(tokens):
            self._df[term] += 1
        self._reavg()

    def remove(self, doc_id: str) -> None:
        tokens = self._docs.pop(doc_id, None)
        if not tokens:
            return
        for term in set(tokens):
            self._df[term] = max(0, self._df[term] - 1)
            if self._df[term] == 0:
                del self._df[term]
        self._reavg()

    def _reavg(self) -> None:
        if not self._docs:
            self._avgdl = 0.0
            return
        self._avgdl = sum(len(t) for t in self._docs.values()) / len(self._docs)

    def score(self, query: str, doc_id: str) -> float:
        tokens = self._docs.get(doc_id)
        if not tokens:
            return 0.0
        q_terms = self.tokenize(query)
        if not q_terms:
            return 0.0
        N = max(1, len(self._docs))
        dl = len(tokens)
        tf_map = Counter(tokens)
        score = 0.0
        for term in q_terms:
            if term not in tf_map:
                continue
            df = self._df.get(term, 0)
            idf = math.log(1.0 + (N - df + 0.5) / (df + 0.5))
            tf = tf_map[term]
            denom = tf + self.k1 * (1.0 - self.b + self.b * dl / max(self._avgdl, 1.0))
            score += idf * (tf * (self.k1 + 1.0)) / denom
        return score

    def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        scored = [(doc_id, self.score(query, doc_id)) for doc_id in self._docs]
        scored = [(d, s) for d, s in scored if s > 0]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


# ── Hard filters for promotion ─────────────────────────────────────


def is_chatter(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if len(t) < 8:
        return True
    if _CHATTER_RE.match(t):
        return True
    # Very short multi-line small talk
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if lines and all(len(ln) < 12 and _CHATTER_RE.match(ln) for ln in lines):
        return True
    return False


def is_multi_fact_blob(text: str) -> bool:
    """True if looks like an un-atomized multi-fact paste (promotion blocker)."""
    t = text or ""
    # Many independent clauses / bullets without structure
    bullets = len(re.findall(r"(?:^|\n)\s*[-*•]\s+", t))
    sentences = len(re.findall(r"[.!?。](?:\s|$)", t))
    if bullets >= 8 and sentences >= 8:
        return True
    if sentences >= 15 and len(t) > 2000:
        return True
    return False


def has_unresolved_pronouns(text: str) -> bool:
    """True when pronouns dominate without named anchors."""
    t = text or ""
    if not t.strip():
        return True
    pronouns = len(_PRONOUN_ONLY_RE.findall(t))
    anchors = len(_NAMED_ENTITY_RE.findall(t))
    if pronouns >= 3 and anchors < 2:
        return True
    # Pure pronoun subjects with no content nouns
    words = re.findall(r"[A-Za-z]+", t)
    if words and pronouns >= max(2, len(words) // 3) and anchors == 0:
        return True
    return False


def passes_hard_filter(topic: Topic) -> bool:
    text = topic.full_text()
    if is_chatter(text):
        return False
    if is_multi_fact_blob(text):
        return False
    if has_unresolved_pronouns(text):
        return False
    return True


# ── Scoring ────────────────────────────────────────────────────────


def evaluate_topic(
    topic: Topic,
    *,
    now: Optional[float] = None,
    weights: Optional[Dict[str, float]] = None,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Recompute six-signal composite + Light/REM boost stubs. Mutates topic."""
    w = weights or DEFAULT_WEIGHTS
    clock = now if now is not None else time.time()

    # Relevance: average quality of uses — map from last BM25 / explicit set
    rel = max(0.0, min(1.0, float(topic.relevance)))

    # Frequency: recall_count scaled (3+ → strong)
    freq = max(0.0, min(1.0, topic.recall_count / 5.0))

    # Query diversity: unique query contexts
    div = max(0.0, min(1.0, len(set(topic.query_contexts)) / 5.0))

    # Recency: 14-day half-life from updated_at
    age_days = max(0.0, (clock - float(topic.updated_at or clock)) / 86400.0)
    rec = 0.5 ** (age_days / max(half_life_days, 0.01))
    rec = max(0.0, min(1.0, rec))

    # Consolidation: digests present + multi-day presence signal
    cons = float(topic.consolidation)
    if topic.digests:
        cons = max(cons, min(1.0, 0.3 + 0.2 * len(topic.digests)))
    age_span_days = max(
        0.0,
        (float(topic.updated_at or clock) - float(topic.created_at or clock)) / 86400.0,
    )
    if age_span_days >= 1.0:
        cons = max(cons, min(1.0, 0.2 + 0.1 * age_span_days))
    cons = max(0.0, min(1.0, cons))

    # Conceptual richness: entity/tag density vs length
    text = topic.full_text()
    toks = max(1, len(BM25Index.tokenize(text)))
    concepts = len(set(topic.entities) | set(topic.tags))
    if concepts == 0:
        # Heuristic concept tokens (capitalized / long tokens)
        concepts = len(set(_NAMED_ENTITY_RE.findall(text)))
    rich = max(0.0, min(1.0, concepts / max(8.0, toks / 20.0)))
    if topic.conceptual_richness:
        rich = max(rich, min(1.0, float(topic.conceptual_richness)))

    topic.frequency = freq
    topic.query_diversity = div
    topic.recency = rec
    topic.consolidation = cons
    topic.conceptual_richness = rich

    base = (
        w.get("relevance", 0.30) * rel
        + w.get("frequency", 0.24) * freq
        + w.get("diversity", 0.15) * div
        + w.get("recency", 0.15) * rec
        + w.get("consolidation", 0.10) * cons
        + w.get("richness", 0.06) * rich
    )
    boost = float(topic.light_boost or 0.0) + float(topic.rem_boost or 0.0)
    composite = max(0.0, min(1.0, base + boost))
    topic.composite_score = composite
    return composite


# ── Topic Hippocampus (STM) ────────────────────────────────────────

AtomExtractFn = Callable[[Topic], List[Any]]
PromoteFn = Callable[[Topic, List[Any]], None]  # topic + extracted atoms


class TopicHippocampus:
    """Short-term memory for topics with BM25 + promote/evict + atom extract."""

    def __init__(
        self,
        data_dir: str = "",
        cap: Optional[int] = None,
        atom_extract: Optional[AtomExtractFn] = None,
        on_promote: Optional[PromoteFn] = None,
        weights: Optional[Dict[str, float]] = None,
    ):
        self.data_dir = Path(data_dir) if data_dir else Path(
            os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
        ) / "memory" / "topic_stm"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cap = cap if cap is not None else resolve_topic_hippo_cap()
        self.atom_extract = atom_extract
        self.on_promote = on_promote
        self.weights = weights or dict(DEFAULT_WEIGHTS)
        self._topics: Dict[str, Topic] = {}
        self._bm25 = BM25Index()
        self._path = self.data_dir / "topics.json"
        self._load()

    # ── Persistence ──

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data.get("topics") or []:
                t = Topic.from_dict(item)
                self._topics[t.topic_id] = t
                self._bm25.add(t.topic_id, t.full_text())
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("TopicHippocampus load failed: %s", e)

    def _save(self) -> None:
        try:
            payload = {
                "topics": [t.to_dict() for t in self._topics.values()],
                "cap": self.cap,
            }
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("TopicHippocampus save failed: %s", e)

    # ── Admit from WM ──

    def admit(self, topic: Topic, *, reevaluate: bool = True) -> dict:
        """Accept a topic from WM. Always re-evaluates score when reevaluate=True.

        If full: try promote eligible; else purge lowest scores. Atom extract on leave.
        """
        # Re-evaluate on every WM→hippo transition
        if reevaluate:
            evaluate_topic(topic, weights=self.weights)

        # Replace existing same id (round-trip)
        if topic.topic_id in self._topics:
            self._topics[topic.topic_id] = topic
            self._bm25.add(topic.topic_id, topic.full_text())
            self._save()
            return {"action": "updated", "topic_id": topic.topic_id, "score": topic.composite_score}

        promoted: List[str] = []
        purged: List[str] = []

        while len(self._topics) >= self.cap:
            # Try promote highest eligible non-core first among those that pass threshold
            did = self._try_promote_one()
            if did:
                promoted.append(did)
                continue
            # Purge lowest score (never core)
            purged_id = self._purge_lowest()
            if purged_id:
                purged.append(purged_id)
            else:
                # Only core remains — still admit (may exceed cap)
                break

        self._topics[topic.topic_id] = topic
        self._bm25.add(topic.topic_id, topic.full_text())
        self._save()
        return {
            "action": "admitted",
            "topic_id": topic.topic_id,
            "score": topic.composite_score,
            "promoted": promoted,
            "purged": purged,
            "count": len(self._topics),
        }

    def _extract_atoms(self, topic: Topic) -> List[Any]:
        if self.atom_extract is None:
            return []
        try:
            return list(self.atom_extract(topic) or [])
        except Exception as e:
            logger.error("atom extract on leave failed for %s: %s", topic.topic_id, e)
            return []

    def _leave(self, topic: Topic, reason: str) -> List[Any]:
        """CRITICAL: extract atoms before topic leaves hippocampus."""
        atoms = self._extract_atoms(topic)
        self._bm25.remove(topic.topic_id)
        self._topics.pop(topic.topic_id, None)
        logger.info(
            "Topic left hippocampus id=%s reason=%s atoms=%d",
            topic.topic_id[:8],
            reason,
            len(atoms),
        )
        return atoms

    def promote(self, topic_id: str, *, force: bool = False) -> dict:
        """Promote topic to LTM if thresholds met (or force). Always extracts atoms."""
        topic = self._topics.get(topic_id)
        if not topic:
            return {"ok": False, "error": "not_found"}
        evaluate_topic(topic, weights=self.weights)
        min_score = resolve_promote_min_score()
        min_recall = resolve_promote_min_recall()
        if not force:
            if not passes_hard_filter(topic):
                return {"ok": False, "error": "hard_filter", "score": topic.composite_score}
            if topic.composite_score < min_score or topic.recall_count < min_recall:
                return {
                    "ok": False,
                    "error": "threshold",
                    "score": topic.composite_score,
                    "recall_count": topic.recall_count,
                    "min_score": min_score,
                    "min_recall": min_recall,
                }
        atoms = self._leave(topic, "promote")
        if self.on_promote is not None:
            try:
                self.on_promote(topic, atoms)
            except Exception as e:
                logger.error("on_promote failed: %s", e)
        self._save()
        return {
            "ok": True,
            "topic_id": topic_id,
            "atoms_extracted": len(atoms),
            "score": topic.composite_score,
            "atoms": atoms,
            "topic": topic,
        }

    def purge(self, topic_id: str) -> dict:
        """Purge topic (atoms still extracted). Core topics refuse purge."""
        topic = self._topics.get(topic_id)
        if not topic:
            return {"ok": False, "error": "not_found"}
        if topic.is_core:
            return {"ok": False, "error": "is_core"}
        atoms = self._leave(topic, "purge")
        self._save()
        return {
            "ok": True,
            "topic_id": topic_id,
            "atoms_extracted": len(atoms),
            "atoms": atoms,
        }

    def _try_promote_one(self) -> Optional[str]:
        min_score = resolve_promote_min_score()
        min_recall = resolve_promote_min_recall()
        candidates = []
        for t in self._topics.values():
            evaluate_topic(t, weights=self.weights)
            if t.is_core:
                continue
            if not passes_hard_filter(t):
                continue
            if t.composite_score >= min_score and t.recall_count >= min_recall:
                candidates.append(t)
        if not candidates:
            return None
        # Promote highest composite first
        candidates.sort(key=lambda x: -x.composite_score)
        tid = candidates[0].topic_id
        result = self.promote(tid)
        return tid if result.get("ok") else None

    def _purge_lowest(self) -> Optional[str]:
        victims = [t for t in self._topics.values() if not t.is_core]
        if not victims:
            return None
        for t in victims:
            evaluate_topic(t, weights=self.weights)
        victims.sort(key=lambda x: (x.composite_score, x.updated_at))
        tid = victims[0].topic_id
        result = self.purge(tid)
        return tid if result.get("ok") else None

    # ── Recall ──

    def search(self, query: str, top_k: int = 10) -> List[dict]:
        hits = self._bm25.search(query, top_k=top_k)
        out = []
        for doc_id, score in hits:
            t = self._topics.get(doc_id)
            if not t:
                continue
            # Track recall signals
            t.recall_count += 1
            t.last_recalled = time.time()
            # Relevance running average of BM25 quality (normalized softly)
            norm = max(0.0, min(1.0, score / 10.0))
            if t.relevance <= 0:
                t.relevance = norm
            else:
                t.relevance = 0.7 * t.relevance + 0.3 * norm
            ctx = query.strip()[:80]
            if ctx and ctx not in t.query_contexts:
                t.query_contexts.append(ctx)
                if len(t.query_contexts) > 20:
                    t.query_contexts = t.query_contexts[-20:]
            evaluate_topic(t, weights=self.weights)
            out.append({
                "topic_id": t.topic_id,
                "title": t.title,
                "bm25": score,
                "composite": t.composite_score,
                "recall_count": t.recall_count,
                "text_preview": t.full_text()[:300],
                "topic": t,
            })
        if out:
            self._save()
        return out

    def get(self, topic_id: str) -> Optional[Topic]:
        return self._topics.get(topic_id)

    def all(self) -> List[Topic]:
        return list(self._topics.values())

    def count(self) -> int:
        return len(self._topics)

    def status(self) -> dict:
        return {
            "count": len(self._topics),
            "cap": self.cap,
            "core_count": sum(1 for t in self._topics.values() if t.is_core),
            "weights": dict(self.weights),
            "promote_min_score": resolve_promote_min_score(),
            "promote_min_recall": resolve_promote_min_recall(),
        }

    def clear(self) -> None:
        self._topics.clear()
        self._bm25 = BM25Index()
        self._save()
