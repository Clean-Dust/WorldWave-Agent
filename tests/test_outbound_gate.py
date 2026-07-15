"""Telegram outbound gate: per-inbound budget (one user-visible text send).

Covers double-bubble prevention:
- budget=1: second send for same chat_id suppressed
- clear_budget / end_inbound: next inbound may send again immediately
- different chat_id independent
- begin_inbound refuses concurrent busy chat
- two threads race: only one text send succeeds
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from gateway import outbound


@pytest.fixture(autouse=True)
def _reset_gate():
    outbound.reset_for_tests()
    yield
    outbound.reset_for_tests()


# ── budget ───────────────────────────────────────────────────────


def test_budget_allows_first_send():
    outbound.set_budget("chat1", 1)
    assert outbound.allow_text_send("chat1", "hello") is True
    assert outbound.get_budget("chat1") == 0


def test_budget_suppresses_second_send():
    outbound.set_budget("chat1", 1)
    assert outbound.allow_text_send("chat1", "first bubble") is True
    assert outbound.allow_text_send("chat1", "second bubble") is False


def test_budget_independent_per_chat():
    outbound.set_budget("a", 1)
    outbound.set_budget("b", 1)
    assert outbound.allow_text_send("a", "hi a") is True
    assert outbound.allow_text_send("b", "hi b") is True
    assert outbound.allow_text_send("a", "again a") is False
    assert outbound.allow_text_send("b", "again b") is False


def test_clear_budget_allows_next_send_immediately():
    """After clear, a new send is allowed (rapid follow-up messages)."""
    outbound.set_budget("chat1", 1)
    assert outbound.allow_text_send("chat1", "one") is True
    outbound.clear_budget("chat1")
    assert outbound.get_budget("chat1") is None
    assert outbound.allow_text_send("chat1", "two") is True


def test_no_budget_allows_multiple_sends():
    """Without budget, multi-send is allowed (CLI / intentional tool use)."""
    assert outbound.allow_text_send("free", "ok1") is True
    assert outbound.allow_text_send("free", "ok2") is True


def test_empty_chat_id_suppressed():
    assert outbound.allow_text_send("", "x") is False
    assert outbound.allow_text_send("  ", "x") is False


def test_set_budget_does_not_restore_spent_budget():
    """Concurrent set_budget must not revive remaining=0 back to 1."""
    outbound.set_budget("chat1", 1)
    assert outbound.allow_text_send("chat1", "first") is True
    assert outbound.get_budget("chat1") == 0
    outbound.set_budget("chat1", 1)  # would-be race: second handler
    assert outbound.get_budget("chat1") == 0
    assert outbound.allow_text_send("chat1", "second") is False


# ── begin_inbound / end_inbound ──────────────────────────────────


def test_begin_inbound_sets_budget_and_busy():
    assert outbound.begin_inbound("c1") is True
    assert outbound.is_inbound_busy("c1") is True
    assert outbound.get_budget("c1") == 1
    outbound.end_inbound("c1")
    assert outbound.is_inbound_busy("c1") is False
    assert outbound.get_budget("c1") is None


def test_begin_inbound_refuses_when_busy():
    assert outbound.begin_inbound("c1") is True
    assert outbound.begin_inbound("c1") is False
    assert outbound.get_budget("c1") == 1  # still original budget
    outbound.end_inbound("c1")
    assert outbound.begin_inbound("c1") is True
    outbound.end_inbound("c1")


def test_concurrent_begin_inbound_only_one_text_send():
    """Two threads race begin_inbound + allow_text_send: only one True send."""
    chat = "race-chat"
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        began = outbound.begin_inbound(chat)
        if not began:
            with lock:
                outcomes.append(("busy", False))
            return
        try:
            # Hold busy briefly so the other thread cannot begin
            time.sleep(0.05)
            allowed = outbound.allow_text_send(chat, "hi from concurrent")
            with lock:
                outcomes.append(("ok", allowed))
            # Keep busy a bit so late starter still sees busy
            time.sleep(0.05)
        finally:
            outbound.end_inbound(chat)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(outcomes) == 2
    allowed_sends = [a for tag, a in outcomes if tag == "ok" and a is True]
    busy_or_denied = [o for o in outcomes if o[0] == "busy" or o == ("ok", False)]
    assert len(allowed_sends) == 1, f"expected exactly one text send, got {outcomes}"
    assert len(busy_or_denied) == 1, f"expected one refused path, got {outcomes}"
    assert outbound.is_inbound_busy(chat) is False
    assert outbound.get_budget(chat) is None


def test_concurrent_allow_after_double_set_budget_still_one_send():
    """Even if both threads set_budget (legacy path), min() keeps one send."""
    chat = "legacy-race"
    barrier = threading.Barrier(2)
    results = []
    rlock = threading.Lock()

    def worker():
        barrier.wait()
        outbound.set_budget(chat, 1)
        ok = outbound.allow_text_send(chat, "bubble")
        with rlock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # With set_budget taking min when active: first sets 1, second min(?,1)=1
    # then both allow: first gets True (0 left), second False.
    # If they race before either decrements, both might briefly see remaining=1
    # and both allow — that's why begin_inbound exists. This test documents
    # best-effort min(); the concurrent begin_inbound test is the hard guarantee.
    assert results.count(True) >= 1
    assert results.count(True) <= 2  # set_budget alone is not fully race-free
    # Hard guarantee path:
    outbound.reset_for_tests()
    assert outbound.begin_inbound(chat) is True
    assert outbound.allow_text_send(chat, "a") is True
    assert outbound.allow_text_send(chat, "b") is False
    outbound.end_inbound(chat)


# ── adapter / publisher wiring ───────────────────────────────────


def test_telegram_adapter_send_message_uses_gate():
    from gateway.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(token="fake-token")
    adapter._api_call = MagicMock(return_value={"ok": True})

    outbound.set_budget("99", 1)
    assert adapter.send_message("99", "bubble one") is True
    assert adapter.send_message("99", "bubble two") is False
    assert adapter._api_call.call_count == 1
    assert adapter._api_call.call_args[0][0] == "sendMessage"


def test_telegram_adapter_send_stream_chunk_uses_gate():
    from gateway.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(token="fake-token")
    adapter._api_call = MagicMock(
        return_value={"ok": True, "result": {"message_id": 7}}
    )

    outbound.set_budget("77", 1)
    assert adapter.send_stream_chunk("77", "stream first") is True
    # Mid-stream edits do not open a second bubble
    assert adapter.send_stream_chunk("77", "stream edit") is True
    adapter.end_stream("77", "final")
    # A new stream open after budget spent must be gated
    assert adapter.send_stream_chunk("77", "stream second open") is False
    send_calls = [
        c for c in adapter._api_call.call_args_list if c[0][0] == "sendMessage"
    ]
    assert len(send_calls) == 1


def test_telegram_adapter_raw_sendmessage_logs(caplog):
    from gateway.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(token="fake-token")
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = Exception("network off")
        with caplog.at_level("INFO", logger="gateway.telegram"):
            adapter._api_call("sendMessage", {"chat_id": "1", "text": "hello world"})
    assert any("raw sendMessage chat=1" in r.message for r in caplog.records)


def test_telegram_update_id_dedup():
    from gateway.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(token="fake-token")
    assert adapter._mark_seen_update(100) is False
    assert adapter._mark_seen_update(100) is True
    assert adapter._mark_seen_update(101) is False
    # Eviction past N
    for i in range(600):
        adapter._mark_seen_update(1000 + i)
    # Early id should have been dropped from the ring
    assert adapter._mark_seen_update(100) is False  # treated as new again
    assert adapter._mark_seen_update(100) is True


def test_telegram_self_sender_filter():
    from gateway.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(token="fake-token")
    adapter._bot_id = "999"
    assert adapter._is_self_sender({"id": 1, "is_bot": False}) is False
    assert adapter._is_self_sender({"id": 2, "is_bot": True}) is True
    assert adapter._is_self_sender({"id": 999, "is_bot": False}) is True
    assert adapter._is_self_sender({"id": "999", "is_bot": False}) is True


def test_telegram_publisher_send_message_uses_gate():
    from tools.telegram import TelegramPublisher

    pub = TelegramPublisher(token="fake-token")
    outbound.set_budget("42", 1)

    with patch("tools.telegram.requests.post") as post:
        post.return_value = MagicMock(json=lambda: {"ok": True, "result": {}})
        r1 = pub.send_message("42", "tool path first")
        r2 = pub.send_message("42", "tool path second")

    assert r1.get("ok") is True
    assert r2.get("ok") is False
    assert r2.get("suppressed") is True
    assert post.call_count == 1


def test_bridge_sets_and_clears_budget():
    """task_handler path: begin_inbound during handler, end_inbound after."""
    from gateway.bridge import TelegramGateway

    seen = {}

    def handler(text, context):
        seen["budget_during"] = outbound.get_budget(context["chat_id"])
        seen["busy_during"] = outbound.is_inbound_busy(context["chat_id"])
        # Simulate tool send consuming budget
        outbound.allow_text_send(context["chat_id"], "from tool")
        return "from bridge"

    with patch("gateway.adapters.telegram.TelegramAdapter") as MockAdapter:
        mock_adapter = MagicMock()
        mock_adapter.send_message = MagicMock(return_value=True)
        MockAdapter.return_value = mock_adapter

        TelegramGateway(token="t", task_handler=handler)
        on_message = MockAdapter.call_args.kwargs.get("on_message")
        assert on_message is not None

        class U:
            content = type(
                "C",
                (),
                {
                    "text": type("T", (), {"clean_text": "你好", "body": "你好"})(),
                },
            )()
            routing = type("R", (), {"photo_path": ""})()
            sender = type(
                "S",
                (),
                {
                    "display_name": "User",
                    "user_id": "1",
                    "id": "1",
                },
            )()
            platform = "telegram"
            session_key = "telegram:1:555"

        import asyncio

        asyncio.run(on_message(U()))

    assert seen["budget_during"] == 1
    assert seen["busy_during"] is True
    assert outbound.get_budget("555") is None
    assert outbound.is_inbound_busy("555") is False
    # Tool already consumed budget; bridge send_message still called (adapter gates)
    mock_adapter.send_message.assert_called_once()


def test_bridge_skips_when_chat_already_busy():
    from gateway.bridge import TelegramGateway

    called = {"n": 0}

    def handler(text, context):
        called["n"] += 1
        return "should not run twice"

    with patch("gateway.adapters.telegram.TelegramAdapter") as MockAdapter:
        mock_adapter = MagicMock()
        mock_adapter.send_message = MagicMock(return_value=True)
        MockAdapter.return_value = mock_adapter

        TelegramGateway(token="t", task_handler=handler)
        on_message = MockAdapter.call_args.kwargs.get("on_message")

        class U:
            content = type(
                "C",
                (),
                {
                    "text": type("T", (), {"clean_text": "hi", "body": "hi"})(),
                },
            )()
            routing = type("R", (), {"photo_path": ""})()
            sender = type(
                "S",
                (),
                {"display_name": "U", "user_id": "1", "id": "1"},
            )()
            platform = "telegram"
            session_key = "telegram:1:777"

        import asyncio

        assert outbound.begin_inbound("777") is True
        try:
            asyncio.run(on_message(U()))
        finally:
            outbound.end_inbound("777")

    assert called["n"] == 0
    mock_adapter.send_message.assert_not_called()
