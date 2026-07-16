"""
core/memory/topic.py — Topic-centric Working Memory (v-next)

Product law:
- WM holds exactly one active topic for LLM injection, plus bound digests.
- Independent thread → separate topic; switch moves entire topic A to Hippocampus.
- Overflow (unsplittable): extract atoms → compress older turns into digest
  (never re-compress digests) → keep recent turns as body.
- is_core / persona never evicted under pressure.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("ww.memory.topic")

# ── Capacity defaults ──────────────────────────────────────────────

DEFAULT_MODEL_CONTEXT = 128_000
DEFAULT_BODY_KEEP_TURNS = 8
DEFAULT_BODY_KEEP_TOKENS = 2000
# ~4 chars per token heuristic (no tokenizer dependency)
CHARS_PER_TOKEN = 4.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def estimate_tokens(text: str) -> int:
    """Cheap token estimate without a tokenizer (~4 chars/token)."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def resolve_wm_token_budget(model_context: Optional[int] = None) -> int:
    """WW_WM_TOKEN_BUDGET or min(32000, 0.25 * model_context_or_128k)."""
    raw = os.environ.get("WW_WM_TOKEN_BUDGET")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(512, int(raw))
        except (TypeError, ValueError):
            pass
    ctx = model_context if model_context and model_context > 0 else DEFAULT_MODEL_CONTEXT
    return max(512, min(32_000, int(0.25 * ctx)))


def resolve_body_keep_turns() -> int:
    return max(1, _env_int("WW_WM_BODY_KEEP_TURNS", DEFAULT_BODY_KEEP_TURNS))


def resolve_body_keep_tokens() -> int:
    return max(64, _env_int("WW_WM_BODY_KEEP_TOKENS", DEFAULT_BODY_KEEP_TOKENS))


# ── Data models ────────────────────────────────────────────────────


@dataclass
class Turn:
    """One conversation turn inside a topic body."""

    role: str  # user | assistant | system | tool
    content: str
    timestamp: float = field(default_factory=time.time)
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def token_estimate(self) -> int:
        return estimate_tokens(self.content)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Turn":
        return cls(
            role=str(d.get("role", "user")),
            content=str(d.get("content", "")),
            timestamp=float(d.get("timestamp") or time.time()),
            turn_id=str(d.get("turn_id") or uuid.uuid4().hex[:12]),
        )


@dataclass
class Digest:
    """Compressed older turns — never re-compressed; travels with body."""

    content: str
    created_at: float = field(default_factory=time.time)
    digest_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turn_count: int = 0
    token_estimate: int = 0
    # Digests are type-locked: is_digest=True means no secondary compression
    is_digest: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Digest":
        content = str(d.get("content", ""))
        return cls(
            content=content,
            created_at=float(d.get("created_at") or time.time()),
            digest_id=str(d.get("digest_id") or uuid.uuid4().hex[:12]),
            turn_count=int(d.get("turn_count") or 0),
            token_estimate=int(d.get("token_estimate") or estimate_tokens(content)),
            is_digest=True,
        )


