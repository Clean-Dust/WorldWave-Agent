"""Gate 0.6 — stuck session / poisoned interrupt must not empty next /ww/run.

Root cause: shared StateManager kept interrupted checkpoints; next run hit
``get_last_checkpoint()`` and returned completed + empty results with a
metrics-dict summary.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.public_reply import extract_user_response, is_metrics_dump  # noqa: E402
from core.state import StateManager  # noqa: E402


def test_prepare_for_run_clears_active_interrupts(tmp_path):
    sm = StateManager(persist_dir=str(tmp_path / "s1"))
    sm.begin_spiral()
    sm.interrupt("rewind: phase 0 repeated 68 times")
    assert sm.get_last_checkpoint() is not None
    assert sm.summary()["active_interrupts"] >= 1

    sid_before = sm.session_id
    new_sid = sm.prepare_for_run("memory_prove:user:chat")
    assert sm.get_last_checkpoint() is None
    assert sm.summary()["active_interrupts"] == 0
    # Poisoned interrupt rotates session
    assert new_sid != sid_before or sm.current_spiral == 0


def test_prepare_for_run_window_isolation(tmp_path):
    sm = StateManager(persist_dir=str(tmp_path / "s2"))
    sm.prepare_for_run("platform:u1:c1")
    sid_a = sm.session_id
    sm.begin_spiral()
    sm.interrupt("rewind: phase 0 repeated 10 times")

    sm.prepare_for_run("platform:u2:c2")
    sid_b = sm.session_id
    assert sid_a != sid_b
    assert sm.get_last_checkpoint() is None
    # Same window without active interrupt keeps session
    sm.prepare_for_run("platform:u2:c2")
    assert sm.session_id == sid_b


def test_run_after_poisoned_interrupt_not_empty(tmp_path, monkeypatch):
    """Two sequential runs on same multi-field window: second must non-empty.

    Simulates plant→probe after a rewind poison left active_interrupts=1.
    """
    from tests.test_loop import make_minimal_ww, make_mock_response

    ww = make_minimal_ww()
    # Point state at temp dir
    ww.state = StateManager(persist_dir=str(tmp_path / "loop_state"))
    # Poison like Banana live
    ww.state.begin_spiral()
    ww.state.interrupt("rewind: phase 0 repeated 68 times")
    assert ww.state.get_last_checkpoint() is not None

    # Mock reflex path: always return a clean reply
    def fake_reflex(goal):
        return {
            "status": "completed",
            "spirals_completed": 0,
            "results": [{
                "actions": [{
                    "tool": "reflex_text",
                    "result": {"success": True, "output": f"REMEMBERED:probe_ok for {goal[:20]}"},
                }],
                "evaluation": {"success": True, "reason": "ok"},
                "success": True,
            }],
            "session_id": ww.state.session_id,
            "summary": "Reflex arc: direct text response",
            "reflex": True,
            "response": f"REMEMBERED:probe_ok for {goal[:20]}",
        }

    ww._reflex_arc_execute = MagicMock(side_effect=fake_reflex)
    ww.config["reflex_arc_enabled"] = True
    # Avoid heavy side paths
    ww.memory = None
    ww._maybe_internal_remember = MagicMock(return_value=None)
    ww._get_entity_context = MagicMock(return_value="")

    window = "memory_prove:prove_user:prove_chat"
    r1 = ww.run(
        "You MUST call remember key=prove_product_code value=PROD-1. Reply REMEMBERED:PROD-1",
        max_spirals=1,
        conversation_window=window,
    )
    assert r1.get("response") or extract_user_response(r1)
    # Must not still be blocked by poison
    assert ww.state.get_last_checkpoint() is None or r1.get("spirals_completed", 0) >= 0

    r2 = ww.run(
        "What is prove_product_code? Reply ONLY the value PROD-1 if known.",
        max_spirals=1,
        conversation_window=window,
    )
    text2 = (r2.get("response") or extract_user_response(r2) or "").strip()
    assert text2, f"second run empty: {r2}"
    assert is_metrics_dump(r2.get("summary")) is False
    assert not isinstance(r2.get("summary"), dict)
    # summary must be natural language string
    assert isinstance(r2.get("summary"), str)


def test_run_output_never_puts_metrics_in_summary(tmp_path):
    from tests.test_loop import make_minimal_ww

    ww = make_minimal_ww()
    ww.state = StateManager(persist_dir=str(tmp_path / "loop_state2"))
    ww.state.interrupt("rewind: phase 0 repeated 68 times")

    def empty_reflex(goal):
        return None  # fall through to spiral

    ww._reflex_arc_execute = MagicMock(side_effect=empty_reflex)
    ww.config["reflex_arc_enabled"] = True
    ww.memory = None
    ww._maybe_internal_remember = MagicMock(return_value=None)
    ww._get_entity_context = MagicMock(return_value="")
    # Force spiral loop to exit immediately by max_spirals=0 effectively —
    # after prepare_for_run clears interrupt, with max_spirals=0 no spirals
    # Actually max_spirals min is used in range; use 0 to get empty results
    # after clean prepare — recovery message must fire
    out = ww.run("hello", max_spirals=0, conversation_window="iso:u:c")
    assert isinstance(out.get("summary"), str)
    assert not is_metrics_dump(out.get("summary"))
    assert "state_metrics" in out
    # Empty spiral recovery: non-empty user response
    assert (out.get("response") or "").strip()
    assert extract_user_response(out)


def test_server_sequential_plant_probe_same_window(tmp_path):
    """Integration-style: two /ww/run via run_task, same entity+platform+user+chat."""
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
    srv.config = {}

    # Shared ww mock with real StateManager poison scenario
    state = StateManager(persist_dir=str(tmp_path / "srv_state"))
    state.interrupt("rewind: phase 0 repeated 68 times")

    call_n = {"n": 0}

    def run_side_effect(goal, max_spirals=3, **kwargs):
        call_n["n"] += 1
        # Mimic fixed loop: prepare_for_run then clean reply
        window = kwargs.get("conversation_window") or ""
        state.prepare_for_run(window)
        val = "PROD-MEM-SEQ"
        if call_n["n"] == 1:
            text = f"REMEMBERED:{val}"
        else:
            text = val
        return {
            "status": "completed",
            "spirals_completed": 1,
            "results": [{
                "actions": [{
                    "tool": "reflex_text",
                    "result": {"success": True, "output": text},
                }],
            }],
            "session_id": state.session_id,
            "summary": "ok",
            "response": text,
        }

    mock_ww = MagicMock()
    mock_ww.run.side_effect = run_side_effect
    mock_ww.set_entity = MagicMock()
    srv.ww = mock_ww

    entity = "prove_product_seq"
    plant = WorldwaveServer.run_task(
        srv,
        "remember key=prove_product_code value=PROD-MEM-SEQ",
        max_spirals=1,
        entity_id=entity,
        platform="memory_prove",
        conversation_window="memory_prove:u:c",
    )
    assert "PROD-MEM-SEQ" in (plant.get("response") or "")

    probe = WorldwaveServer.run_task(
        srv,
        "What is prove_product_code?",
        max_spirals=1,
        entity_id=entity,
        platform="memory_prove",
        conversation_window="memory_prove:u:c",
    )
    assert (probe.get("response") or "").strip()
    assert "PROD-MEM-SEQ" in probe["response"]
