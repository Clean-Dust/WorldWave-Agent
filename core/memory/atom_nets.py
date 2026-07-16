"""
core/memory/atom_nets.py — Four logical networks + Connect graph (v-next)

Nets:
  World       — objective verifiable facts
  Experience  — agent interaction events (passive lossless landing)
  Observation — synthesized insights with evidence links
  Opinion     — subjective beliefs with confidence

Connect relations:
  Updates  — supersede (new preferred as current; old kept historical)
  Extends  — both remain valid
  Derives  — inferred from parents

Dual timestamps:
  valid_from / valid_until — real-world validity window
  learned_at               — when the agent learned the fact

Outdated → mark invalid/supersede; no hard delete of history.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("ww.memory.atom_nets")

LOGICAL_NETS = ("world", "experience", "observation", "opinion")
CONNECT_RELATIONS = ("Updates", "Extends", "Derives")

# Map to EdgeStore relation types when bridging
RELATION_TO_EDGE = {
    "Updates": "SUPERSEDES",
    "Extends": "RELATED_TO",
    "Derives": "DERIVED_FROM",
}


def _now() -> float:
    return time.time()


@dataclass
class MemoryAtomV2:
    """Self-contained memory atom with dual timestamps and logical net."""

    content: str
    atom_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    logical_net: str = "experience"  # world|experience|observation|opinion
    # Dual timestamps
    learned_at: float = field(default_factory=_now)  # agent learn time
    valid_from: float = 0.0  # world-time start (0 → learned_at)
    valid_until: float = 0.0  # world-time end (0 = still valid)
    # Supersede / history (no hard delete)
    superseded_by: str = ""
    invalid_at: float = 0.0
    # Opinion confidence [0,1]
    confidence: float = 0.5
    entities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    source: str = ""  # user|system|tool|inference|passive|dreaming
    topic_id: str = ""
    evidence: List[str] = field(default_factory=list)  # atom_ids supporting this
    is_core: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        net = (self.logical_net or "experience").lower().strip()
        if net not in LOGICAL_NETS:
            net = "experience"
        self.logical_net = net
        if self.valid_from <= 0:
            self.valid_from = self.learned_at
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    @property
    def is_currently_valid(self) -> bool:
        if self.superseded_by or self.invalid_at:
            return False
        if self.valid_until and _now() > self.valid_until:
            return False
        return True

    def mark_invalid(self, when: Optional[float] = None) -> None:
        self.invalid_at = when if when is not None else _now()
        if not self.valid_until:
            self.valid_until = self.invalid_at

    def to_dict(self) -> dict:
        return {
            "atom_id": self.atom_id,
            "content": self.content,
            "logical_net": self.logical_net,
            "learned_at": self.learned_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "superseded_by": self.superseded_by,
            "invalid_at": self.invalid_at,
            "confidence": self.confidence,
            "entities": list(self.entities),
            "tags": list(self.tags),
            "source": self.source,
            "topic_id": self.topic_id,
            "evidence": list(self.evidence),
            "is_core": self.is_core,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryAtomV2":
        return cls(
            content=str(d.get("content") or ""),
            atom_id=str(d.get("atom_id") or uuid.uuid4().hex[:16]),
            logical_net=str(d.get("logical_net") or "experience"),
            learned_at=float(d.get("learned_at") or d.get("timestamp") or _now()),
            valid_from=float(d.get("valid_from") or 0.0),
            valid_until=float(d.get("valid_until") or 0.0),
            superseded_by=str(d.get("superseded_by") or ""),
            invalid_at=float(d.get("invalid_at") or 0.0),
            confidence=float(d.get("confidence") if d.get("confidence") is not None else 0.5),
            entities=list(d.get("entities") or []),
            tags=list(d.get("tags") or []),
            source=str(d.get("source") or ""),
            topic_id=str(d.get("topic_id") or ""),
            evidence=list(d.get("evidence") or []),
            is_core=bool(d.get("is_core", False)),
            meta=dict(d.get("meta") or {}),
        )


@dataclass
class AtomLink:
    """Typed connection between atoms."""

    source_id: str  # newer / derived
    target_id: str  # older / parent
    relation: str  # Updates | Extends | Derives
    created_at: float = field(default_factory=_now)
    link_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    meta: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AtomLink":
        return cls(
            source_id=str(d["source_id"]),
            target_id=str(d["target_id"]),
            relation=str(d.get("relation") or "Extends"),
            created_at=float(d.get("created_at") or _now()),
            link_id=str(d.get("link_id") or uuid.uuid4().hex[:12]),
            meta=dict(d.get("meta") or {}),
        )


# ── Rule-based extract (no LLM on hot path) ────────────────────────

# Sentence / clause boundaries (EN + CJK punctuation)
_FACT_SPLIT = re.compile(
    r"(?<=[.!?。！？；;])\s+|"
    r"(?<=\n)[-*•]\s+|"  # bullet starts
    r"\n+"
)
# Multi-fact blob glue words → secondary split
_BLOB_SPLIT = re.compile(
    r"\s+(?:and also|also,|additionally,|furthermore,|plus,|moreover,)\s+",
    re.IGNORECASE,
)
# Capitalized entity-ish tokens (proper nouns / product names)
_ENTITY_RE = re.compile(
    r"\b([A-Z][a-zA-Z0-9_\-]{1,}(?:\s+[A-Z][a-zA-Z0-9_\-]{1,}){0,3})\b"
)
# Lowercase technical identifiers often worth indexing
_TECH_TOKEN_RE = re.compile(
    r"\b([a-z][a-z0-9]+(?:[_-][a-z0-9]+)+|[A-Z]{2,}[a-z0-9]*|[a-z]+(?:API|SDK|CLI|URL|ID))\b"
)
_CHATTER = frozenset({
    "ok", "okay", "thanks", "thank you", "hi", "hello", "hey", "lol",
    "sure", "yep", "yeah", "nope", "nm", "cool", "nice", "great",
    "got it", "sounds good", "alright", "fine", "嗯", "好的", "哈哈",
    "ok.", "okay.", "thanks.", "sure.",
})
# Pronoun-only / near-empty content (no durable fact)
_PRONOUN_ONLY = re.compile(
    r"^(?:i|you|he|she|it|we|they|me|him|her|us|them|this|that|these|those|"
    r"my|your|his|her|its|our|their|the|a|an|is|are|was|were|be|been|"
    r"am|do|does|did|have|has|had|will|would|can|could|should|may|might|"
    r"not|no|yes|and|or|but|so|if|then|than|to|of|in|on|at|for|with|"
    r"about|from|as|by|up|out|"
    r"我|你|他|她|它|我们|你们|他们|这|那|是|的|了|吗|呢|吧)+"
    r"(?:\s+|$|[.!?。])*$",
    re.IGNORECASE,
)
_ROLE_PREFIX = re.compile(r"^(?:user|assistant|system|tool)\s*:\s*", re.IGNORECASE)


def atom_llm_extract_enabled() -> bool:
    """Optional background LLM extract. Default OFF (WW_ATOM_LLM_EXTRACT=1)."""
    raw = os.environ.get("WW_ATOM_LLM_EXTRACT")
    if raw is None or str(raw).strip() == "":
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _is_chatter(chunk: str) -> bool:
    s = chunk.strip().lower().rstrip(".!?,;: ")
    if not s:
        return True
    if s in _CHATTER:
        return True
    # Multi-token pure affirmations: "ok thanks", "sure cool", …
    tokens = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", s)
    if tokens and all(t in _CHATTER or len(t) <= 2 for t in tokens) and len(s) < 40:
        return True
    # Very short affirmations
    if len(s) <= 4 and s.isalpha():
        return True
    return False


def _is_pronoun_only(chunk: str) -> bool:
    cleaned = _ROLE_PREFIX.sub("", chunk.strip())
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 4:
        return True
    # No entity-ish or tech token and matches pronoun-heavy pattern
    if _ENTITY_RE.search(cleaned) or _TECH_TOKEN_RE.search(cleaned):
        return False
    # Drop pure deictic / filler sentences without content words >3 chars (non-pronoun)
    words = [w for w in re.findall(r"[a-zA-Z\u4e00-\u9fff]{2,}", cleaned.lower())]
    content = [
        w for w in words
        if w not in {
            "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
            "us", "them", "this", "that", "these", "those", "my", "your",
            "his", "its", "our", "their", "the", "and", "but", "for", "with",
            "about", "from", "have", "has", "had", "was", "were", "are", "is",
            "been", "will", "would", "could", "should", "can", "not", "yes",
            "no", "just", "really", "very", "also", "then", "than", "into",
            "like", "know", "think", "want", "need", "got", "get", "said",
            "tell", "told", "see", "look", "okay", "sure", "well", "please",
            "thanks", "thank",
        }
    ]
    if not content:
        return True
    if _PRONOUN_ONLY.match(cleaned):
        return True
    return False


def _extract_entities(chunk: str) -> List[str]:
    ents = [m.group(1) for m in _ENTITY_RE.finditer(chunk)]
    # Skip sentence-start false positives that are common English words
    stop_cap = {
        "The", "This", "That", "There", "These", "Those", "When", "Where",
        "What", "Why", "How", "And", "But", "For", "With", "From", "After",
        "Before", "Please", "Thanks", "Hello", "Okay", "Sure", "Noted",
        "User", "Assistant", "System", "Digest", "Body", "Turn", "Reply",
    }
    seen: Set[str] = set()
    out: List[str] = []
    for e in ents:
        if e in stop_cap:
            continue
        el = e.lower()
        if el not in seen:
            seen.add(el)
            out.append(e)
    for m in _TECH_TOKEN_RE.finditer(chunk):
        tok = m.group(1)
        el = tok.lower()
        if el not in seen and len(tok) >= 3:
            seen.add(el)
            out.append(tok)
    return out[:12]


def _split_into_fact_chunks(text: str) -> List[str]:
    """Sentence split + multi-fact blob split + bullet lines."""
    if not text or not text.strip():
        return []
    raw = text.strip()
    # Primary sentence split
    chunks = [c.strip() for c in _FACT_SPLIT.split(raw) if c and c.strip()]
    # Bullet / line fallback when barely split
    if len(chunks) <= 1 and "\n" in raw:
        chunks = [ln.strip(" -*\t•") for ln in raw.splitlines() if len(ln.strip()) > 8]
    # Secondary: multi-fact glue words
    expanded: List[str] = []
    for c in chunks:
        parts = [p.strip() for p in _BLOB_SPLIT.split(c) if p and p.strip()]
        if len(parts) > 1:
            expanded.extend(parts)
        else:
            # Split long "A. B. C" already handled; also split on " — " / " / "
            if " — " in c and len(c) > 60:
                expanded.extend(p.strip() for p in c.split(" — ") if len(p.strip()) > 8)
            elif "; " in c and len(c) > 40:
                expanded.extend(p.strip() for p in c.split("; ") if len(p.strip()) > 8)
            else:
                expanded.append(c)
    # Strip role prefixes for cleaner atoms
    cleaned = []
    for c in expanded:
        c2 = _ROLE_PREFIX.sub("", c).strip()
        # Drop digest header scaffolding
        if c2.startswith("[Digests]") or c2.startswith("[Body]"):
            continue
        if c2.lower().startswith("digest of "):
            continue
        cleaned.append(c2)
    return cleaned


def extract_atoms_from_text(
    text: str,
    *,
    topic_id: str = "",
    source: str = "extract",
    logical_net: str = "experience",
    learned_at: Optional[float] = None,
) -> List[MemoryAtomV2]:
    """Split text into self-contained atom candidates (rule-based, no dual LLM).

    Improvements over naive split:
      - sentence + multi-fact blob split
      - entity-ish / tech token capture
      - drop chatter and pronoun-only noise
    """
    if not text or not text.strip():
        return []
    learned = learned_at if learned_at is not None else _now()
    chunks = _split_into_fact_chunks(text)

    atoms: List[MemoryAtomV2] = []
    seen_content: Set[str] = set()
    for chunk in chunks:
        if len(chunk) < 6:
            continue
        if _is_chatter(chunk):
            continue
        if _is_pronoun_only(chunk):
            continue
        key = chunk[:200].lower()
        if key in seen_content:
            continue
        seen_content.add(key)
        entities = _extract_entities(chunk)
        # Prefer world net when it looks like a durable fact with entities
        net = logical_net
        if entities and re.search(
            r"\b(is|are|was|were|uses|used|prefers|preferred|works at|joined|named)\b",
            chunk,
            re.IGNORECASE,
        ):
            net = "world" if logical_net == "experience" else logical_net
        atoms.append(
            MemoryAtomV2(
                content=chunk[:500],
                logical_net=net,
                learned_at=learned,
                valid_from=learned,
                entities=entities,
                source=source,
                topic_id=topic_id,
            )
        )
    # Fallback: whole text as one atom if split yielded nothing useful
    # (but still drop pure chatter)
    if not atoms and len(text.strip()) >= 6 and not _is_chatter(text.strip()):
        cleaned = _ROLE_PREFIX.sub("", text.strip())
        if not _is_pronoun_only(cleaned):
            atoms.append(
                MemoryAtomV2(
                    content=cleaned[:500],
                    logical_net=logical_net,
                    learned_at=learned,
                    valid_from=learned,
                    entities=_extract_entities(cleaned),
                    source=source,
                    topic_id=topic_id,
                )
            )
    return atoms


def maybe_llm_extract_atoms(
    text: str,
    *,
    topic_id: str = "",
    source: str = "llm_extract",
) -> List[MemoryAtomV2]:
    """Optional background LLM extract. Default off; requires cheap model config.

    Never called on hot write path unless WW_ATOM_LLM_EXTRACT=1 and a model
    client is available. Failures return [] (rule extract remains primary).
    """
    if not atom_llm_extract_enabled():
        return []
    if not text or len(text.strip()) < 20:
        return []
    try:
        # Lazy import — keep hot path free of LLM deps
        from core.config import ConfigManager  # type: ignore

        cfg = ConfigManager()
        model = (
            os.environ.get("WW_ATOM_LLM_MODEL")
            or cfg.get("atom_extract_model")
            or cfg.get("cheap_model")
            or ""
        )
        if not model:
            logger.debug("WW_ATOM_LLM_EXTRACT=1 but no cheap model configured")
            return []
        # Intentionally minimal: if LLM client unavailable, no-op
        llm = getattr(cfg, "get_llm", None)
        if llm is None:
            return []
        client = llm() if callable(llm) else None
        if client is None or not hasattr(client, "chat"):
            return []
        prompt = (
            "Extract durable factual memory atoms as a JSON list of strings. "
            "No chatter, no pronouns-only. Max 8 items.\n\n" + text[:3000]
        )
        resp = client.chat(
            messages=[{"role": "user", "content": prompt}],
            json_mode=True,
            max_tokens=400,
            model=model,
        )
        import json as _json

        data = _json.loads(resp) if isinstance(resp, str) else resp
        items = data if isinstance(data, list) else data.get("atoms") or data.get("facts") or []
        out: List[MemoryAtomV2] = []
        for item in items[:8]:
            content = item if isinstance(item, str) else str(item.get("content") or item.get("text") or "")
            content = content.strip()
            if len(content) < 6 or _is_chatter(content):
                continue
            out.append(
                MemoryAtomV2(
                    content=content[:500],
                    logical_net="world",
                    source=source,
                    topic_id=topic_id,
                    entities=_extract_entities(content),
                    confidence=0.6,
                )
            )
        return out
    except Exception as e:
        logger.debug("optional LLM atom extract skipped: %s", e)
        return []


def extract_atoms_from_topic(topic: Any, *, source: str = "topic_leave") -> List[MemoryAtomV2]:
    """Extract atoms covering body + digests of a topic unit.

    Called on leave (promote/purge) — MUST run before topic is discarded.
    Rule-based by default; optional LLM extract if WW_ATOM_LLM_EXTRACT=1.
    """
    topic_id = getattr(topic, "topic_id", "") or ""
    text = ""
    if hasattr(topic, "full_text"):
        text = topic.full_text()
    elif isinstance(topic, dict):
        text = str(topic.get("text") or topic.get("content") or "")
        topic_id = str(topic.get("topic_id") or topic_id)
    else:
        text = str(topic)
    atoms = extract_atoms_from_text(
        text, topic_id=topic_id, source=source, logical_net="experience"
    )
    # Optional background LLM enrich (never replaces rule extract)
    if atom_llm_extract_enabled():
        extra = maybe_llm_extract_atoms(text, topic_id=topic_id, source="llm_extract")
        seen = {a.content[:120].lower() for a in atoms}
        for a in extra:
            if a.content[:120].lower() not in seen:
                atoms.append(a)
    for a in atoms:
        if getattr(topic, "is_core", False):
            a.is_core = True
        if getattr(topic, "entities", None):
            for e in topic.entities:
                if e not in a.entities:
                    a.entities.append(e)
    return atoms


# ── Atom store ─────────────────────────────────────────────────────


class AtomNetStore:
    """Persistent atom store with four nets + connect graph. No hard deletes."""

    def __init__(self, data_dir: str = ""):
        base = data_dir or os.path.join(
            os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww")), "memory"
        )
        self.data_dir = Path(base) / "atom_nets"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._atoms: Dict[str, MemoryAtomV2] = {}
        self._links: List[AtomLink] = []
        self._by_net: Dict[str, Set[str]] = {n: set() for n in LOGICAL_NETS}
        self._entity_index: Dict[str, Set[str]] = defaultdict(set)
        self._path = self.data_dir / "atoms.json"
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data.get("atoms") or []:
                a = MemoryAtomV2.from_dict(item)
                self._index_add(a)
            for item in data.get("links") or []:
                self._links.append(AtomLink.from_dict(item))
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("AtomNetStore load failed: %s", e)

    def _save(self) -> None:
        try:
            payload = {
                "atoms": [a.to_dict() for a in self._atoms.values()],
                "links": [lk.to_dict() for lk in self._links],
            }
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("AtomNetStore save failed: %s", e)

    def _index_add(self, atom: MemoryAtomV2) -> None:
        self._atoms[atom.atom_id] = atom
        self._by_net.setdefault(atom.logical_net, set()).add(atom.atom_id)
        for e in atom.entities:
            self._entity_index[e.lower()].add(atom.atom_id)

    def add(self, atom: MemoryAtomV2) -> MemoryAtomV2:
        self._index_add(atom)
        self._save()
        return atom

    def add_many(self, atoms: List[MemoryAtomV2]) -> List[MemoryAtomV2]:
        for a in atoms:
            self._index_add(a)
        self._save()
        return atoms

    def get(self, atom_id: str) -> Optional[MemoryAtomV2]:
        return self._atoms.get(atom_id)

    def connect(
        self,
        source: MemoryAtomV2,
        target: MemoryAtomV2,
        relation: str,
    ) -> AtomLink:
        """Link source → target with Updates|Extends|Derives.

        Updates: mark target superseded by source (history retained).
        Extends: both valid.
        Derives: source derived from target (evidence).
        """
        rel = relation if relation in CONNECT_RELATIONS else "Extends"
        if rel == "Updates":
            target.superseded_by = source.atom_id
            target.mark_invalid()
            # Ensure source is current
            source.superseded_by = ""
            source.invalid_at = 0.0
            if not source.valid_from:
                source.valid_from = source.learned_at
        elif rel == "Derives":
            if target.atom_id not in source.evidence:
                source.evidence.append(target.atom_id)
        link = AtomLink(
            source_id=source.atom_id,
            target_id=target.atom_id,
            relation=rel,
        )
        self._links.append(link)
        self._atoms[source.atom_id] = source
        self._atoms[target.atom_id] = target
        self._save()
        return link

    def updates(self, new_atom: MemoryAtomV2, old_atom: MemoryAtomV2) -> AtomLink:
        return self.connect(new_atom, old_atom, "Updates")

    def extends(self, atom: MemoryAtomV2, base: MemoryAtomV2) -> AtomLink:
        return self.connect(atom, base, "Extends")

    def derives(self, derived: MemoryAtomV2, parent: MemoryAtomV2) -> AtomLink:
        return self.connect(derived, parent, "Derives")

    def query(
        self,
        *,
        text: str = "",
        logical_net: Optional[str] = None,
        entity: str = "",
        current_only: bool = True,
        include_historical: bool = False,
        limit: int = 20,
    ) -> List[MemoryAtomV2]:
        """Query atoms. current_only excludes invalid/superseded (freshness).

        include_historical=True returns superseded atoms as well (historical query).
        """
        results: List[MemoryAtomV2] = []
        q = (text or "").lower()
        ent = (entity or "").lower()
        for a in self._atoms.values():
            if logical_net and a.logical_net != logical_net:
                continue
            if current_only and not include_historical and not a.is_currently_valid:
                continue
            if ent and ent not in {e.lower() for e in a.entities} and ent not in a.content.lower():
                continue
            if q and q not in a.content.lower() and not any(q in e.lower() for e in a.entities):
                continue
            results.append(a)
        # Prefer current valid + higher confidence + fresher learned_at
        results.sort(
            key=lambda x: (
                1 if x.is_currently_valid else 0,
                x.confidence,
                x.learned_at,
            ),
            reverse=True,
        )
        return results[:limit]

    def current_truth(self, query: str, limit: int = 5) -> List[MemoryAtomV2]:
        """Freshness-safe: invalid/superseded never win as current truth."""
        hits = self.query(text=query, current_only=True, include_historical=False, limit=limit * 3)
        return [h for h in hits if h.is_currently_valid][:limit]

    def historical(self, query: str, limit: int = 10) -> List[MemoryAtomV2]:
        """Include superseded/invalid for timeline views."""
        return self.query(text=query, current_only=False, include_historical=True, limit=limit)

    def all(self) -> List[MemoryAtomV2]:
        return list(self._atoms.values())

    def links(self) -> List[AtomLink]:
        return list(self._links)

    def by_net(self, net: str) -> List[MemoryAtomV2]:
        ids = self._by_net.get(net, set())
        return [self._atoms[i] for i in ids if i in self._atoms]

    def stats(self) -> dict:
        by_net = {n: len(self._by_net.get(n, ())) for n in LOGICAL_NETS}
        valid = sum(1 for a in self._atoms.values() if a.is_currently_valid)
        return {
            "total": len(self._atoms),
            "valid": valid,
            "invalid_or_superseded": len(self._atoms) - valid,
            "links": len(self._links),
            "by_net": by_net,
        }

    def clear(self) -> None:
        self._atoms.clear()
        self._links.clear()
        self._by_net = {n: set() for n in LOGICAL_NETS}
        self._entity_index.clear()
        self._save()
