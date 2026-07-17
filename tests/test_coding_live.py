"""tests/test_coding_live.py — PM 0.10 live path, model route, metrics, loop bridge."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestModelRoute:
    def test_resolve_from_env(self):
        from coding.model_route import resolve_coding_model
        old_m = os.environ.get("WW_CODING_MODEL")
        old_p = os.environ.get("WW_CODING_PROVIDER")
        try:
            os.environ["WW_CODING_MODEL"] = "mock-coding-model"
            os.environ["WW_CODING_PROVIDER"] = "mock-provider"
            r = resolve_coding_model(prefer_coding=True)
            assert r["model"] == "mock-coding-model"
            assert r["provider"] == "mock-provider"
            assert r["coding_preferred"] is True
            assert r["fallback"] is False
            assert r["source"] == "WW_CODING_MODEL"
        finally:
            if old_m is None:
                os.environ.pop("WW_CODING_MODEL", None)
            else:
                os.environ["WW_CODING_MODEL"] = old_m
            if old_p is None:
                os.environ.pop("WW_CODING_PROVIDER", None)
            else:
                os.environ["WW_CODING_PROVIDER"] = old_p

    def test_fallback_when_unset(self):
        from coding.model_route import resolve_coding_model
        old = os.environ.get("WW_CODING_MODEL")
        try:
            os.environ.pop("WW_CODING_MODEL", None)
            r = resolve_coding_model(main_model="main-model-xyz", prefer_coding=True)
            assert r["model"] == "main-model-xyz"
            assert r["source"] == "main"
            assert r["fallback"] is True
        finally:
            if old is not None:
                os.environ["WW_CODING_MODEL"] = old

    def test_apply_to_mock_client(self):
        from coding.model_route import resolve_coding_model, apply_coding_model_to_client

        class MockClient:
            def __init__(self):
                self.model = "old"
                self._provider = "old-prov"
                self.last_model = "old"

            def switch_provider(self, p):
                self._provider = p

        old = os.environ.get("WW_CODING_MODEL")
        try:
            os.environ["WW_CODING_MODEL"] = "new-coding-model"
            c = MockClient()
            route = resolve_coding_model(prefer_coding=True)
            apply_coding_model_to_client(c, route)
            assert c.model == "new-coding-model"
        finally:
            if old is None:
                os.environ.pop("WW_CODING_MODEL", None)
            else:
                os.environ["WW_CODING_MODEL"] = old


class TestCodingMetrics:
    def test_to_dict_and_export(self, tmp_path):
        from coding.orchestrator import CodingMetrics
        m = CodingMetrics(rounds=3, tools=5, verifies=2, redirects=1, trips=0, autocompacts=1)
        d = m.to_dict()
        for k in ("rounds", "tools", "verifies", "redirects", "trips", "autocompacts"):
            assert k in d
        path = tmp_path / "m.json"
        text = m.export(str(path))
        assert path.is_file()
        loaded = json.loads(text)
        assert loaded["verifies"] == 2
        assert loaded["redirects"] == 1


class TestOrchestratorLimits:
    def test_max_tool_rounds_and_same_fp_env(self):
        from coding.orchestrator import get_max_tool_rounds, get_max_same_fp
        old_r = os.environ.get("WW_CODING_MAX_TOOL_ROUNDS")
        old_f = os.environ.get("WW_CODING_MAX_SAME_FP")
        try:
            os.environ["WW_CODING_MAX_TOOL_ROUNDS"] = "7"
            os.environ["WW_CODING_MAX_SAME_FP"] = "4"
            assert get_max_tool_rounds() == 7
            assert get_max_same_fp() == 4
        finally:
            if old_r is None:
                os.environ.pop("WW_CODING_MAX_TOOL_ROUNDS", None)
            else:
                os.environ["WW_CODING_MAX_TOOL_ROUNDS"] = old_r
            if old_f is None:
                os.environ.pop("WW_CODING_MAX_SAME_FP", None)
            else:
                os.environ["WW_CODING_MAX_SAME_FP"] = old_f

    def test_max_tool_rounds_handoff(self, tmp_path):
        from coding.orchestrator import coding_run_ticket, reset_ticket_state
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        f = pkg / "m.py"
        f.write_text("def leaf(x):\n    return x\n", encoding="utf-8")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_m.py").write_text(
            "from pkg.m import leaf\n\ndef test_leaf():\n    assert leaf(1) == 1\n",
            encoding="utf-8",
        )
        old_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(tmp_path) + (os.pathsep + old_pp if old_pp else "")
        cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            reset_ticket_state()
            r = coding_run_ticket(
                goal="verify leaf",
                project_root=str(tmp_path),
                symbol="leaf",
                file_path=str(f),
                test_path=str(tests),
                max_tool_rounds=1,  # force early handoff after first tool
            )
            assert r.get("handoff") is not None or r.get("status") in (
                "handoff", "completed", "replanned",
            )
            # With max_tool_rounds=1, first _bump_tool may pass and second fails
            if r.get("handoff"):
                assert r["handoff"].get("reason") in (
                    "max_tool_rounds",
                    "replan_recorded_awaiting_new_edit",
                    "max_replans_exhausted",
                    "same_fingerprint_threshold",
                    "circuit_tripped",
                )
            assert r.get("metrics") is not None
            assert "rounds" in r["metrics"]
        finally:
            os.chdir(cwd)
            if old_pp:
                os.environ["PYTHONPATH"] = old_pp
            else:
                os.environ.pop("PYTHONPATH", None)

    def test_explain_failure_into_replan(self, tmp_path):
        from coding.orchestrator import coding_run_ticket, reset_ticket_state
        from coding.aci import DefensiveEditor
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        f = pkg / "m.py"
        f.write_text("def leaf(x):\n    return x * 2\n", encoding="utf-8")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_m.py").write_text(
            "from pkg.m import leaf\n\ndef test_leaf():\n    assert leaf(2) == 4\n"
            "def test_fail():\n    assert leaf(1) == 999\n",
            encoding="utf-8",
        )
        old_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(tmp_path) + (os.pathsep + old_pp if old_pp else "")
        cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            reset_ticket_state()
            r = coding_run_ticket(
                goal="fix leaf",
                project_root=str(tmp_path),
                symbol="leaf",
                file_path=str(f),
                test_path=str(tests),
                max_replans=1,
            )
            # Should have verify fail path with explain and/or replan
            steps = r.get("steps") or {}
            # Either green (if only one test coincidence) or explain/replan present
            if not r.get("success"):
                assert (
                    "explain_failure" in steps
                    or any(k.startswith("replan") for k in steps)
                    or r.get("handoff")
                )
            assert not str(r.get("user_summary") or "").strip().startswith("{")
        finally:
            os.chdir(cwd)
            if old_pp:
                os.environ["PYTHONPATH"] = old_pp
            else:
                os.environ.pop("PYTHONPATH", None)

    def test_sample_path_when_samples_gt_zero(self, tmp_path):
        from coding.harness import coding_sample_repair
        f = tmp_path / "x.py"
        f.write_text("def f():\n    return 1\n", encoding="utf-8")
        old = os.environ.get("WW_CODING_SAMPLES")
        try:
            os.environ["WW_CODING_SAMPLES"] = "2"
            r = coding_sample_repair(str(f), error_text="AssertionError: fail")
            assert r.get("enabled") is True
            assert len(r.get("samples") or []) == 2
        finally:
            if old is None:
                os.environ.pop("WW_CODING_SAMPLES", None)
            else:
                os.environ["WW_CODING_SAMPLES"] = old


class TestLoopBridge:
    def test_redirect_via_user_message_path(self):
        from coding.orchestrator import reset_ticket_state, get_ticket_state
        from coding.loop_bridge import handle_coding_user_message
        from coding import orchestrator as orch
        reset_ticket_state()
        orch._ticket_state["goal"] = "fix leaf"
        orch._ticket_state["subgoal"] = "fix leaf"
        orch._ticket_state["status"] = "running"
        orch._ticket_state["plan"] = [{"id": "s1", "title": "fix leaf", "status": "active"}]
        r = handle_coding_user_message(
            message="Instead focus on hub performance",
            messages=[{"role": "user", "content": "fix leaf"}],
            force_redirect=True,
        )
        assert r["redirect"]["success"]
        assert r["redirect"]["subgoal"] != "fix leaf"
        assert not r["user_summary"].strip().startswith("{")
        assert get_ticket_state()["subgoal"] == r["redirect"]["subgoal"]

    def test_autocompact_over_threshold(self, tmp_path):
        from coding.loop_bridge import handle_coding_user_message
        msgs = [
            {"role": "user", "content": "a" * 15000},
            {"role": "assistant", "content": "b" * 15000},
        ]
        r = handle_coding_user_message(
            message="continue",
            messages=msgs,
            project_root=str(tmp_path),
            token_budget=500,
            force_autocompact=True,
        )
        assert r["autocompact"]["triggered"] is True


class TestCodingGoalExpanded:
    def test_bugfix_implement_refactor_write_tests_en_zh(self):
        from coding.mode import is_coding_goal
        assert is_coding_goal("bugfix the login crash")
        assert is_coding_goal("implement user auth module")
        assert is_coding_goal("refactor the payment service")
        assert is_coding_goal("write tests for calc.add")
        assert is_coding_goal("写测试 for the API")
        assert is_coding_goal("重构代码 leaf 函数")
        assert is_coding_goal("实现功能 in hub.py")
        assert not is_coding_goal("what is the weather today?")


class TestPMVersion010:
    def test_pm_version(self):
        from coding import PM_VERSION, get_status
        assert PM_VERSION == "0.11.0"
        st = get_status()
        assert st["version"] == "0.11.0"
        assert "model_route" in st["modules"]
        assert "loop_bridge" in st["modules"]
