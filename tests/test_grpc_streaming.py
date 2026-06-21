"""Tests for gRPC server-side streaming in AgentServiceImpl.RunTask."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from core.agent_grpc import AgentServiceImpl
from proto.wavegate.v1 import agent_pb2 as ag_pb2
from proto.wavegate.v1 import unified_message_pb2 as um_pb2


def _make_request(goal="test goal", session_key="sess:1", max_spirals=1):
    return ag_pb2.RunTaskRequest(
        session_key=session_key,
        goal=goal,
        max_spirals=max_spirals,
    )


def _spy_run_calls_phases(phases=None):
    if phases is None:
        phases = ["PERCEIVE", "RECALL", "PLAN", "ACT", "EVALUATE", "LEARN"]
    pcts = [15, 30, 45, 60, 80, 100]

    def run(goal, max_spirals=3, image_path="", reasoning_effort="",
            on_spiral_progress=None):
        if on_spiral_progress:
            for i, phase in enumerate(phases):
                on_spiral_progress(phase, f"Phase {phase}", pcts[i])
        return {"status": "completed", "spirals_completed": 1, "results": []}
    return run


class TestRunTaskStreaming:
    """Test streaming behavior of AgentServiceImpl.RunTask."""

    def test_yields_progress_chunks_for_each_phase(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        svc._ww.run = _spy_run_calls_phases()
        responses = list(svc.RunTask(_make_request(), None))
        progress_chunks = responses[:-1]
        final = responses[-1]
        assert len(progress_chunks) == 6
        assert final.is_final is True

    def test_stream_seq_increments(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        svc._ww.run = _spy_run_calls_phases()
        responses = list(svc.RunTask(_make_request(), None))
        for i, resp in enumerate(responses[:-1]):
            assert resp.stream_seq == i
            assert resp.is_final is False

    def test_final_has_complete_result(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        svc._ww.run = _spy_run_calls_phases()
        responses = list(svc.RunTask(_make_request(), None))
        final = responses[-1]
        assert final.is_final is True
        assert len(final.payload.text) > 0

    def test_progress_chunk_has_correct_structure(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        svc._ww.run = _spy_run_calls_phases()
        responses = list(svc.RunTask(_make_request(), None))
        first = responses[0]
        assert first.payload.HasField("stream_chunk")
        chunk = first.payload.stream_chunk
        assert "PERCEIVE" in chunk.delta
        assert chunk.stream_type == "thinking"
        assert chunk.seq == 0

    def test_error_handling_yields_error_response(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        def failing_run(**kwargs):
            raise RuntimeError("simulated crash")
        svc._ww.run = failing_run
        responses = list(svc.RunTask(_make_request(), None))
        assert len(responses) == 1
        assert responses[0].is_final is True
        assert responses[0].payload.HasField("error")
        assert "simulated crash" in responses[0].payload.error.message

    def test_correlation_id_present(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        svc._ww.run = _spy_run_calls_phases()
        responses = list(svc.RunTask(_make_request(), None))
        for resp in responses:
            assert resp.correlation_id
            assert len(resp.correlation_id) > 0

    def test_session_key_echoed(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        svc._ww.run = _spy_run_calls_phases()
        responses = list(svc.RunTask(
            _make_request(session_key="my-session"), None))
        for resp in responses:
            assert resp.session_key == "my-session"

    def test_zero_phases_still_yields_final(self):
        svc = AgentServiceImpl()
        svc._ww = MagicMock()
        svc._ww.run = lambda goal, max_spirals=3, image_path="", reasoning_effort="", on_spiral_progress=None: {"status": "done"}
        responses = list(svc.RunTask(_make_request(), None))
        assert len(responses) == 1
        assert responses[0].is_final is True
        assert "done" in responses[0].payload.text


class TestProgressCallback:
    """Test on_spiral_progress integration in core/loop.py Worldwave.run()."""

    def test_callback_fires_at_phases(self):
        from core.loop import Worldwave
        ww = Worldwave()
        ww._llm_perceive = lambda g, pf=None: {"observations": ["ok"], "key_signals": [], "environment_summary": "test", "uncertainties": []}
        ww._llm_recall = lambda p, g: {"query": g, "entities": [], "memories": [], "llm_insight": ""}
        ww._llm_plan = lambda p, r, g: {"strategy": "test", "steps": []}
        ww._llm_act = lambda p, g: []
        ww._llm_evaluate = lambda p, a, g: {"success": True, "reason": "test", "goal_remaining": False}
        ww._llm_learn = lambda s, g: {"stored": False}
        ww._goal_achieved = lambda s: True
        calls = []
        result = ww.run("test", max_spirals=1,
                        on_spiral_progress=lambda p, m, pct: calls.append((p, pct)))
        assert len(calls) >= 1
        phases_seen = [c[0] for c in calls]
        expected = ["PERCEIVE", "RECALL", "PLAN", "ACT", "EVALUATE", "LEARN"]
        for p in phases_seen:
            assert p in expected
        for _, pct in calls:
            assert 0 <= pct <= 100

    def test_no_callback_does_not_crash(self):
        from core.loop import Worldwave
        ww = Worldwave()
        ww._llm_perceive = lambda g, pf=None: {"observations": ["ok"], "key_signals": [], "environment_summary": "test", "uncertainties": []}
        ww._llm_recall = lambda p, g: {"query": g, "entities": [], "memories": [], "llm_insight": ""}
        ww._llm_plan = lambda p, r, g: {"strategy": "test", "steps": []}
        ww._llm_act = lambda p, g: []
        ww._llm_evaluate = lambda p, a, g: {"success": True, "reason": "test", "goal_remaining": False}
        ww._llm_learn = lambda s, g: {"stored": False}
        ww._goal_achieved = lambda s: True
        result = ww.run("test", max_spirals=1)
        assert result["status"] == "completed"
