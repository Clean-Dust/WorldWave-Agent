#!/usr/bin/env python3
"""Self-test: Telegram double-reply guard (no human needed).

What it proves:
1. Concurrent inbound for same chat → at most 1 user-visible text send
2. Single gateway path → exactly 1 sendMessage for one user "你好"
3. Optional: warn if this host's .env still has TELEGRAM_WW_TOKEN while
   WW_PRODUCTION_TELEGRAM_HOST is set (dual-poller footgun)

Does NOT need a real Telegram chat. Uses mocks for Bot API.

Usage:
  cd /path/to/worldwave && .venv/bin/python scripts/telegram_double_reply_selftest.py
  # or: pytest scripts/telegram_double_reply_selftest.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _check_dual_poller_footgun() -> list[str]:
    """Heuristic warnings only — never print token values."""
    warnings: list[str] = []
    env_path = ROOT / ".env"
    if not env_path.exists():
        return warnings
    active_token = False
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("TELEGRAM_WW_TOKEN=") and not line.startswith(
                    "TELEGRAM_WW_TOKEN_DISABLED"
                ):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val and "your_bot" not in val.lower():
                        active_token = True
    # If this machine is not the designated telegram host, warn
    host_role = os.environ.get("WW_ROLE", "").lower()
    if active_token and host_role in ("apple", "dev", "local"):
        warnings.append(
            "WW_ROLE=%s has active TELEGRAM_WW_TOKEN — risk of dual poller "
            "with Banana. Disable local token or set WW_ROLE=banana."
            % (host_role or "local")
        )
    if active_token and os.environ.get("WW_DISABLE_LOCAL_TELEGRAM") == "1":
        warnings.append(
            "WW_DISABLE_LOCAL_TELEGRAM=1 but TELEGRAM_WW_TOKEN still set in .env"
        )
    return warnings


def test_concurrent_begin_inbound_one_send() -> None:
    from gateway.outbound import (
        allow_text_send,
        begin_inbound,
        end_inbound,
        reset_for_tests,
    )

    reset_for_tests()
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        began = begin_inbound("selftest-chat")
        if not began:
            with lock:
                outcomes.append(("busy", False))
            return
        try:
            time.sleep(0.05)
            sent = allow_text_send("selftest-chat", "hello")
            with lock:
                outcomes.append(("sent" if sent else "blocked", sent))
        finally:
            end_inbound("selftest-chat")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    true_sends = [o for o in outcomes if o[1]]
    assert len(true_sends) == 1, outcomes
    assert any(o[0] == "busy" for o in outcomes), outcomes


def test_bridge_one_send_for_nihao() -> None:
    from gateway import outbound
    from gateway.bridge import TelegramGateway

    outbound.reset_for_tests()
    sends: list[str] = []

    def handler(text, context):
        time.sleep(0.02)
        return "你好！有什么我可以帮你的吗？😊"

    with patch("gateway.adapters.telegram.TelegramAdapter") as MockA:
        mock = MagicMock()

        def sm(chat_id, text, **kwargs):
            from gateway.outbound import allow_text_send

            if not allow_text_send(str(chat_id), text or ""):
                return False
            sends.append(text)
            return True

        mock.send_message.side_effect = sm
        MockA.return_value = mock
        TelegramGateway(token="fake", task_handler=handler)
        on_message = MockA.call_args.kwargs["on_message"]

        class U:
            content = type(
                "C",
                (),
                {"text": type("T", (), {"clean_text": "你好", "body": "你好"})()},
            )()
            routing = type("R", (), {"photo_path": ""})()
            sender = type(
                "S",
                (),
                {
                    "display_name": "clean",
                    "user_id": "5233788587",
                    "id": "5233788587",
                },
            )()
            platform = "telegram"
            session_key = "telegram:5233788587:5233788587"

        # Fire two concurrent handlers (old dual-poller / fire-and-forget shape)
        barrier = threading.Barrier(2)

        def run_one():
            barrier.wait()
            asyncio.run(on_message(U()))

        t1 = threading.Thread(target=run_one)
        t2 = threading.Thread(target=run_one)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert len(sends) == 1, sends
    assert "你好" in sends[0] or "嘿" in sends[0] or "帮" in sends[0]


def test_adapter_send_message_gated() -> None:
    from gateway import outbound
    from gateway.adapters.telegram import TelegramAdapter

    outbound.reset_for_tests()
    adapter = TelegramAdapter(token="fake")
    adapter._api_call = MagicMock(return_value={"ok": True})
    outbound.begin_inbound("99")
    assert adapter.send_message("99", "one") is True
    assert adapter.send_message("99", "two") is False
    assert adapter._api_call.call_count == 1
    outbound.end_inbound("99")


def main() -> int:
    warnings = _check_dual_poller_footgun()
    for w in warnings:
        print("WARN:", w)

    tests = [
        test_concurrent_begin_inbound_one_send,
        test_bridge_one_send_for_nihao,
        test_adapter_send_message_gated,
    ]
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            t()
            print("PASS", name)
        except Exception as e:
            failed += 1
            print("FAIL", name, e)
    if failed:
        print(f"RESULT: {failed}/{len(tests)} failed")
        return 1
    print(f"RESULT: {len(tests)}/{len(tests)} passed (no human Telegram needed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
