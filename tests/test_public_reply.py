"""Same Timeline M1 E2 — zero user-facing leaks via shared public_reply.

Covers:
- is_internal_response_text rejects Reflex arc / direct response / traceback
- extract_user_response priority + never returns internal leaks
- Synthetic reflex-only summary → empty (OK policy)
- Synthetic reflex_text "pong" → "pong"
- run_task wrapper attaches clean top-level ``response`` (mocked ww.run)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.public_reply import (
    collapse_multi_greeting,
    extract_user_response,
    is_internal_response_text,
    public_reply,
)


# ── is_internal_response_text ────────────────────────────────────


def test_internal_rejects_reflex_arc():
    assert is_internal_response_text("Reflex arc: direct text response") is True
    assert is_internal_response_text("Reflex arc: 2 tool calls, success") is True
    assert is_internal_response_text("status=ok Reflex arc complete") is True


def test_internal_rejects_direct_response():
    assert is_internal_response_text("Reflex arc direct response") is True
    assert is_internal_response_text("this is a Direct Response path") is True


def test_internal_rejects_error_and_traceback():
    assert is_internal_response_text("error: something broke") is True
    assert is_internal_response_text("Traceback (most recent call last):\n  File") is True


def test_internal_accepts_normal_text():
    assert is_internal_response_text("Hello, how can I help?") is False
    assert is_internal_response_text("pong") is False
    assert is_internal_response_text("Your marker is SECRET_XYZ") is False


def test_internal_empty_is_internal():
    assert is_internal_response_text("") is True
    assert is_internal_response_text("   ") is True
    assert is_internal_response_text(None) is True
    assert is_internal_response_text(42) is True


# ── extract_user_response ────────────────────────────────────────


def test_extract_prefers_top_level_response():
    result = {
        "response": "clean reply",
        "summary": "Reflex arc: direct text response",
        "results": [{
            "actions": [{"tool": "reflex_text", "result": {"output": "other"}}],
            "evaluation": {"reason": "Reflex arc direct response"},
        }],
    }
    assert extract_user_response(result) == "clean reply"


def test_extract_rejects_internal_top_level_and_falls_through():
    result = {
        "response": "Reflex arc: direct text response",
        "results": [{
            "actions": [{
                "tool": "reflex_text",
                "result": {"success": True, "output": "pong"},
            }],
            "evaluation": {"success": True, "reason": "Reflex arc direct response"},
        }],
        "summary": "Reflex arc: direct text response",
    }
    assert extract_user_response(result) == "pong"


def test_extract_synthetic_reflex_summary_only_returns_empty():
    """Policy: empty is OK when there is no real user content."""
    result = {
        "status": "completed",
        "summary": "Reflex arc: 1 tool calls, success",
        "results": [{
            "spiral": 0,
            "actions": [{
                "tool": "recall_mine",
                "result": {"success": True, "facts": {}, "total": 0},
            }],
            "evaluation": {
                "success": True,
                "reason": "Reflex arc: all actions succeeded",
            },
            "success": True,
        }],
        "reflex": True,
    }
    assert extract_user_response(result) == ""


def test_extract_reflex_text_pong():
    result = {
        "status": "completed",
        "summary": "Reflex arc: direct text response",
        "results": [{
            "actions": [{
                "tool": "reflex_text",
                "result": {"success": True, "output": "pong"},
            }],
            "evaluation": {"success": True, "reason": "Reflex arc direct response"},
            "success": True,
        }],
        "reflex": True,
    }
    assert extract_user_response(result) == "pong"


def test_extract_any_action_output():
    result = {
        "summary": "Reflex arc: 1 tool calls, success",
        "results": [{
            "actions": [{
                "tool": "uuid",
                "result": {
                    "success": True,
                    "output": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                },
            }],
            "evaluation": {
                "success": True,
                "reason": "Reflex arc: all actions succeeded",
            },
        }],
    }
    assert "a1b2c3d4" in extract_user_response(result)


def test_extract_recall_mine_facts_output():
    result = {
        "summary": "Reflex arc: 1 tool calls, success",
        "results": [{
            "actions": [{
                "tool": "recall_mine",
                "result": {
                    "success": True,
                    "facts": {"marker": "TIMELINE_E4_XYZ"},
                    "total": 1,
                    "output": "marker: TIMELINE_E4_XYZ",
                },
            }],
            "evaluation": {
                "success": True,
                "reason": "Reflex arc: all actions succeeded",
            },
        }],
    }
    got = extract_user_response(result)
    assert "TIMELINE_E4_XYZ" in got
    assert "Reflex arc" not in got


def test_extract_skips_failed_actions():
    result = {
        "results": [{
            "actions": [{
                "tool": "shell",
                "result": {
                    "success": False,
                    "output": "should not surface",
                    "error": "boom",
                },
            }],
        }],
    }
    assert extract_user_response(result) == ""


def test_extract_non_dict_returns_empty():
    assert extract_user_response(None) == ""
    assert extract_user_response("nope") == ""
    assert extract_user_response([]) == ""


# ── collapse_multi_greeting ──────────────────────────────────────


def test_collapse_multi_greeting_keeps_first_short_paragraphs():
    text = "你好！有什么需要帮忙的吗？😊\n\n嗨！好久不见，近来可好？"
    assert collapse_multi_greeting(text) == "你好！有什么需要帮忙的吗？😊"


def test_collapse_multi_greeting_single_paragraph_unchanged():
    text = "你好！有什么需要帮忙的吗？😊"
    assert collapse_multi_greeting(text) == text


def test_collapse_multi_greeting_keeps_long_paragraphs():
    """If any paragraph is ≥100 chars, do not collapse (not pure greets)."""
    long_a = "A" * 100
    long_b = "B" * 100
    text = f"{long_a}\n\n{long_b}"
    assert collapse_multi_greeting(text) == text


def test_collapse_multi_greeting_mixed_length_not_collapsed():
    short = "Hi!"
    long = "x" * 120
    text = f"{short}\n\n{long}"
    assert collapse_multi_greeting(text) == text


def test_collapse_via_extract_user_response():
    result = {
        "response": "你好！\n\n嗨！好久不见。",
    }
    assert extract_user_response(result) == "你好！"


def test_collapse_via_public_reply():
    assert public_reply("嗨\n\n你好呀") == "嗨"


# ── public_reply ─────────────────────────────────────────────────


def test_public_reply_strips_internal():
    assert public_reply("Reflex arc: x", fallback="Done.") == "Done." or \
           public_reply("Reflex arc: x", fallback="Done.") == ""
    # With empty fallback, internal → empty
    assert public_reply("Reflex arc: x", fallback="") == ""
    assert public_reply("hello world", fallback="Done.") == "hello world"


# ── run_task attaches response ───────────────────────────────────


def test_run_task_attaches_clean_response_field():
    """Server wrapper always sets result['response'] from shared extractor."""
    from server import WorldwaveServer

    # Minimal stub: avoid full Worldwave init
    srv = object.__new__(WorldwaveServer)
    srv._run_lock = __import__("threading").RLock()
    srv._lock_waits = 0
    srv._lock_runs = 0
    srv._session_locks = {}
    srv._session_locks_guard = __import__("threading").Lock()
    srv.identity_resolver = None
    srv.entity_mgr = None
    srv._task_history = []
    srv._last_result = None

    raw = {
        "status": "completed",
        "summary": "Reflex arc: direct text response",
        "results": [{
            "actions": [{
                "tool": "reflex_text",
                "result": {"success": True, "output": "pong"},
            }],
            "evaluation": {
                "success": True,
                "reason": "Reflex arc direct response",
            },
            "success": True,
        }],
        "reflex": True,
    }

    mock_ww = MagicMock()
    mock_ww.run.return_value = dict(raw)
    mock_ww.set_entity = MagicMock()
    srv.ww = mock_ww
    srv.config = {}

    out = WorldwaveServer.run_task(srv, "say pong", max_spirals=1, platform="http")
    assert isinstance(out, dict)
    assert "response" in out
    assert out["response"] == "pong"
    assert "Reflex arc" not in out["response"]
    # Debug summary may still leak internally — OK if not user chat
    assert "Reflex arc" in out.get("summary", "")


def test_run_task_response_empty_when_only_internal_summary():
    from server import WorldwaveServer

    srv = object.__new__(WorldwaveServer)
    srv._run_lock = __import__("threading").RLock()
    srv._lock_waits = 0
    srv._lock_runs = 0
    srv._session_locks = {}
    srv._session_locks_guard = __import__("threading").Lock()
    srv.identity_resolver = None
    srv.entity_mgr = None
    srv._task_history = []
    srv._last_result = None

    raw = {
        "status": "completed",
        "summary": "Reflex arc: 1 tool calls, success",
        "results": [{
            "actions": [{
                "tool": "recall_mine",
                "result": {"success": True, "facts": {}, "total": 0},
            }],
            "evaluation": {
                "success": True,
                "reason": "Reflex arc: all actions succeeded",
            },
            "success": True,
        }],
        "reflex": True,
    }
    mock_ww = MagicMock()
    mock_ww.run.return_value = dict(raw)
    mock_ww.set_entity = MagicMock()
    srv.ww = mock_ww
    srv.config = {}

    out = WorldwaveServer.run_task(srv, "what do you know?", max_spirals=1)
    assert out["response"] == ""


def test_cli_reexports_shared_extractor():
    """ww_cli must not re-implement; import from core.public_reply."""
    from ww_cli import extract_user_response as cli_extract
    from core.public_reply import extract_user_response as core_extract

    assert cli_extract is core_extract


# ── recall_mine human-readable output (E4 support) ───────────────


def test_recall_mine_includes_output(tmp_path, monkeypatch):
    from core.config import ConfigManager
    from core.entity_state import EntityStateManager
    from core.memory.tools import MemoryTools

    cfg = MagicMock()
    cfg.get = MagicMock(return_value="")
    # EntityStateManager needs a config with data dir
    monkeypatch.setenv("WW_CONFIG", str(tmp_path / "cfg"))
    esm = EntityStateManager(config=cfg, data_dir=str(tmp_path / "entities"))
    esm.set_working_memory("ent_test", "marker", "TIMELINE_E4_XYZ")

    tools = MemoryTools(memory_system=None, entity_state_mgr=esm, entity_id="ent_test")
    out = tools.recall_mine(query="marker")
    assert out.get("success") is True
    assert "TIMELINE_E4_XYZ" in (out.get("output") or "")
    assert out["facts"].get("marker") == "TIMELINE_E4_XYZ"


def test_reflex_fallthrough_when_no_user_visible():
    """Tool-only reflex with empty outputs returns None (fall through to spiral)."""
    from tests.test_loop import make_minimal_ww, make_mock_response

    ww = make_minimal_ww()
    ww.llm._call.side_effect = None
    ww.llm._call.return_value = make_mock_response(
        tool_calls=[{
            "function": {
                "name": "recall_mine",
                "arguments": '{"query": "missing"}',
            }
        }]
    )
    # Ensure tools.call returns empty-output recall
    ww.tools = MagicMock()
    ww.tools.call.return_value = {
        "success": True,
        "facts": {},
        "total": 0,
        "output": "",
    }
    ww.tools.to_openai_tools.return_value = []
    ww.tools.prompt_block.return_value = ""
    ww.tools.tool_names.return_value = ["recall_mine"]

    result = ww._reflex_arc_execute("what is missing?")
    assert result is None
