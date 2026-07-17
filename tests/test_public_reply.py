"""Gate 0 product honesty — public_reply never promotes memory dumps.

Covers:
- is_internal_response_text rejects Reflex arc / direct response / traceback
- is_dump_like_text rejects multi-line key:value and spiral JSON
- extract_user_response: only _REPLY_TOOLS; recall_mine alone → ""
- Synthetic reflex_text real answer → answer
- Top-level dump-like response cleaned to empty
- Empty respond + raw status → not JSON dump
- run_task wrapper attaches clean top-level ``response`` (mocked ww.run)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.public_reply import (
    collapse_multi_greeting,
    extract_user_response,
    is_dump_like_text,
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
    assert is_internal_response_text(
        "I don't have your blood type or passport in memory."
    ) is False


def test_internal_empty_is_internal():
    assert is_internal_response_text("") is True
    assert is_internal_response_text("   ") is True
    assert is_internal_response_text(None) is True
    assert is_internal_response_text(42) is True


# ── dump-like detection ──────────────────────────────────────────


def test_dump_like_multiline_kv():
    dump = "home_city: ZetaCity\npet_name: ZetaPet"
    assert is_dump_like_text(dump) is True
    assert is_internal_response_text(dump) is True


def test_dump_like_single_snake_key():
    assert is_dump_like_text("home_city: ZetaCity") is True
    assert is_dump_like_text("marker: TIMELINE_E4_XYZ") is True


def test_dump_like_spiral_json():
    body = (
        '{"status": "completed", "spirals_completed": 0, '
        '"results": [{"actions": []}], "summary": "x"}'
    )
    assert is_dump_like_text(body) is True
    assert is_internal_response_text(body) is True


def test_dump_like_rejects_natural_prose():
    assert is_dump_like_text("Your home city is ZetaCity and pet is ZetaPet.") is False
    assert is_dump_like_text(
        "I don't have your blood type or passport in memory."
    ) is False


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


def test_extract_empty_top_level_still_uses_spiral_reply():
    """Gate 0.3 regression: empty response key must not hide usable spiral text."""
    result = {
        "response": "",
        "status": "completed",
        "results": [{
            "actions": [
                {
                    "tool": "recall_mine",
                    "result": {"success": True, "output": "home_city: X"},
                },
                {
                    "tool": "reflex_text",
                    "result": {"success": True, "output": "You live in X."},
                },
            ],
        }],
    }
    assert extract_user_response(result) == "You live in X."


def test_extract_prefers_later_synthesis_over_earlier_stub():
    result = {
        "results": [{
            "actions": [
                {
                    "tool": "reflex_text",
                    "result": {"success": True, "output": "stub"},
                },
                {
                    "tool": "respond",
                    "result": {"success": True, "output": "final synthesis reply"},
                },
            ],
        }],
    }
    assert extract_user_response(result) == "final synthesis reply"


def test_extract_only_recall_mine_success_returns_empty():
    """Gate 0: recall_mine success alone must never become the chat reply."""
    result = {
        "summary": "Reflex arc: 1 tool calls, success",
        "results": [{
            "actions": [{
                "tool": "recall_mine",
                "result": {
                    "success": True,
                    "facts": {"home_city": "ZetaCity", "pet_name": "ZetaPet"},
                    "total": 2,
                    "output": "home_city: ZetaCity\npet_name: ZetaPet",
                },
            }],
            "evaluation": {
                "success": True,
                "reason": "Reflex arc: all actions succeeded",
            },
        }],
    }
    assert extract_user_response(result) == ""


def test_extract_recall_mine_facts_output_never_promoted():
    """Former priority-4 path: raw fact dump must not surface."""
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
    assert extract_user_response(result) == ""


def test_extract_non_reply_tool_output_not_promoted():
    """Priority 4 removed: uuid/shell alone is not user chat text."""
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
    assert extract_user_response(result) == ""


def test_extract_top_level_dump_like_cleaned_to_empty():
    result = {
        "response": "home_city: ZetaCity\npet_name: ZetaPet",
        "results": [],
    }
    assert extract_user_response(result) == ""


def test_extract_empty_respond_plus_status_not_json_dump():
    """Empty respond output + raw status body must not become JSON reply."""
    result = {
        "status": "completed",
        "spirals_completed": 1,
        "summary": "Direct response generated",
        "results": [{
            "actions": [{
                "tool": "respond",
                "result": {"success": True, "output": ""},
            }],
            "evaluation": {
                "success": True,
                "reason": "Direct response generated",
                "response": "",
            },
        }],
    }
    assert extract_user_response(result) == ""
    # Even if someone stuffed the whole result into response
    result2 = dict(result)
    result2["response"] = (
        '{"status": "completed", "spirals_completed": 1, "results": []}'
    )
    assert extract_user_response(result2) == ""


def test_extract_respond_with_real_answer():
    result = {
        "results": [{
            "actions": [{
                "tool": "respond",
                "result": {
                    "success": True,
                    "output": "I don't have your blood type in memory.",
                },
            }],
        }],
    }
    assert "blood type" in extract_user_response(result)


def test_extract_skips_failed_actions():
    result = {
        "results": [{
            "actions": [{
                "tool": "respond",
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


def test_public_reply_strips_memory_dump():
    dump = "home_city: ZetaCity\npet_name: ZetaPet"
    assert public_reply(dump, fallback="Done.") == "Done."
    assert public_reply(dump, fallback="") == ""


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
                "result": {
                    "success": True,
                    "facts": {"home_city": "ZetaCity"},
                    "total": 1,
                    "output": "home_city: ZetaCity",
                },
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

    out = WorldwaveServer.run_task(srv, "what is my blood type?", max_spirals=1)
    assert out["response"] == ""


def test_cli_reexports_shared_extractor():
    """ww_cli must not re-implement; import from core.public_reply."""
    from ww_cli import extract_user_response as cli_extract
    from core.public_reply import extract_user_response as core_extract

    assert cli_extract is core_extract


# ── recall_mine human-readable output (internal tool still has output) ──


def test_recall_mine_includes_output(tmp_path, monkeypatch):
    from core.config import ConfigManager
    from core.entity_state import EntityStateManager
    from core.memory.tools import MemoryTools

    cfg = MagicMock()
    cfg.get = MagicMock(return_value="")
    monkeypatch.setenv("WW_CONFIG", str(tmp_path / "cfg"))
    esm = EntityStateManager(config=cfg, data_dir=str(tmp_path / "entities"))
    esm.set_working_memory("ent_test", "marker", "TIMELINE_E4_XYZ")

    tools = MemoryTools(memory_system=None, entity_state_mgr=esm, entity_id="ent_test")
    out = tools.recall_mine(query="marker")
    assert out.get("success") is True
    assert "TIMELINE_E4_XYZ" in (out.get("output") or "")
    assert out["facts"].get("marker") == "TIMELINE_E4_XYZ"


def test_reflex_fallthrough_when_no_user_visible():
    """Tool-only reflex with empty synthesis falls through (returns None)."""
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
    # Synthesis also empty (clear side_effect so return_value wins)
    ww.llm.chat.side_effect = None
    ww.llm.chat.return_value = ""

    result = ww._reflex_arc_execute("what is missing?")
    assert result is None


def test_reflex_synthesizes_after_recall_mine():
    """After recall_mine, reflex must append reflex_text synthesis (not dump)."""
    from tests.test_loop import make_minimal_ww, make_mock_response

    ww = make_minimal_ww()
    ww.llm._call.side_effect = None
    ww.llm._call.return_value = make_mock_response(
        tool_calls=[{
            "function": {
                "name": "recall_mine",
                "arguments": "{}",
            }
        }]
    )
    ww.tools = MagicMock()
    ww.tools.call.return_value = {
        "success": True,
        "facts": {"home_city": "ZetaCity", "pet_name": "ZetaPet"},
        "total": 2,
        "output": "home_city: ZetaCity\npet_name: ZetaPet",
    }
    ww.tools.to_openai_tools.return_value = []
    ww.tools.prompt_block.return_value = ""
    ww.tools.tool_names.return_value = ["recall_mine"]
    ww.llm.chat.side_effect = None
    ww.llm.chat.return_value = (
        "I don't have your blood type or passport in memory."
    )

    result = ww._reflex_arc_execute(
        "What is my blood type and passport number?"
    )
    assert result is not None
    actions = result["results"][0]["actions"]
    tools = [a.get("tool") for a in actions]
    assert "recall_mine" in tools
    assert "reflex_text" in tools
    from core.public_reply import extract_user_response
    reply = extract_user_response(result)
    assert "blood type" in reply.lower() or "don't have" in reply.lower() or "do not" in reply.lower()
    assert "home_city:" not in reply
    assert "pet_name:" not in reply
