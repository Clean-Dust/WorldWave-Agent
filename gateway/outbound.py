"""Shared outbound text-send gate for Telegram (and other chat adapters).

Prevents double-bubble replies when both:
  - gateway/bridge returns a reply and calls adapter.send_message
  - tools/telegram.TelegramPublisher.send_message (telegram_send tool)

While an inbound gateway handler is active it sets budget=1 for that chat_id.
Any path that tries a second user-visible text send is suppressed.

No global time-window: after the inbound handler clears budget, the next user
message may reply immediately (rapid back-and-forth must not be blocked).

Module-level, thread-safe.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

log = logging.getLogger("gateway.outbound")

_lock = threading.Lock()
# chat_id -> remaining allowed user-visible text sends (missing = no budget)
_budgets: Dict[str, int] = {}


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


def allow_text_send(chat_id: str, text: str = "") -> bool:
    """Return True if a user-visible text send is allowed.

    Rules:
      - empty chat_id → suppress
      - budget set and remaining <= 0 → suppress
      - budget set and remaining > 0 → allow and decrement
      - no budget → allow (CLI / intentional multi-send outside gateway)

    On allow: log INFO with chat + text preview.
    On suppress: log WARNING.
    """
    k = _key(chat_id)
    snippet = (text or "").replace("\n", " ")[:120]
    if not k:
        log.warning("Telegram send suppressed: empty chat_id text=%r", snippet)
        return False

    with _lock:
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

    log.info("Telegram send chat=%s text=%s", k, snippet)
    return True
