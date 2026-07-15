"""core/public_reply.py — Shared user-facing reply extraction.

Single source of truth for stripping internal mechanism strings
(Reflex arc, direct response, traceback, bare error prefixes) and
picking the best user-visible text from a /ww/run result dict.

Used by:
- server.run_task (always attaches clean top-level ``response``)
- ww_cli (terminal chat display)
- gateway task handler (platform chat replies)

Debug fields (summary, evaluation.reason, metrics) may still contain
internal labels — that is intentional. Only fields meant for users
must go through this module.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

# Tools that intentionally produce user-facing chat text
_REPLY_TOOLS = frozenset({"reflex_text", "respond", "reply", "final_answer"})

# Top-level keys clients may already populate
_TOP_LEVEL_KEYS = ("response", "reply", "output", "message")

# Per-action result keys that can hold user text
_RESULT_TEXT_KEYS = ("output", "text", "response")

# evaluation keys (skip internal via is_internal_response_text)
_EVAL_KEYS = ("response", "summary")


def is_internal_response_text(text: Any) -> bool:
    """True if text looks like an internal status leak, not user-facing content."""
    if not text or not isinstance(text, str):
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
    return False


def _clean(val: Any) -> str:
    """Return stripped user-safe string, or empty if unusable/internal."""
    if not isinstance(val, str):
        return ""
    s = val.strip()
    if not s or is_internal_response_text(s):
        return ""
    return s


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
      1. Top-level response/reply/output/message
      2. evaluation.response / evaluation.summary (if not internal)
      3. reflex_text / respond action result.output|text|response
      4. Any successful action result with output/text/response

    Never returns internal leaks (Reflex arc, direct response, traceback, …).
    Empty string is OK when no real user content exists (e.g. tool-only
    reflex whose summary is only "Reflex arc: …").
    """
    if not isinstance(result, dict):
        return ""

    # 1. Top-level fields (server always sets clean ``response`` after wrap)
    for key in _TOP_LEVEL_KEYS:
        got = _clean(result.get(key))
        if got:
            return got

    # 2. evaluation.response / evaluation.summary (skip internal)
    for r in _iter_spirals(result):
        ev = r.get("evaluation") or {}
        if not isinstance(ev, dict):
            continue
        for key in _EVAL_KEYS:
            got = _clean(ev.get(key))
            if got:
                return got

    # 3. Prefer reflex_text / respond-style tools
    for r in _iter_spirals(result):
        for a in (r.get("actions") or []):
            if not isinstance(a, dict):
                continue
            tool = str(a.get("tool") or "").lower()
            if tool not in _REPLY_TOOLS:
                continue
            res = a.get("result") or {}
            if not isinstance(res, dict):
                continue
            for key in _RESULT_TEXT_KEYS:
                got = _clean(res.get(key))
                if got:
                    return got

    # 4. Walk any successful action for output/text/response
    for r in _iter_spirals(result):
        for a in (r.get("actions") or []):
            if not isinstance(a, dict):
                continue
            res = a.get("result") or {}
            if not isinstance(res, dict):
                continue
            if res.get("success") is False:
                continue
            for key in _RESULT_TEXT_KEYS:
                got = _clean(res.get(key))
                if got:
                    return got

    return ""


def public_reply(text: Any, fallback: str = "") -> str:
    """Sanitize a single reply string for chat surfaces.

    Strips internal mechanism prefixes and stack traces. Returns
    ``fallback`` (or empty) when the text is not safe to show.
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
    return t
