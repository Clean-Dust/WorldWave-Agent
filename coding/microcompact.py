"""coding/microcompact.py — Bound long tool results for the model context.

Truncates long coding-tool outputs to head + tail with a stable fingerprint
so the agent can still identify repeated failure modes without flooding tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable, Dict, List, Optional, Union

DEFAULT_LIMIT = int(os.environ.get("WW_CODING_MICROCOMPACT_LIMIT", "6000"))
HEAD_RATIO = 0.55
TAIL_RATIO = 0.35


def fingerprint(text: str) -> str:
    """Stable short hash of text (normalized whitespace)."""
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def compact_text(text: str, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Truncate *text* to head+tail if longer than *limit*.

    Returns a dict with:
      - text: possibly truncated string
      - truncated: bool
      - fingerprint: 16-char sha256 prefix of original
      - original_length: int
    """
    text = text if text is not None else ""
    if not isinstance(text, str):
        text = str(text)
    fp = fingerprint(text)
    original_length = len(text)
    if original_length <= limit:
        return {
            "text": text,
            "truncated": False,
            "fingerprint": fp,
            "original_length": original_length,
        }

    head_len = int(limit * HEAD_RATIO)
    tail_len = int(limit * TAIL_RATIO)
    # Leave room for marker
    marker = f"\n\n...[microcompact truncated {original_length} chars, fingerprint={fp}]...\n\n"
    available = limit - len(marker)
    if available < 64:
        available = max(32, limit // 2)
        head_len = available // 2
        tail_len = available - head_len
        marker = f"\n...[{fp}]...\n"
    else:
        head_len = int(available * HEAD_RATIO / (HEAD_RATIO + TAIL_RATIO))
        tail_len = available - head_len

    compacted = text[:head_len] + marker + text[-tail_len:]
    return {
        "text": compacted,
        "truncated": True,
        "fingerprint": fp,
        "original_length": original_length,
    }


def compact_result(result: Any, limit: int = DEFAULT_LIMIT) -> Any:
    """Compact a tool result (str / dict / list / other) for model consumption.

    - str: truncate with fingerprint metadata if needed
    - dict: compact long string values; attach top-level fingerprint when truncated
    - list: compact each element
    - other: JSON-serialize then compact as text if large
    """
    if result is None:
        return result

    if isinstance(result, str):
        c = compact_text(result, limit=limit)
        if not c["truncated"]:
            return result
        return {
            "output": c["text"],
            "truncated": True,
            "fingerprint": c["fingerprint"],
            "original_length": c["original_length"],
        }

    if isinstance(result, dict):
        out: Dict[str, Any] = {}
        any_trunc = False
        fps: List[str] = []
        # Budget per long string field
        field_limit = max(512, limit // 2)
        for k, v in result.items():
            if isinstance(v, str) and len(v) > field_limit:
                c = compact_text(v, limit=field_limit)
                out[k] = c["text"]
                any_trunc = any_trunc or c["truncated"]
                fps.append(c["fingerprint"])
            elif isinstance(v, (dict, list)):
                # Nested: serialize budget check
                try:
                    s = json.dumps(v, default=str)
                except (TypeError, ValueError):
                    s = str(v)
                if len(s) > field_limit:
                    c = compact_text(s, limit=field_limit)
                    out[k] = c["text"] if not isinstance(v, (dict, list)) else json.loads(
                        c["text"]
                    ) if False else c["text"]
                    # Keep compact string form for huge nested structures
                    out[k] = c["text"]
                    any_trunc = True
                    fps.append(c["fingerprint"])
                else:
                    out[k] = v
            else:
                out[k] = v
        if any_trunc:
            out.setdefault("truncated", True)
            if "fingerprint" not in out and fps:
                out["fingerprint"] = fps[0]
            elif "fingerprint" not in out:
                try:
                    out["fingerprint"] = fingerprint(json.dumps(result, default=str))
                except (TypeError, ValueError):
                    out["fingerprint"] = fingerprint(str(result))
        return out

    if isinstance(result, list):
        return [compact_result(item, limit=max(256, limit // max(1, min(len(result), 10)))) for item in result]

    # Fallback: stringify large objects
    try:
        s = json.dumps(result, default=str)
    except (TypeError, ValueError):
        s = str(result)
    if len(s) <= limit:
        return result
    c = compact_text(s, limit=limit)
    return {
        "output": c["text"],
        "truncated": True,
        "fingerprint": c["fingerprint"],
        "original_length": c["original_length"],
    }


def wrap_handler(handler: Callable, limit: int = DEFAULT_LIMIT) -> Callable:
    """Wrap a tool handler so its return value is microcompacted."""

    def _wrapped(*args, **kwargs):
        result = handler(*args, **kwargs)
        return compact_result(result, limit=limit)

    # Preserve name for debugging
    _wrapped.__name__ = getattr(handler, "__name__", "wrapped") + "_microcompact"
    _wrapped.__wrapped__ = handler  # type: ignore[attr-defined]
    return _wrapped


def wrap_tools(tools: List[Dict], limit: int = DEFAULT_LIMIT, prefix: str = "coding_") -> List[Dict]:
    """Return a new tool list with handlers wrapped for *prefix* tools."""
    out = []
    for t in tools:
        name = t.get("name", "")
        if name.startswith(prefix) and callable(t.get("handler")):
            nt = dict(t)
            nt["handler"] = wrap_handler(t["handler"], limit=limit)
            out.append(nt)
        else:
            out.append(t)
    return out