@dataclass
class Topic:
    """Topic unit: body turns + bound digests (one evaluation unit)."""

    topic_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    title: str = ""
    turns: List[Turn] = field(default_factory=list)
    digests: List[Digest] = field(default_factory=list)
    is_core: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Scoring state (shared by body+digests as one unit)
    relevance: float = 0.0
    frequency: float = 0.0
    query_diversity: float = 0.0
    recency: float = 1.0
    consolidation: float = 0.0
    conceptual_richness: float = 0.0
    composite_score: float = 0.0
    recall_count: int = 0
    last_recalled: float = 0.0
    light_boost: float = 0.0  # stub; default 0
    rem_boost: float = 0.0  # stub; default 0
    query_contexts: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def body_text(self) -> str:
        lines = []
        for t in self.turns:
            lines.append(f"{t.role}: {t.content}")
        return "\n".join(lines)

    def digests_text(self) -> str:
        return "\n---\n".join(d.content for d in self.digests if d.content)

    def full_text(self) -> str:
        parts = []
        if self.digests:
            parts.append("[Digests]\n" + self.digests_text())
        body = self.body_text()
        if body:
            parts.append("[Body]\n" + body)
        return "\n\n".join(parts) if parts else self.title or ""

    def token_estimate(self) -> int:
        n = estimate_tokens(self.title)
        for t in self.turns:
            n += t.token_estimate()
        for d in self.digests:
            n += d.token_estimate or estimate_tokens(d.content)
        return n

    def append_turn(self, role: str, content: str, timestamp: Optional[float] = None) -> Turn:
        turn = Turn(
            role=role,
            content=content,
            timestamp=timestamp if timestamp is not None else time.time(),
        )
        self.turns.append(turn)
        self.updated_at = turn.timestamp
        if not self.title and role == "user":
            self.title = content[:80].replace("\n", " ")
        return turn

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "turns": [t.to_dict() for t in self.turns],
            "digests": [d.to_dict() for d in self.digests],
            "is_core": self.is_core,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "relevance": self.relevance,
            "frequency": self.frequency,
            "query_diversity": self.query_diversity,
            "recency": self.recency,
            "consolidation": self.consolidation,
            "conceptual_richness": self.conceptual_richness,
            "composite_score": self.composite_score,
            "recall_count": self.recall_count,
            "last_recalled": self.last_recalled,
            "light_boost": self.light_boost,
            "rem_boost": self.rem_boost,
            "query_contexts": list(self.query_contexts),
            "entities": list(self.entities),
            "tags": list(self.tags),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Topic":
        return cls(
            topic_id=str(d.get("topic_id") or uuid.uuid4().hex[:16]),
            title=str(d.get("title") or ""),
            turns=[Turn.from_dict(t) for t in (d.get("turns") or [])],
            digests=[Digest.from_dict(x) for x in (d.get("digests") or [])],
            is_core=bool(d.get("is_core", False)),
            created_at=float(d.get("created_at") or time.time()),
            updated_at=float(d.get("updated_at") or time.time()),
            relevance=float(d.get("relevance") or 0.0),
            frequency=float(d.get("frequency") or 0.0),
            query_diversity=float(d.get("query_diversity") or 0.0),
            recency=float(d.get("recency") if d.get("recency") is not None else 1.0),
            consolidation=float(d.get("consolidation") or 0.0),
            conceptual_richness=float(d.get("conceptual_richness") or 0.0),
            composite_score=float(d.get("composite_score") or 0.0),
            recall_count=int(d.get("recall_count") or 0),
            last_recalled=float(d.get("last_recalled") or 0.0),
            light_boost=float(d.get("light_boost") or 0.0),
            rem_boost=float(d.get("rem_boost") or 0.0),
            query_contexts=list(d.get("query_contexts") or []),
            entities=list(d.get("entities") or []),
            tags=list(d.get("tags") or []),
            meta=dict(d.get("meta") or {}),
        )


# Optional atom extractor callback: (Topic) -> List[dict-or-atom]
AtomExtractFn = Callable[[Topic], List[Any]]


def compress_older_turns(
    topic: Topic,
    keep_turns: Optional[int] = None,
    keep_tokens: Optional[int] = None,
) -> Optional[Digest]:
    """Compress older body turns into a new digest; never re-compress digests.

    Keeps the tighter of last N turns OR ~keep_tokens on the body.
    Returns the new Digest or None if nothing to compress.
    """
    if not topic.turns:
        return None
    n_keep = keep_turns if keep_turns is not None else resolve_body_keep_turns()
    tok_keep = keep_tokens if keep_tokens is not None else resolve_body_keep_tokens()

    # Walk from end: keep recent until turn count OR token budget hit (tighter wins)
    kept: List[Turn] = []
    used_tokens = 0
    for turn in reversed(topic.turns):
        ttok = turn.token_estimate()
        if len(kept) >= n_keep:
            break
        if kept and used_tokens + ttok > tok_keep:
            break
        kept.append(turn)
        used_tokens += ttok
    kept.reverse()

    cut = len(topic.turns) - len(kept)
    if cut <= 0:
        return None

    older = topic.turns[:cut]
    # Build digest summary (rule-based; no LLM)
    lines = []
    for t in older:
        preview = t.content.replace("\n", " ").strip()
        if len(preview) > 200:
            preview = preview[:197] + "..."
        lines.append(f"- [{t.role}] {preview}")
    digest_body = f"Digest of {len(older)} earlier turns:\n" + "\n".join(lines)
    digest = Digest(
        content=digest_body,
        turn_count=len(older),
        token_estimate=estimate_tokens(digest_body),
        is_digest=True,
    )
    topic.digests.append(digest)
    topic.turns = kept
    topic.updated_at = time.time()
    return digest


def topic_overflow_pressure(topic: Topic, budget: Optional[int] = None) -> bool:
    """True when topic token estimate exceeds WM budget."""
    b = budget if budget is not None else resolve_wm_token_budget()
    return topic.token_estimate() > b


# ── Working Topic Store (single active topic) ──────────────────────


