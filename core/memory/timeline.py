"""Lightweight timeline store for temporal / event-ordering memory (P1.1 / P2.2).

Events extracted from fact ingest or free text are stored with a sortable
``ts_sort_key`` so probes can compute day gaps and ordered sequences without
relying only on prose recall.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ISO, slash, and "Mon DD, YYYY"
_RE_DATE = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}"
    r")\b",
    re.I,
)

# "on DATE I/we ..." / "DATE: event" / "event on DATE"
_RE_EVENT_ON = re.compile(
    r"(?:"
    r"(?:on|since|from|until|before|after|by)\s+"
    r"(?P<d1>"
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}"
    r")"
    r"\s*[,:]?\s*(?P<e1>[^.;\n]{3,120})"
    r"|(?P<e2>[A-Za-z][^.;\n]{2,80}?)\s+"
    r"(?:on|at)\s+"
    r"(?P<d2>"
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}"
    r")"
    r")",
    re.I,
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass
class TimelineEvent:
    """One dated (or relative) event on an entity timeline."""

    ts_sort_key: float
    text: str
    entity_id: str = ""
    date_str: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TimelineEvent":
        return cls(
            ts_sort_key=float(d.get("ts_sort_key") or 0.0),
            text=str(d.get("text") or ""),
            entity_id=str(d.get("entity_id") or ""),
            date_str=str(d.get("date_str") or ""),
            event_id=str(d.get("event_id") or uuid.uuid4().hex[:12]),
            meta=dict(d.get("meta") or {}),
        )


def parse_date_to_sort_key(raw: str) -> Optional[float]:
    """Parse a date string to a UTC midnight unix timestamp (sort key)."""
    s = (raw or "").strip()
    if not s:
        return None
    # ISO
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except ValueError:
        pass
    # M/D/Y or D/M/Y — prefer US M/D when ambiguous and first > 12 is day-first
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 70 else 1900
        # If first component > 12 → day/month/year
        if a > 12:
            day, month = a, b
        elif b > 12:
            month, day = a, b
        else:
            month, day = a, b  # default US
        try:
            dt = datetime(y, month, day, tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    # Mon DD, YYYY
    m = re.fullmatch(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
        r"(\d{1,2}),?\s+(\d{4})",
        s,
        re.I,
    )
    if m:
        mon = _MONTHS.get(m.group(1).lower()[:3], 0)
        if not mon:
            mon = _MONTHS.get(m.group(1).lower(), 0)
        try:
            dt = datetime(int(m.group(3)), mon, int(m.group(2)), tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def days_between_dates(a: str, b: str) -> Optional[int]:
    """Absolute day gap between two parseable date strings."""
    ka = parse_date_to_sort_key(a)
    kb = parse_date_to_sort_key(b)
    if ka is None or kb is None:
        return None
    return abs(int(round((kb - ka) / 86400.0)))


def extract_timeline_events(
    text: str, *, entity_id: str = ""
) -> List[TimelineEvent]:
    """Best-effort dated events from free text."""
    text = (text or "").strip()
    if not text:
        return []
    events: List[TimelineEvent] = []
    seen: set = set()

    for m in _RE_EVENT_ON.finditer(text):
        d = (m.group("d1") or m.group("d2") or "").strip()
        e = (m.group("e1") or m.group("e2") or "").strip()
        e = re.sub(r"\s+", " ", e).strip(" ,;:")
        if not d or not e or len(e) < 3:
            continue
        key = (d.lower(), e.lower()[:80])
        if key in seen:
            continue
        seen.add(key)
        sk = parse_date_to_sort_key(d)
        if sk is None:
            continue
        events.append(
            TimelineEvent(
                ts_sort_key=sk,
                text=e[:200],
                entity_id=entity_id,
                date_str=d,
            )
        )

    # Fallback: bare dates with surrounding clause
    if not events:
        for m in _RE_DATE.finditer(text):
            d = m.group(1)
            sk = parse_date_to_sort_key(d)
            if sk is None:
                continue
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 60)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            key = (d.lower(), snippet.lower()[:80])
            if key in seen:
                continue
            seen.add(key)
            events.append(
                TimelineEvent(
                    ts_sort_key=sk,
                    text=snippet[:200],
                    entity_id=entity_id,
                    date_str=d,
                )
            )

    events.sort(key=lambda e: (e.ts_sort_key, e.text))
    return events


def format_event_order(events: Sequence[Any], *, max_events: int = 20) -> str:
    """Numbered chronological sequence for event_ordering probes (P2.2)."""
    items: List[TimelineEvent] = []
    for e in events or []:
        if isinstance(e, TimelineEvent):
            items.append(e)
        elif isinstance(e, dict):
            try:
                items.append(TimelineEvent.from_dict(e))
            except Exception:
                continue
        else:
            continue
    items = sorted(items, key=lambda x: (x.ts_sort_key, x.text))[:max_events]
    if not items:
        return ""
    lines = ["event order (chronological):"]
    for i, e in enumerate(items, 1):
        date_bit = f" ({e.date_str})" if e.date_str else ""
        lines.append(f"{i}. {e.text}{date_bit}")
    return "\n".join(lines)


class TimelineStore:
    """Append-only per-entity timeline (JSONL under data_dir)."""

    def __init__(self, data_dir: str = ""):
        self.data_dir = data_dir or ""
        self._events: List[TimelineEvent] = []
        self._loaded = False
        if self.data_dir:
            Path(self.data_dir).mkdir(parents=True, exist_ok=True)
            self._path = Path(self.data_dir) / "timeline.jsonl"
        else:
            self._path = None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path or not self._path.is_file():
            return
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._events.append(TimelineEvent.from_dict(json.loads(line)))
                except Exception:
                    continue
        except Exception:
            pass

    def _persist(self, event: TimelineEvent) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass

    def append(
        self,
        text: str,
        *,
        entity_id: str = "",
        date_str: str = "",
        ts_sort_key: Optional[float] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> TimelineEvent:
        self._ensure_loaded()
        sk = ts_sort_key
        if sk is None and date_str:
            sk = parse_date_to_sort_key(date_str)
        if sk is None:
            sk = time.time()
        ev = TimelineEvent(
            ts_sort_key=float(sk),
            text=(text or "").strip()[:500],
            entity_id=(entity_id or "").strip(),
            date_str=(date_str or "").strip(),
            meta=dict(meta or {}),
        )
        self._events.append(ev)
        self._persist(ev)
        return ev

    def append_from_text(self, text: str, *, entity_id: str = "") -> List[TimelineEvent]:
        """Extract dated events from text and append all."""
        out: List[TimelineEvent] = []
        for e in extract_timeline_events(text, entity_id=entity_id):
            stored = self.append(
                e.text,
                entity_id=entity_id or e.entity_id,
                date_str=e.date_str,
                ts_sort_key=e.ts_sort_key,
            )
            out.append(stored)
        return out

    def list_events(
        self, entity_id: str = "", *, limit: int = 100
    ) -> List[TimelineEvent]:
        self._ensure_loaded()
        eid = (entity_id or "").strip()
        items = [
            e
            for e in self._events
            if not eid or e.entity_id == eid or not e.entity_id
        ]
        items.sort(key=lambda e: (e.ts_sort_key, e.text))
        return items[: max(1, int(limit))]

    def days_between(
        self,
        a_query: str,
        b_query: str,
        *,
        entity_id: str = "",
    ) -> Optional[int]:
        """Best-effort day gap between two event queries (text or date)."""
        # Direct date strings
        direct = days_between_dates(a_query, b_query)
        if direct is not None:
            return direct

        events = self.list_events(entity_id=entity_id, limit=200)
        if not events:
            # Try parsing dates embedded in queries
            da = _RE_DATE.search(a_query or "")
            db = _RE_DATE.search(b_query or "")
            if da and db:
                return days_between_dates(da.group(1), db.group(1))
            return None

        def match(q: str) -> Optional[TimelineEvent]:
            qn = (q or "").strip().lower()
            if not qn:
                return None
            # Exact date match
            dm = _RE_DATE.search(q or "")
            if dm:
                d = dm.group(1)
                for e in events:
                    if e.date_str and e.date_str.lower() == d.lower():
                        return e
                    if parse_date_to_sort_key(e.date_str) == parse_date_to_sort_key(d):
                        return e
            # Substring / token overlap on text
            best: Optional[TimelineEvent] = None
            best_score = 0
            tokens = [t for t in re.findall(r"[a-z0-9]+", qn) if len(t) > 2]
            for e in events:
                blob = f"{e.text} {e.date_str}".lower()
                if qn in blob:
                    return e
                score = sum(1 for t in tokens if t in blob)
                if score > best_score:
                    best_score = score
                    best = e
            return best if best_score > 0 else None

        ea = match(a_query)
        eb = match(b_query)
        if ea is None or eb is None:
            return None
        return abs(int(round((eb.ts_sort_key - ea.ts_sort_key) / 86400.0)))


# Module-level convenience (stateless helpers mirror store API names)
def list_events(
    store: TimelineStore, entity_id: str = "", *, limit: int = 100
) -> List[TimelineEvent]:
    return store.list_events(entity_id, limit=limit)


def days_between(
    store: TimelineStore,
    a_query: str,
    b_query: str,
    *,
    entity_id: str = "",
) -> Optional[int]:
    return store.days_between(a_query, b_query, entity_id=entity_id)
