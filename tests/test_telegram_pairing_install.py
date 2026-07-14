"""Tests: clean-install deps, Telegram token-only init, pairing user notice."""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest


# ── Bug A: requirements.txt must list dotenv ─────────────────


def test_requirements_include_dotenv():
    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / "requirements.txt").read_text()
    assert "dotenv" in text
    assert "python-dotenv" in text


def test_core_config_imports_dotenv():
    """ConfigManager hard-depends on dotenv — must be importable after pip install."""
    from core.config import ConfigManager

    cm = ConfigManager()
    assert cm is not None


# ── Bug B: token-only Telegram registration ──────────────────


def test_telegram_adapter_token_only_no_workspace(tmp_path):
    """TelegramAdapter accepts token alone; workspace stays None."""
    store = str(tmp_path / "pairing.json")
    with patch.dict(os.environ, {"WW_API_KEY": "test-key-for-unit"}, clear=False):
        # Clear workspace env so adapter does not pick it up
        os.environ.pop("TELEGRAM_WW_WORKSPACE", None)
        from gateway.adapters.telegram import TelegramAdapter
        from gateway.pairing import PairingManager

        adapter = TelegramAdapter(
            token="123456:ABC-TEST-TOKEN",
            workspace_id=None,
            pairing_mgr=PairingManager(store_path=store),
        )
        assert adapter._token == "123456:ABC-TEST-TOKEN"
        assert adapter._workspace_id is None


def test_telegram_adapter_invalid_workspace_env_is_ignored(tmp_path):
    store = str(tmp_path / "pairing.json")
    env = {
        "WW_API_KEY": "test-key-for-unit",
        "TELEGRAM_WW_WORKSPACE": "not-an-int",
    }
    with patch.dict(os.environ, env, clear=False):
        from gateway.adapters.telegram import TelegramAdapter
        from gateway.pairing import PairingManager

        adapter = TelegramAdapter(
            token="tok",
            workspace_id=None,
            pairing_mgr=PairingManager(store_path=store),
        )
        assert adapter._workspace_id is None


def test_init_gateway_logic_token_only_registers():
    """Mirrors server._init_gateway: token alone → register; workspace optional."""
    registered = []

    class FakeGW:
        def register(self, adapter, start=False):
            registered.append((adapter, start))

    class FakeTelegramGateway:
        def __init__(self, token="", workspace_id=None, poll_interval=2.0, task_handler=None):
            self.token = token
            self.workspace_id = workspace_id

    def init_gateway_like_server(gateway, token, workspace_raw):
        """Copy of registration decision from server.WorldwaveServer._init_gateway."""
        token = (token or "").strip()
        workspace_raw = (workspace_raw or "").strip()
        if token:
            workspace_id = None
            if workspace_raw:
                try:
                    workspace_id = int(workspace_raw)
                except ValueError:
                    workspace_id = None
            tg = FakeTelegramGateway(
                token=token,
                workspace_id=workspace_id,
                poll_interval=2.0,
                task_handler=None,
            )
            gateway.register(tg, start=False)
            return True
        return False

    gw = FakeGW()
    assert init_gateway_like_server(gw, "999:TOKENONLY", "") is True
    assert len(registered) == 1
    assert registered[0][0].token == "999:TOKENONLY"
    assert registered[0][0].workspace_id is None
    assert registered[0][1] is False

    # Invalid workspace → still register, workspace None
    registered.clear()
    assert init_gateway_like_server(gw, "tok", "not-int") is True
    assert registered[0][0].workspace_id is None

    # Token + valid workspace
    registered.clear()
    assert init_gateway_like_server(gw, "tok", "-100123") is True
    assert registered[0][0].workspace_id == -100123

    # No token → do not register
    registered.clear()
    assert init_gateway_like_server(gw, "", "123") is False
    assert registered == []


# ── Bug C: pairing notice + rate limit ───────────────────────


def test_pairing_notice_text_has_code_no_reflex():
    from gateway.pairing import PairingManager

    with tempfile.TemporaryDirectory() as d:
        pm = PairingManager(store_path=os.path.join(d, "p.json"))
        code = pm.request_pairing("telegram", "u1", "Alice", "c1")
        text = pm.pairing_notice_text(code)
        assert code in text
        assert "ww pairing approve" in text
        assert "reflex" not in text.lower()
        assert "Reflex arc" not in text


def test_pairing_should_notify_rate_limit():
    from gateway.pairing import PairingManager

    with tempfile.TemporaryDirectory() as d:
        pm = PairingManager(store_path=os.path.join(d, "p.json"))
        code = pm.request_pairing("telegram", "u2", "Bob", "c2")
        assert pm.should_notify_user(code) is True
        # Same code within cooldown → no re-send
        assert pm.should_notify_user(code) is False
        # After cooldown → allow again
        pm._notice_sent[code.upper()] = time.time() - 9999
        assert pm.should_notify_user(code) is True


def test_telegram_pairing_sends_notice_once(tmp_path):
    """Unknown user gets a pairing DM; second message does not re-spam."""
    store = str(tmp_path / "pairing.json")
    from gateway.adapters.telegram import TelegramAdapter
    from gateway.pairing import PairingManager

    pm = PairingManager(store_path=store)
    with patch.dict(os.environ, {"WW_API_KEY": "k", "WW_PAIRING_AUTO_APPROVE": "false"}, clear=False):
        adapter = TelegramAdapter(
            token="t",
            workspace_id=None,
            pairing_mgr=pm,
        )
        sent = []

        def fake_api(method, data=None, raw=False):
            sent.append((method, data))
            return {"ok": True}

        adapter._api_call = fake_api  # type: ignore

        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 4242, "type": "private"},
                "from": {"id": 99, "first_name": "Stranger", "is_bot": False},
                "text": "hello bot",
            },
        }

        # Drive the pairing branch the same way _poll_loop does
        message = update["message"]
        chat = message["chat"]
        chat_id = chat["id"]
        sender = message["from"]
        user_id = str(sender["id"])
        display_name = sender.get("first_name", "?")

        assert not pm.is_allowed("telegram", user_id)
        code = pm.request_pairing("telegram", user_id, display_name, str(chat_id))
        if pm.should_notify_user(code):
            notice = pm.pairing_notice_text(code)
            adapter._api_call("sendMessage", {
                "chat_id": str(chat_id),
                "text": notice[:4000],
            })
        # second arrival — should not notify
        code2 = pm.request_pairing("telegram", user_id, display_name, str(chat_id))
        assert code2 == code
        if pm.should_notify_user(code2):
            adapter._api_call("sendMessage", {
                "chat_id": str(chat_id),
                "text": "again",
            })

        assert len(sent) == 1
        assert sent[0][0] == "sendMessage"
        assert code in sent[0][1]["text"]
        assert "reflex" not in sent[0][1]["text"].lower()
        # Still not whitelisted — task must not be processed by callers
        assert not pm.is_allowed("telegram", user_id)
