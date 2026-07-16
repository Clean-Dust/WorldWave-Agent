"""Tests: chat-core slash commands (parse, /new core WM, /true force flag, telegram list)."""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── parse_chat_core_command ───────────────────────────────────────────


@pytest.mark.parametrize(
    "line,name,args",
    [
        ("/new", "new", ""),
        ("new", "new", ""),
        ("/true", "true", ""),
        ("true", "true", ""),
        ("/memory edit", "memory", "edit"),
        ("memory edit", "memory", "edit"),
        ("/memory set abc hello world", "memory", "set abc hello world"),
        ("/memory del xyz", "memory", "del xyz"),
        ("/gateway restart", "gateway", "restart"),
        ("gateway restart", "gateway", "restart"),
        ("/gateway restart telegram", "gateway", "restart telegram"),
        ("/model flash", "model", "flash"),
        ("model flash", "model", "flash"),
        ("/model", "model", ""),
        ("/status", "status", ""),
        ("/stop", "stop", ""),
        ("/clear", "clear", ""),
        ("/help", "help", ""),
        ("／new", "new", ""),  # fullwidth solidus
    ],
)
def test_parse_chat_core_commands(line, name, args):
    from core.chat_commands import parse_chat_core_command

    p = parse_chat_core_command(line)
    assert p is not None, f"expected parse for {line!r}"
    assert p.name == name
    assert p.args == args


@pytest.mark.parametrize(
    "line",
    [
        "",
        "hello world",
        "please /new something",
        "gateway setup",  # other gateway → None (existing parser)
        "gateway list",
        "/update",
        "update status",
        "status please check the box",  # not bare meta
    ],
)
def test_parse_chat_core_rejects_non_core(line):
    from core.chat_commands import parse_chat_core_command

    assert parse_chat_core_command(line) is None


def test_help_lists_frozen_set():
    from core.chat_commands import format_help_text

    text = format_help_text("repl")
    for cmd in (
        "/help",
        "/new",
        "/model",
        "/memory",
        "/true",
        "/stop",
        "/status",
        "/gateway restart",
        "/exit",
        "/clear",
    ):
        assert cmd in text, f"missing {cmd}"


# ── /new does not remove core WM ──────────────────────────────────────


def test_clear_session_wm_keeps_core():
    from core.entity_state import EntityStateManager

    with tempfile.TemporaryDirectory() as td:
        cfg = MagicMock()
        cfg.get = MagicMock(return_value=None)
        cfg.expand_path = MagicMock(side_effect=lambda p: os.path.expanduser(p))
        esm = EntityStateManager(config=cfg, data_dir=td)
        esm.working_memory_capacity = 32
        eid = "ent_test_new"
        esm.set_working_memory(eid, "temp_a", "a", kind="outcome")
        esm.set_working_memory(eid, "temp_b", "b", kind="outcome")
        esm.set_working_memory(eid, "core_pref", "keep me", is_core=True)

        promoted = []

        def on_evict(entity_id, key, value, meta=None):
            promoted.append(key)

        esm.set_on_wm_evict(on_evict)
        # Make temp_a promote-worthy (access >= WM_PROMOTE_MIN_ACCESS default 2)
        st = esm.get(eid)
        st.working_memory_meta["temp_a"]["access_count"] = 5
        esm.save(st)

        counts = esm.clear_session_working_memory(eid)
        st2 = esm.get(eid)
        assert "core_pref" in st2.working_memory
        assert "temp_a" not in st2.working_memory
        assert "temp_b" not in st2.working_memory
        assert counts["kept_core"] >= 1
        assert counts["wm_cleared"] >= 2
        assert "temp_a" in promoted


# ── force_next_tool_once ──────────────────────────────────────────────


