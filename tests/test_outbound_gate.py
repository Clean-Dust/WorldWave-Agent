"""Telegram outbound gate: per-inbound budget (one user-visible text send).

Covers double-bubble prevention:
- budget=1: second send for same chat_id suppressed
- clear_budget: next inbound may send again immediately
- different chat_id independent
"""

from __future__ import annotations

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
    """task_handler path: budget active during handler, cleared after."""
    from gateway.bridge import TelegramGateway

    seen = {}

    def handler(text, context):
        seen["budget_during"] = outbound.get_budget(context["chat_id"])
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
    assert outbound.get_budget("555") is None
    mock_adapter.send_message.assert_called_once()
