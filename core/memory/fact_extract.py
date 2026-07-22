"""Deterministic durable-fact extraction for conversation ingest (no LLM).

Used on BEAM (and optional general) ingest paths so numbers, dates, names, and
metric updates become searchable atoms via the single v-next remember path.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# updated from X to Y / changed from X to Y / went from X to Y
_RE_UPDATE = re.compile(
    r"(?P<subj>(?:my\s+)?[A-Za-z][A-Za-z0-9_\- ]{1,40}?)\s+"
    r"(?:was\s+updated|updated|changed|went|increased|decreased|rose|fell)\s+"
    r"(?:from\s+)?(?P<old>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit1>[A-Za-z%]{0,16})\s*"
    r"(?:to|→|->)\s*"
    r"(?P<new>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit2>[A-Za-z%]{0,16})",
    re.I,
)

# N commits / N PR(s) / N stars / metric labels
_RE_COUNT_NOUN = re.compile(
    r"\b(?P<n>[\d,]+)\s+"
    r"(?P<noun>commits?|prs?|pull\s+requests?|stars?|issues?|reviews?|"
    r"followers?|lines?|tests?|bugs?|messages?|tokens?|days?|hours?|"
    r"weeks?|months?|years?|percent|%)\b",
    re.I,
)

# key metric: number  /  commits: 165  /  age is 42
_RE_LABELED_NUM = re.compile(
    r"\b(?P<key>[A-Za-z][A-Za-z0-9_\-]{1,32})\s*(?:=|:|is|are|was|were)\s*"
    r"(?P<val>[\d,]+(?:\.\d+)?)\s*(?P<unit>[A-Za-z%]{0,16})\b",
    re.I,
)

# ISO / common dates
_RE_DATE = re.compile(
    r"\b(?P<label>(?:on|since|from|until|before|after|by|date|deadline)?)\s*"
    r"(?P<date>"
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}"
    r")\b",
    re.I,
)

# Proper-ish names after "my name is" / "I'm" / "I am"
_RE_NAME = re.compile(
    r"\b(?:my\s+name\s+is|i(?:'m|\s+am))\s+"
    r"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b",
)

# city / company style: "home city is X", "work at X"
_RE_PLACE = re.compile(
    r"\b(?:home\s+city|city|live(?:s)?\s+in|based\s+in|work(?:s)?\s+at|"
    r"company|employer)\s*(?:is|:|=)?\s+"
    r"(?P<val>[A-Za-z][A-Za-z0-9 ._\-]{1,48})",
    re.I,
)

_STOP_KEYS = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "there",
        "it",
        "we",
        "you",
        "they",
        "and",
        "or",
        "but",
        "from",
        "to",
        "for",
        "with",
        "about",
        "into",
        "over",
        "after",
        "before",
        "when",
        "where",
        "what",
        "which",
        "who",
        "how",
        "i",
        "my",
        "our",
        "your",
        "his",
        "her",
        "its",
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "not",
        "no",
        "yes",
        "ok",
        "please",
        "thanks",
        "thank",
        "hello",
        "hi",
        "user",
        "assistant",
        "role",
        "content",
        "message",
        "turn",
        "part",
        "batch",
    }
)


def fact_extract_enabled() -> bool:
    """WW_BEAM_FACT_EXTRACT default ON (lightweight, no LLM)."""
    raw = os.environ.get("WW_BEAM_FACT_EXTRACT")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disabled")


def _norm_key(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", (raw or "").strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or s in _STOP_KEYS or len(s) > 48:
        return ""
    return s


def _norm_num(raw: str) -> str:
    return (raw or "").replace(",", "").strip()


def extract_durable_facts(text: str) -> List[Dict[str, str]]:
    """Parse durable facts from free text. Returns [{key, value, kind}, ...].

    Prefer later matches for the same key (knowledge_update semantics).
    """
    text = (text or "").strip()
    if not text or len(text) < 3:
        return []

    found: Dict[str, Dict[str, str]] = {}

    def put(key: str, value: str, kind: str = "outcome") -> None:
        k = _norm_key(key)
        v = (value or "").strip()
        if not k or not v or len(v) > 200:
            return
        if k in _STOP_KEYS:
            return
        found[k] = {"key": k, "value": v, "kind": kind}

    for m in _RE_UPDATE.finditer(text):
        subj = m.group("subj") or "metric"
        new = _norm_num(m.group("new"))
        unit = (m.group("unit2") or m.group("unit1") or "").strip()
        val = f"{new} {unit}".strip() if unit else new
        put(subj, val, "outcome")
        # Also store bare noun if subject ends with known metric word
        noun_m = re.search(
            r"(commits?|prs?|stars?|issues?|tests?|days?|hours?)\s*$",
            subj,
            re.I,
        )
        if noun_m:
            put(noun_m.group(1), val, "outcome")

    for m in _RE_COUNT_NOUN.finditer(text):
        noun = m.group("noun").replace(" ", "_")
        put(noun, _norm_num(m.group("n")), "outcome")

    for m in _RE_LABELED_NUM.finditer(text):
        key = m.group("key")
        if key.lower() in _STOP_KEYS:
            continue
        unit = (m.group("unit") or "").strip()
        val = _norm_num(m.group("val"))
        if unit and unit.lower() not in ("is", "are", "was", "were", "to", "of"):
            val = f"{val} {unit}"
        put(key, val, "outcome")

    for m in _RE_DATE.finditer(text):
        date = m.group("date")
        label = (m.group("label") or "date").strip() or "date"
        if label.lower() in ("on", "since", "from", "until", "before", "after", "by"):
            put(f"date_{label.lower()}", date, "outcome")
        else:
            put("mentioned_date", date, "outcome")

    for m in _RE_NAME.finditer(text):
        put("user_name", m.group("name").strip(), "outcome")

    for m in _RE_PLACE.finditer(text):
        # Derive key from the matched keyword area
        span = text[max(0, m.start() - 0) : m.start() + 20].lower()
        if "home" in span or "city" in span or "live" in span or "based" in span:
            put("home_city", m.group("val").strip().rstrip(".,;"), "outcome")
        elif "work" in span or "company" in span or "employer" in span:
            put("employer", m.group("val").strip().rstrip(".,;"), "outcome")
        else:
            put("place", m.group("val").strip().rstrip(".,;"), "outcome")

    return list(found.values())


def apply_facts_to_memory(
    memory: Any,
    facts: Sequence[Dict[str, str]],
    *,
    entity_id: str = "",
) -> List[Dict[str, Any]]:
    """Write extracted facts through v-next remember (Updates same key).

    ``memory`` may be MemoryVNext or MemorySystem (with .vnext / .remember).
    """
    if not facts:
        return []
    results: List[Dict[str, Any]] = []
    target = memory
    if target is None:
        return results
    # Prefer MemorySystem.vnext or direct MemoryVNext
    vnext = getattr(target, "vnext", None)
    if vnext is not None:
        target = vnext
    remember = getattr(target, "remember", None)
    if not callable(remember):
        return results
    eid = (entity_id or "").strip()
    for f in facts:
        key = str(f.get("key") or "").strip()
        value = str(f.get("value") or "").strip()
        kind = str(f.get("kind") or "outcome")
        if not key or not value:
            continue
        try:
            kwargs: Dict[str, Any] = {"kind": kind}
            if eid:
                kwargs["entity_id"] = eid
            out = remember(key, value, **kwargs)
            results.append(
                {
                    "key": key,
                    "value": value,
                    "status": (out or {}).get("status", "stored")
                    if isinstance(out, dict)
                    else "stored",
                    "atom_id": (out or {}).get("atom_id")
                    if isinstance(out, dict)
                    else None,
                }
            )
        except TypeError:
            # remember without entity_id / kind kwargs
            try:
                out = remember(key, value)
                results.append(
                    {
                        "key": key,
                        "value": value,
                        "status": "stored",
                        "atom_id": (out or {}).get("atom_id")
                        if isinstance(out, dict)
                        else None,
                    }
                )
            except Exception:
                continue
        except Exception:
            continue
    return results