def test_force_next_tool_allows_once():
    from core.loop import Worldwave

    ww = Worldwave.__new__(Worldwave)
    ww.force_next_tool_once = False
    ww._last_blocked = None
    ww.verbose = False
    ww._log = lambda *a, **k: None

    # Minimal basal ganglia mock that always blocks
    bg = MagicMock()
    bg.classify_action.return_value = "unsafe"
    bg.evaluate_action.return_value = {
        "allow": False,
        "g_score": 0.1,
        "n_score": 0.99,
        "reason": "BLOCKED: N-score high",
    }
    ww.basal_ganglia = bg
    ww.cascade = MagicMock()
    ww.cascade.current_stress_level.return_value = 0.0
    ww.state = MagicMock()
    ww.state.current_spiral = 1

    # First eval: blocked + stashed
    r1 = Worldwave._evaluate_action_safety(ww, "shell", {"cmd": "rm -rf /"})
    assert r1.get("allow") is False
    assert ww._last_blocked is not None
    assert ww._last_blocked.get("tool") == "shell"

    # Set /true flag
    ww.force_next_tool_once = True
    r2 = Worldwave._evaluate_action_safety(ww, "shell", {"cmd": "ls"})
    assert r2.get("allow") is True
    assert r2.get("forced") is True
    assert ww.force_next_tool_once is False  # consumed

    # Third call blocks again
    r3 = Worldwave._evaluate_action_safety(ww, "shell", {"cmd": "x"})
    assert r3.get("allow") is False


def test_handle_true_message():
    from core.chat_commands import (
        ChatCommandContext,
        ParsedChatCommand,
        handle_chat_core,
    )

    calls = {}

    def api_post(ep, data):
        calls["ep"] = ep
        return {
            "status": "ok",
            "force_next_tool_once": True,
            "last_blocked": {"tool": "shell", "n_score": 0.9, "reason": "blocked"},
        }

    ctx = ChatCommandContext(
        api_get=lambda e: {},
        api_post=api_post,
        platform="repl",
        is_owner=True,
    )
    msg = handle_chat_core(ParsedChatCommand("true", "", "/true"), ctx)
    assert "once" in msg.lower()
    assert "shell" in msg
    assert "safety" in msg.lower()
    assert calls["ep"] == "/ww/chat/true"


def test_handle_gateway_restart_owner_only_telegram():
    from core.chat_commands import (
        ChatCommandContext,
        ParsedChatCommand,
        handle_chat_core,
    )

    posts = []

    def api_post(ep, data):
        posts.append((ep, data))
        return {"ok": True}

    ctx = ChatCommandContext(
        api_get=lambda e: {},
        api_post=api_post,
        platform="telegram",
        is_owner=False,
    )
    msg = handle_chat_core(
        ParsedChatCommand("gateway", "restart", "/gateway restart"), ctx
    )
    assert "owner" in msg.lower()
    assert posts == []

    ctx.is_owner = True
    msg2 = handle_chat_core(
        ParsedChatCommand("gateway", "restart", "/gateway restart"), ctx
    )
    assert "restarted" in msg2.lower()
    assert any("/ww/gateway/start" in p[0] for p in posts)


# ── Telegram command list ─────────────────────────────────────────────


def test_telegram_direct_commands_include_frozen():
    from gateway.adapters.telegram import TELEGRAM_DIRECT_COMMANDS

    for name in ("help", "new", "clear", "model", "memory", "true", "stop", "status", "gateway"):
        assert name in TELEGRAM_DIRECT_COMMANDS


def test_telegram_register_includes_true_and_gateway(monkeypatch):
    import urllib.request as ur
    from gateway.adapters.telegram import TelegramAdapter

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=10):
        import json as _json
        body = req.data
        captured["payload"] = _json.loads(body.decode())
        return FakeResp()

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)

    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._token = "123:ABC"
    adapter._register_commands()
    names = [c["command"] for c in captured.get("payload", {}).get("commands", [])]
    assert "true" in names
    assert "gateway" in names
    assert "new" in names
    assert "memory" in names


def test_telegram_handle_true_uses_shared(monkeypatch):
    from gateway.adapters.telegram import TelegramAdapter

    sent = []

    class _T(TelegramAdapter):
        def __init__(self):
            pass

        def send_message(self, chat_id, text, **kwargs):
            sent.append(text)
            return True

        def _telegram_api_get(self, endpoint):
            return {}

        def _telegram_api_post(self, endpoint, data):
            if endpoint == "/ww/chat/true":
                return {"status": "ok", "last_blocked": None}
            return {}

        def _is_telegram_owner(self, user_id):
            return True

    adapter = _T()
    ok = adapter._handle_direct_command("1", "true", "", {"from": {"id": 99}})
    assert ok is True
    assert sent
    assert "once" in sent[0].lower() or "safety" in sent[0].lower()