class WorkingTopicStore:
    """In-process + optional disk single-topic working memory.

    At any moment: exactly one active Topic (body + digests). Switching to
    an independent topic B moves entire topic A out via on_switch callback
    (caller should park A in TopicHippocampus).
    """

    def __init__(
        self,
        data_dir: str = "",
        token_budget: Optional[int] = None,
        model_context: Optional[int] = None,
        on_switch: Optional[Callable[[Topic], None]] = None,
        atom_extract: Optional[AtomExtractFn] = None,
    ):
        self.data_dir = Path(data_dir) if data_dir else None
        self.token_budget = (
            token_budget
            if token_budget is not None
            else resolve_wm_token_budget(model_context)
        )
        self.on_switch = on_switch
        self.atom_extract = atom_extract
        self._active: Optional[Topic] = None
        self._lock_path = None
        if self.data_dir:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._lock_path = self.data_dir / "active_topic.json"
            self._load()

    # ── Persistence ──

    def _load(self) -> None:
        if not self._lock_path or not self._lock_path.is_file():
            return
        try:
            data = json.loads(self._lock_path.read_text(encoding="utf-8"))
            if data:
                self._active = Topic.from_dict(data)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("WorkingTopicStore load failed: %s", e)

    def _save(self) -> None:
        if not self._lock_path:
            return
        try:
            payload = self._active.to_dict() if self._active else None
            self._lock_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("WorkingTopicStore save failed: %s", e)

    # ── API ──

    @property
    def active(self) -> Optional[Topic]:
        return self._active

    def clear(self) -> Optional[Topic]:
        """Clear active topic without parking (returns previous)."""
        prev = self._active
        self._active = None
        self._save()
        return prev

    def switch_topic(
        self,
        title: str = "",
        *,
        is_core: bool = False,
        topic: Optional[Topic] = None,
    ) -> Tuple[Optional[Topic], Topic]:
        """Switch to a new independent topic; park previous via on_switch.

        Returns (previous_topic_or_None, new_active).
        """
        previous = self._active
        if previous is not None and self.on_switch is not None:
            try:
                self.on_switch(previous)
            except Exception as e:
                logger.error("on_switch failed for topic %s: %s", previous.topic_id, e)
                raise

        if topic is not None:
            new_t = topic
        else:
            new_t = Topic(title=title or "", is_core=is_core)
            if title:
                new_t.title = title

        self._active = new_t
        self._save()
        return previous, new_t

    def ensure_active(self, title: str = "") -> Topic:
        """Return active topic, creating an empty one if needed."""
        if self._active is None:
            self._active = Topic(title=title or "conversation")
            self._save()
        return self._active

    def append_turn(
        self,
        role: str,
        content: str,
        *,
        new_topic: bool = False,
        topic_title: str = "",
        is_core: bool = False,
    ) -> Topic:
        """Append a turn to active topic (or switch if new_topic=True).

        On overflow: (1) atom extract (2) compress older → digest (3) keep recent body.
        Digests are never re-compressed.
        """
        if new_topic or self._active is None:
            if self._active is not None and new_topic:
                self.switch_topic(title=topic_title or content[:80], is_core=is_core)
            else:
                self._active = Topic(
                    title=topic_title or (content[:80] if role == "user" else "conversation"),
                    is_core=is_core,
                )

        topic = self._active
        assert topic is not None
        if is_core:
            topic.is_core = True
        topic.append_turn(role, content)
        self._handle_overflow(topic)
        self._save()
        return topic

    def _handle_overflow(self, topic: Topic) -> None:
        """Atom extract then digest compress until under budget (or core)."""
        # Core topics still digest for size but never get discarded from WM here
        guard = 0
        while topic_overflow_pressure(topic, self.token_budget) and guard < 8:
            guard += 1
            # 1) Extract atoms first
            if self.atom_extract is not None:
                try:
                    self.atom_extract(topic)
                except Exception as e:
                    logger.warning("atom_extract on overflow failed: %s", e)
            # 2) Compress older turns into digest (digests themselves never re-compressed)
            dig = compress_older_turns(topic)
            if dig is None:
                break  # nothing left to compress

    def inject_block(self) -> str:
        """Build LLM context block for active topic only (not system persona)."""
        if not self._active:
            return ""
        t = self._active
        parts = [f"## Active topic: {t.title or t.topic_id}"]
        if t.digests:
            parts.append("### Digests (bound)")
            for i, d in enumerate(t.digests, 1):
                parts.append(f"[Digest {i}]\n{d.content}")
        if t.turns:
            parts.append("### Recent turns")
            parts.append(t.body_text())
        return "\n\n".join(parts)

    def status(self) -> dict:
        t = self._active
        return {
            "has_active": t is not None,
            "topic_id": t.topic_id if t else None,
            "title": t.title if t else None,
            "turns": len(t.turns) if t else 0,
            "digests": len(t.digests) if t else 0,
            "tokens": t.token_estimate() if t else 0,
            "token_budget": self.token_budget,
            "is_core": bool(t.is_core) if t else False,
            "topic_slots": 1,
        }


def looks_like_topic_switch(prev_text: str, new_text: str) -> bool:
    """Heuristic: independent thread if little lexical overlap and both non-trivial.

    Split rule product law: if a thread can stand alone → separate topic.
    This is a lightweight signal for callers; agents may also force new_topic.
    """
    if not prev_text or not new_text:
        return False
    if len(new_text.strip()) < 12:
        return False
    a = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{3,}", prev_text.lower()))
    b = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{3,}", new_text.lower()))
    if not a or not b:
        return True
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    return overlap < 0.15
