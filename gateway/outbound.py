"""Shared outbound text-send gate for Telegram (and other chat adapters).

Prevents double-bubble replies when both:
  - gateway/bridge returns a reply and calls adapter.send_message
  - tools/telegram.TelegramPublisher.send_message (telegram_send tool)

Also applies a short time-window dedup so paths that never set a budget
still cannot fire two user-visible texts in quick succession.

Module-level, thread-safe. Budget is per chat_id and set only around
gateway inbound task handling (budget=1).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

log = logging.getLogger("gateway.outbound")

# Seconds: second text send to same chat_id within this window is suppressed
DEDUP_WINDOW_SEC = 6.0

_lock = threading.Lock()
# chat_id -> remaining allowed user-visible text sends (None / missing = no budget)
_budgets: Dict[str, int] = {}
# chat_id -> monotonic timestamp of last *allowed* text send
_last_send_at: Dict[str, float] = {}


def _key(chat_id: str) -> str:
    return str(chat_id or "").strip()


def set_budget(chat_id: str, n: int = 1) -> None:
    """Allow at most *n* user-visible text sends for this chat_id."""
    k = _key(chat_id)
    if not k:
        return
    with _lock:
        _budgets[k] = max(0, int(n))


def clear_budget(chat_id: str) -> None:
    """Remove send budget for chat_id (inbound handler finished)."""
    k = _key(chat_id)
    if not k:
        return
    with _lock:
        _budgets.pop(k, None)


def get_budget(chat_id: str) -> Optional[int]:
    """Return remaining budget or None if no budget is set."""
    k = _key(chat_id)
    with _lock:
        return _budgets.get(k)


def reset_for_tests() -> None:
    """Clear all gate state (unit tests only)."""
    with _lock:
        _budgets.clear()
        _last_send_at.clear()


def allow_text_send(chat_id: str, text: str = "") -> bool:
    """Return True if a user-visible text send is allowed.

    Rules (any failure → suppress, do not hit API):
      1. Time-window: same chat_id already sent within DEDUP_WINDOW_SEC
      2. Budget: budget is set and remaining <= 0

    On allow: decrement budget (if set), record timestamp, log INFO.
    On suppress: log WARNING.
    """
    k = _key(chat_id)
    snippet = (text or "").replace("\n", " ")[:120]
    if not k:
        log.warning("Telegram send suppressed: empty chat_id text=%r", snippet)
        return False

    now = time.monotonic()
    with _lock:
        last = _last_send_at.get(k)
        if last is not None and (now - last) < DEDUP_WINDOW_SEC:
            log.warning(
                "Telegram send suppressed (time-window %.1fs): chat=%s text=%r",
                DEDUP_WINDOW_SEC,
                k,
                snippet,
            )
            return False

        if k in _budgets:
            remaining = _budgets[k]
            if remaining <= 0:
                log.warning(
                    "Telegram send suppressed (budget exceeded): chat=%s text=%r",
                    k,
                    snippet,
                )
                return False
            _budgets[k] = remaining - 1

        _last_send_at[k] = now

    log.info("Telegram send chat=%s text=%s", k, snippet)
    return True
