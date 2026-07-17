"""core/public_reply.py — Shared user-facing reply extraction.

Single source of truth for stripping internal mechanism strings
(Reflex arc, direct response, traceback, bare error prefixes) and
picking the best user-visible text from a /ww/run result dict.

Product law (Gate 0 honesty):
- Only tools in ``_REPLY_TOOLS`` may supply the chat reply.
- Memory / store tool dumps must never become the user-facing response.
- Dump-like ``key: value`` blocks and spiral JSON bodies are rejected.
- Empty string is preferred over a raw inject/tool dump.

Used by:
- server.run_task (always attaches clean top-level ``response``)
- ww_cli (terminal chat display)
- gateway task handler (platform chat replies)

Debug fields (summary, evaluation.reason, metrics) may still contain
internal labels — that is intentional. Only fields meant for users
must go through this module.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

# Tools that intentionally produce user-facing chat text
_REPLY_TOOLS = frozenset({"reflex_text", "respond", "reply", "final_answer"})

# Memory / store tools — their outputs are internal data, never chat replies
_MEMORY_TOOLS = frozenset({
    "recall_mine",
    "remember",
    "forget",
    "memory_search",
    "memory_store",
    "memory_recall",
    "recall",
    "search",
    "switch_topic",
    "memory_get",
    "memory_list",
    "hippo_search",
    "hippo_promote",
    "atom_search",
    "ltm_search",
})

# Short multi-paragraph greets (each para under this length) collapse to first
_GREETING_PARA_MAX = 100

# Top-level keys clients may already populate
_TOP_LEVEL_KEYS = ("response", "reply", "output", "message")

# Per-action result keys that can hold user text
_RESULT_TEXT_KEYS = ("output", "text", "response", "content", "message", "reply")

# evaluation keys (skip internal via is_internal_response_text)
_EVAL_KEYS = ("response", "summary")

# Multi-line snake_key: value memory dumps
_KV_LINE_RE = re.compile(r"^[A-Za-z_][\w]*:\s*\S.*$", re.MULTILINE)

# Markers that look like a full /ww/run JSON body pasted as chat
_SPIRAL_JSON_MARKERS = (
    "spirals_completed",
    '"results":',
    '"results" :',
    "\"status\": \"completed\"",
    "\"status\":\"completed\"",
)

# Gate 0.6: StateManager.summary() metrics must never become chat text
_METRICS_MARKERS = (
    "active_interrupts",
    "interrupt_history",
    "total_checkpoints",
    "current_spiral",
    "total_spirals",
)


def is_metrics_dump(text: Any) -> bool:
    """True if value is (or stringifies as) a spiral state metrics dict."""
    if isinstance(text, dict):
        keys = set(text.keys())
        if keys & {
            "active_interrupts",
            "interrupt_history",
            "total_checkpoints",
            "session_id",
            "current_spiral",
            "total_spirals",
        }:
            # Metrics-shaped dict (not a normal evaluation payload)
            if "active_interrupts" in keys or "interrupt_history" in keys:
                return True
            if "total_checkpoints" in keys and "current_phase" in keys:
                return True
        return False
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    lower = s.lower()
    # Dict-like string from str(state.summary()) or json.dumps
    hit = sum(1 for m in _METRICS_MARKERS if m in lower)
    if hit >= 2:
        return True
    if "active_interrupts" in lower and "interrupt" in lower:
        return True
    if "rewind:" in lower and "phase" in lower and "repeated" in lower:
        return True
    return False


def is_dump_like_text(text: Any) -> bool:
    """True if text looks like a raw memory/tool dump or spiral JSON, not chat.

    Detects:
    - multi-line ``snake_key: value`` blocks (≥2 KV lines)
    - text that starts with a common fact key pattern and is mostly KV lines
    - JSON that looks like a full ``/ww/run`` result body
    - StateManager metrics dumps (active_interrupts / interrupt_history)
    """
    if text is None:
        return False
    if isinstance(text, dict):
        return is_metrics_dump(text)
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False

    if is_metrics_dump(s):
        return True

    lower = s.lower()
    # Full spiral / run result JSON
    if s.startswith("{") and any(m in s or m in lower for m in _SPIRAL_JSON_MARKERS):
        return True
    if any(m in s for m in ("spirals_completed", '"results":', '"results" :')):
        # JSON-ish body even without leading brace (truncated paste)
        if '"actions"' in s or '"evaluation"' in s or "spirals_completed" in s:
            return True

    non_empty = [ln.strip() for ln in s.splitlines() if ln.strip()]
    kv_lines = [ln for ln in non_empty if _KV_LINE_RE.match(ln)]

    if len(kv_lines) >= 2:
        # Mostly dump: ≥2 KV lines and little free-form prose between them
        kv_ratio = len(kv_lines) / max(len(non_empty), 1)
        if kv_ratio >= 0.5:
            return True
        # First few lines are pure key: value → dump
        head = non_empty[: min(4, len(non_empty))]
        if head and all(_KV_LINE_RE.match(ln) for ln in head):
            return True

    # Single-line pure inject style (home_city: ZetaCity) is dump-like as whole reply
    if len(non_empty) == 1 and re.match(r"^[a-z][a-z0-9_]*:\s+\S", non_empty[0]):
        return True

    return False


def is_internal_response_text(text: Any) -> bool:
    """True if text looks like an internal status leak, not user-facing content."""
    if text is None:
        return True
    if isinstance(text, dict):
        return True  # never promote raw dicts (metrics / tool payloads)
    if not isinstance(text, str):
        return True
    s = text.strip()
    if not s:
        return True
    lower = s.lower()
    if "reflex arc" in lower:
        return True
    if "direct response" in lower:
        return True
    if lower.startswith("error:"):
        return True
    if "traceback" in lower:
        return True
    if is_dump_like_text(s):
        return True
    return False


def collapse_multi_greeting(text: Any) -> str:
    """Keep only the first paragraph when the reply is multi short greets.

    If the reply has ≥2 paragraphs (split on blank lines), and every
    paragraph is shorter than 100 characters (typical multi-bubble
    small-talk), return the first paragraph only. Otherwise return
    the original stripped text unchanged.

    Pure function — no I/O. Safe to apply on all user-facing replies.
    """
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if not s:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", s) if p.strip()]
    if len(paragraphs) < 2:
        return s
    if all(len(p) < _GREETING_PARA_MAX for p in paragraphs):
        return paragraphs[0]
    return s


def _clean(val: Any) -> str:
    """Return stripped user-safe string, or empty if unusable/internal/dump.

    Gate 0.6: dict metrics / non-string dumps never become user response.
    """
    if isinstance(val, dict):
        return ""
    if not isinstance(val, str):
        return ""
    s = val.strip()
    if not s or is_internal_response_text(s) or is_metrics_dump(s):
        return ""
    return collapse_multi_greeting(s)


def _iter_spirals(result: Mapping[str, Any]):
    spiral_results = result.get("results") or []
    if not isinstance(spiral_results, list):
        return
    for r in spiral_results:
        if isinstance(r, dict):
            yield r


def extract_user_response(result: Optional[Dict[str, Any]]) -> str:
    """Best user-facing reply text from a /ww/run result dict.

    Priority:
      1. Top-level response/reply/output/message (if clean, non-dump)
      2. evaluation.response / evaluation.summary (if not internal)
      3. Only ``_REPLY_TOOLS`` action result.output|text|response

    Memory tools (``recall_mine``, ``remember``, ``search``, …) never supply
    the chat reply by themselves. Priority-4 “any action output” is removed.

    Never returns internal leaks (Reflex arc, direct response, traceback,
    memory dumps, spiral JSON). Empty string is OK when no real user content
    exists — callers may fall back to “Done.”
    """
    if not isinstance(result, dict):
        return ""

    # 1. Top-level fields (server always sets clean ``response`` after wrap)
    for key in _TOP_LEVEL_KEYS:
        got = _clean(result.get(key))
        if got:
            return got

    # Reject top-level summary when it is a metrics dict (legacy poisoned runs)
    top_summary = result.get("summary")
    if is_metrics_dump(top_summary):
        pass  # never promote

    # 2. evaluation.response / evaluation.summary (skip internal / metrics)
    for r in _iter_spirals(result):
        ev = r.get("evaluation") or {}
        if not isinstance(ev, dict):
            continue
        for key in _EVAL_KEYS:
            raw = ev.get(key)
            if is_metrics_dump(raw):
                continue
            got = _clean(raw)
            if got:
                return got

    # 3. Only reflex_text / respond-style tools (never memory / arbitrary tools)
    # Walk spirals newest-first so a later synthesis wins over an earlier stub.
    spiral_list = list(_iter_spirals(result))
    for r in reversed(spiral_list):
        actions = list(r.get("actions") or [])
        # Prefer last reply-tool action (synthesis often appended after tools)
        for a in reversed(actions):
            if not isinstance(a, dict):
                continue
            tool = str(a.get("tool") or "").lower()
            if tool not in _REPLY_TOOLS:
                continue
            if tool in _MEMORY_TOOLS:
                continue
            res = a.get("result")
            # Plain-string result (some adapters)
            if isinstance(res, str):
                got = _clean(res)
                if got:
                    return got
                continue
            if not isinstance(res, dict):
                continue
            if res.get("success") is False:
                continue
            for key in _RESULT_TEXT_KEYS:
                got = _clean(res.get(key))
                if got:
                    return got

    # Priority 4 removed: never promote arbitrary / memory tool dumps as chat.

    return ""


def public_reply(text: Any, fallback: str = "") -> str:
    """Sanitize a single reply string for chat surfaces.

    Strips internal mechanism prefixes, stack traces, and dump-like
    memory blocks. Returns ``fallback`` (or empty) when the text is not
    safe to show.
    """
    if not text:
        return fallback
    t = str(text).strip()
    if not t:
        return fallback
    if is_internal_response_text(t):
        return fallback or ""
    # Extra short-circuit for status-style prefixes gateway used to filter
    bad_prefixes = (
        "status=",
        "[completed]",
        "[failed]",
        "[interrupted]",
        "WW not initialized",
        "loop.run",
        "## ",
    )
    if any(t.startswith(p) for p in bad_prefixes):
        return fallback or ""
    return collapse_multi_greeting(t)
