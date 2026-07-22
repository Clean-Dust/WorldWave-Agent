"""Deterministic durable-fact extraction for conversation ingest (no LLM).

Used on BEAM (and optional general) ingest paths so numbers, dates, names,
metrics, preferences, and contradiction markers become searchable atoms via
the single v-next remember path.
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

# N commits / N PR(s) / N stars / metric labels (incl. latency ms)
_RE_COUNT_NOUN = re.compile(
    r"\b(?P<n>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<noun>commits?|prs?|pull\s+requests?|stars?|issues?|reviews?|"
    r"followers?|lines?|tests?|bugs?|messages?|tokens?|days?|hours?|"
    r"weeks?|months?|years?|percent|%|"
    r"ms|milliseconds?|seconds?|s\b|requests?|users?|errors?|retries?|"
    r"latency|count|score|points?)\b",
    re.I,
)

# key metric: number  /  commits: 165  /  age is 42 / latency=42ms
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

# Preferences: "I prefer X", "please always use Y", "my preference is Z"
_RE_PREFERENCE = re.compile(
    r"\b(?:"
    r"i\s+prefer\s+(?P<p1>[^.;\n]{2,80})"
    r"|please\s+always\s+(?P<p2>[^.;\n]{2,80})"
    r"|my\s+preference\s+is\s+(?P<p3>[^.;\n]{2,80})"
    r"|prefer(?:s|red)?\s+(?P<p4>[^.;\n]{2,60})"
    r"|always\s+use\s+(?P<p5>[^.;\n]{2,60})"
    r")\b",
    re.I,
)

# Explicit contradiction markers
_RE_CONTRADICTION = re.compile(
    r"\b(?:"
    r"but\s+earlier"
    r"|but\s+actually"
    r"|actually\b"
    r"|on\s+second\s+thought"
    r"|I\s+was\s+wrong"
    r"|correction\s*:"
    r"|wait,?\s+no"
    r"|contradict(?:s|ion|ed)?"
    r")\b",
    re.I,
)

# "X is A but earlier/actually X is B" style dual values
_RE_DUAL_VALUE = re.compile(
    r"\b(?P<key>[A-Za-z][A-Za-z0-9_\-]{1,32})\s+"
    r"(?:is|was|are|were|=|:)\s+"
    r"(?P<v1>[\w.\-]+(?:\s+[\w.\-]+){0,3})"
    r"\s*[,;]?\s*"
    r"(?:but\s+(?:earlier|actually)|actually|however|though)\s+"
    r"(?:(?:it|they|that|this|\1)\s+)?"
    r"(?:is|was|are|were|=|:)?\s*"
    r"(?P<v2>[\w.\-]+(?:\s+[\w.\-]+){0,3})",
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


def has_contradiction_marker(text: str) -> bool:
    return bool(_RE_CONTRADICTION.search(text or ""))


def extract_durable_facts(text: str) -> List[Dict[str, str]]:
    """Parse durable facts from free text. Returns [{key, value, kind}, ...].

    Prefer later matches for the same key (knowledge_update semantics).
    Preference facts use kind=preference. Conflicts set conflict=true.
    """
    text = (text or "").strip()
    if not text or len(text) < 3:
        return []

    found: Dict[str, Dict[str, str]] = {}
    conflicts: List[Dict[str, str]] = []
    contra = has_contradiction_marker(text)

    def put(
        key: str,
        value: str,
        kind: str = "outcome",
        *,
        conflict: bool = False,
        force_new: bool = False,
    ) -> None:
        k = _norm_key(key)
        v = (value or "").strip()
        if not k or not v or len(v) > 200:
            return
        if k in _STOP_KEYS:
            return
        rec: Dict[str, str] = {"key": k, "value": v, "kind": kind}
        if conflict:
            rec["conflict"] = "true"
        if force_new or conflict:
            # Keep parallel conflict atoms (do not collapse in found map alone)
            if k in found and found[k].get("value") != v:
                old = dict(found[k])
                old["conflict"] = "true"
                conflicts.append(old)
                rec["conflict"] = "true"
                conflicts.append(rec)
                # Keep latest as "current" map entry too
                found[k] = rec
            else:
                found[k] = rec
        else:
            # Knowledge update: later value wins in map
            if k in found and found[k].get("value") != v and contra:
                old = dict(found[k])
                old["conflict"] = "true"
                conflicts.append(old)
                rec["conflict"] = "true"
                conflicts.append(rec)
            found[k] = rec

    # Explicit dual-value contradiction patterns first
    for m in _RE_DUAL_VALUE.finditer(text):
        key = m.group("key")
        v1 = (m.group("v1") or "").strip().rstrip(".,;")
        v2 = (m.group("v2") or "").strip().rstrip(".,;")
        if key and v1 and v2 and v1.lower() != v2.lower():
            put(key, v1, "outcome", conflict=True, force_new=True)
            put(key, v2, "outcome", conflict=True, force_new=True)

    for m in _RE_UPDATE.finditer(text):
        subj = m.group("subj") or "metric"
        new = _norm_num(m.group("new"))
        old = _norm_num(m.group("old") or "")
        unit = (m.group("unit2") or m.group("unit1") or "").strip()
        val = f"{new} {unit}".strip() if unit else new
        # Sequential update: store new as current (not conflict unless marker)
        put(subj, val, "outcome", conflict=False)
        noun_m = re.search(
            r"(commits?|prs?|stars?|issues?|tests?|days?|hours?|latency|"
            r"ms|count|score)\s*$",
            subj,
            re.I,
        )
        if noun_m:
            put(noun_m.group(1), val, "outcome")
        if old and contra:
            old_val = f"{old} {unit}".strip() if unit else old
            put(subj, old_val, "outcome", conflict=True, force_new=True)
            put(subj, val, "outcome", conflict=True, force_new=True)

    for m in _RE_COUNT_NOUN.finditer(text):
        noun = m.group("noun").replace(" ", "_")
        # Normalize latency unit aliases
        nl = noun.lower()
        if nl in ("milliseconds", "millisecond", "ms"):
            noun = "latency_ms"
        elif nl in ("seconds", "second") and "latency" in text.lower():
            noun = "latency_s"
        put(noun, _norm_num(m.group("n")), "outcome")

    for m in _RE_LABELED_NUM.finditer(text):
        key = m.group("key")
        if key.lower() in _STOP_KEYS:
            continue
        unit = (m.group("unit") or "").strip()
        val = _norm_num(m.group("val"))
        ul = unit.lower()
        if ul in ("ms", "millisecond", "milliseconds"):
            put("latency_ms" if "latenc" in key.lower() or key.lower() == "latency" else key, val, "outcome")
            if "latenc" in key.lower() or key.lower() in ("p50", "p95", "p99", "rtt"):
                put("latency_ms", val, "outcome")
            continue
        if unit and ul not in ("is", "are", "was", "were", "to", "of"):
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
        span = text[max(0, m.start() - 0) : m.start() + 20].lower()
        if "home" in span or "city" in span or "live" in span or "based" in span:
            put("home_city", m.group("val").strip().rstrip(".,;"), "outcome")
        elif "work" in span or "company" in span or "employer" in span:
            put("employer", m.group("val").strip().rstrip(".,;"), "outcome")
        else:
            put("place", m.group("val").strip().rstrip(".,;"), "outcome")

    # Preferences (P1.5)
    for m in _RE_PREFERENCE.finditer(text):
        pref = (
            m.group("p1")
            or m.group("p2")
            or m.group("p3")
            or m.group("p4")
            or m.group("p5")
            or ""
        ).strip().rstrip(".,;")
        if pref and len(pref) >= 2:
            put("preference", pref[:180], "preference")
            # Also key a short slug for search
            slug = _norm_key(pref.split()[0] if pref.split() else "pref")
            if slug:
                put(f"pref_{slug}"[:40], pref[:180], "preference")

    # If contradiction markers and same key got multiple numeric values in text,
    # re-scan count nouns for parallel conflict atoms
    if contra:
        nums: Dict[str, List[str]] = {}
        for m in _RE_COUNT_NOUN.finditer(text):
            noun = m.group("noun").replace(" ", "_").lower()
            nums.setdefault(noun, []).append(_norm_num(m.group("n")))
        for noun, vals in nums.items():
            uniq = []
            for v in vals:
                if v not in uniq:
                    uniq.append(v)
            if len(uniq) >= 2:
                for v in uniq:
                    put(noun, v, "outcome", conflict=True, force_new=True)

    # Merge: map values first, then any conflict-only extras not already listed
    out: List[Dict[str, str]] = list(found.values())
    seen_kv = {(f["key"], f["value"]) for f in out}
    for c in conflicts:
        kv = (c.get("key", ""), c.get("value", ""))
        if kv not in seen_kv and kv[0] and kv[1]:
            out.append(c)
            seen_kv.add(kv)
    return out


def apply_facts_to_memory(
    memory: Any,
    facts: Sequence[Dict[str, str]],
    *,
    entity_id: str = "",
) -> List[Dict[str, Any]]:
    """Write extracted facts through v-next remember (Updates same key).

    Conflict-tagged facts store parallel atoms (meta.conflict=true) via Extends
    or dual remember with conflict markers so both sides remain visible.
    ``memory`` may be MemoryVNext or MemorySystem (with .vnext / .remember).
    """
    if not facts:
        return []
    results: List[Dict[str, Any]] = []
    target = memory
    if target is None:
        return results
    vnext = getattr(target, "vnext", None)
    if vnext is not None:
        target = vnext
    remember = getattr(target, "remember", None)
    if not callable(remember):
        return results
    eid = (entity_id or "").strip()

    # Group conflict pairs by key
    by_key: Dict[str, List[Dict[str, str]]] = {}
    for f in facts:
        k = str(f.get("key") or "").strip()
        if k:
            by_key.setdefault(k, []).append(dict(f))

    for f in facts:
        key = str(f.get("key") or "").strip()
        value = str(f.get("value") or "").strip()
        kind = str(f.get("kind") or "outcome")
        is_conflict = str(f.get("conflict") or "").lower() in ("1", "true", "yes")
        if not key or not value:
            continue
        try:
            kwargs: Dict[str, Any] = {"kind": kind}
            if eid:
                kwargs["entity_id"] = eid
            # Preferences tagged for recall
            if kind == "preference":
                kwargs["kind"] = "constraint"  # durable preference-like
                # remember path still stores content; tag via category if supported
                kwargs["category"] = "preference"
            out = remember(key, value, **kwargs)
            atom_id = (out or {}).get("atom_id") if isinstance(out, dict) else None
            status = (
                (out or {}).get("status", "stored")
                if isinstance(out, dict)
                else "stored"
            )

            # Mark atom meta.conflict when flagged
            if is_conflict and atom_id:
                _mark_atom_conflict(target, atom_id, key=key, value=value)

            # If multiple conflict values for same key, link with Extends
            # so both remain current rather than pure supersede-only
            if is_conflict:
                siblings = by_key.get(key) or []
                if len(siblings) >= 2:
                    _ensure_conflict_siblings(target, key, eid)

            results.append(
                {
                    "key": key,
                    "value": value,
                    "status": status,
                    "atom_id": atom_id,
                    "conflict": is_conflict,
                    "kind": kind,
                }
            )
        except TypeError:
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
                        "conflict": is_conflict,
                        "kind": kind,
                    }
                )
            except Exception:
                continue
        except Exception:
            continue

    # Timeline side-effects: dated events from fact values / original not available here
    return results


def apply_timeline_from_text(
    memory: Any,
    text: str,
    *,
    entity_id: str = "",
) -> List[Dict[str, Any]]:
    """Extract dated events into memory.timeline store when present."""
    target = memory
    vnext = getattr(target, "vnext", None) if target is not None else None
    if vnext is not None:
        target = vnext
    if target is None:
        return []
    store = getattr(target, "timeline", None)
    if store is None:
        return []
    try:
        events = store.append_from_text(text, entity_id=entity_id)
        return [e.to_dict() for e in events]
    except Exception:
        return []


def _mark_atom_conflict(target: Any, atom_id: str, *, key: str, value: str) -> None:
    atoms = getattr(target, "atoms", None)
    if atoms is None:
        return
    try:
        a = atoms.get(atom_id) if hasattr(atoms, "get") else None
        if a is None:
            return
        meta = dict(getattr(a, "meta", None) or {})
        meta["conflict"] = True
        meta["key"] = key
        meta["value"] = value
        a.meta = meta
        tags = list(getattr(a, "tags", None) or [])
        if "conflict" not in tags:
            tags.append("conflict")
        a.tags = tags
        # Keep BOTH current for contradiction (clear supersede on conflict twins)
        if hasattr(a, "superseded_by"):
            # Do not force invalidate conflict siblings here
            pass
        if hasattr(atoms, "add"):
            atoms.add(a)
    except Exception:
        pass


def _ensure_conflict_siblings(target: Any, key: str, entity_id: str) -> None:
    """Ensure same-key conflict atoms are both tagged and linked via Extends."""
    atoms = getattr(target, "atoms", None)
    if atoms is None or not hasattr(atoms, "query"):
        return
    try:
        hits = atoms.query(
            text=f"{key}:", current_only=False, entity_id=entity_id, limit=20
        )
        conflict_hits = []
        for a in hits or []:
            content = str(getattr(a, "content", "") or "")
            if not content.startswith(f"{key}:"):
                continue
            meta = getattr(a, "meta", None) or {}
            tags = getattr(a, "tags", None) or []
            if meta.get("conflict") or "conflict" in tags:
                conflict_hits.append(a)
        # If we have ≥2 with different values, revive both as current + Extends
        if len(conflict_hits) < 2:
            # Tag all current same-key as conflict when ≥2 distinct values
            cur = [
                a
                for a in (hits or [])
                if str(getattr(a, "content", "") or "").startswith(f"{key}:")
            ]
            vals = {str(getattr(a, "content", "")) for a in cur}
            if len(vals) >= 2:
                conflict_hits = cur
        if len(conflict_hits) < 2:
            return
        for a in conflict_hits:
            meta = dict(getattr(a, "meta", None) or {})
            meta["conflict"] = True
            a.meta = meta
            tags = list(getattr(a, "tags", None) or [])
            if "conflict" not in tags:
                tags.append("conflict")
            a.tags = tags
            # Revive: clear supersession so both sides visible for probes
            try:
                a.superseded_by = ""
                a.invalid_at = 0.0
            except Exception:
                pass
            if hasattr(atoms, "add"):
                atoms.add(a)
        # Link pairs with Extends (both valid)
        if hasattr(atoms, "extends") and len(conflict_hits) >= 2:
            try:
                atoms.extends(conflict_hits[0], conflict_hits[1])
            except Exception:
                pass
    except Exception:
        pass
