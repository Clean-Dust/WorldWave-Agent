"""tests/test_coding_agent_path.py — PM 0.10 productized coding path."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "coding_repo"


class TestCodingMode:
    def test_is_coding_goal(self):
        from coding.mode import is_coding_goal
        assert is_coding_goal("fix the bug in pkg/core.py leaf function")
        assert is_coding_goal("implement refactor of coding/orchestrator.py")
        assert is_coding_goal("run pytest and edit_symbol on add")
        assert is_coding_goal("bugfix regression in leaf")
        assert is_coding_goal("write tests for the module")
        assert is_coding_goal("重构代码并写测试")
        assert not is_coding_goal("what is the weather today?")
        assert not is_coding_goal("hello")

    def test_build_coding_context_injects_essence(self):
        from coding.mode import build_coding_context
        ctx = build_coding_context(goal="fix failing test in calc.py", force=True)
        assert ctx["active"] is True
        assert "CODING_AGENT" in ctx["system_block"] or "repo_map" in ctx["system_block"]
        assert ctx["role"].get("role") in ("coder", "architect", "reviewer")

    def test_architect_cannot_edit(self):
        from coding.mode import architect_cannot_edit_proof
        r = architect_cannot_edit_proof("coding_edit_symbol")
        assert r["ok"] is True
        assert r["architect_allowed"] is False
        assert r["coder_allowed"] is True

    def test_agents_md_load(self):
        from coding.mode import load_agents_md
        # Project root may have AGENTS.md; function must not crash
        content = load_agents_md(str(ROOT))
        assert isinstance(content, str)


class TestOrchestrator:
    def test_run_ticket_steps(self, tmp_path):
        from coding.orchestrator import coding_run_ticket, reset_ticket_state
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        f = pkg / "m.py"
        f.write_text("def leaf(x):\n    return x * 2\n", encoding="utf-8")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_m.py").write_text(
            "from pkg.m import leaf\n\ndef test_leaf():\n    assert leaf(2) == 4\n",
            encoding="utf-8",
        )
        old_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(tmp_path) + (os.pathsep + old_pp if old_pp else "")
        cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            reset_ticket_state()
            r = coding_run_ticket(
                goal="verify leaf works",
                project_root=str(tmp_path),
                symbol="leaf",
                file_path=str(f),
                test_path=str(tests),
            )
            assert "steps" in r
            assert "repo_map" in r["steps"]
            assert "locate" in r["steps"]
            assert "verify" in r["steps"]
            assert r.get("user_summary")
            # user_summary must not be raw JSON dump
            assert not r["user_summary"].strip().startswith("{")
        finally:
            os.chdir(cwd)
            if old_pp:
                os.environ["PYTHONPATH"] = old_pp
            else:
                os.environ.pop("PYTHONPATH", None)

    def test_redirect_changes_subgoal(self):
        from coding.orchestrator import apply_redirect, reset_ticket_state, get_ticket_state
        reset_ticket_state()
        st = get_ticket_state()
        # seed
        from coding import orchestrator as orch
        orch._ticket_state["goal"] = "fix leaf"
        orch._ticket_state["subgoal"] = "fix leaf"
        orch._ticket_state["plan"] = [{"id": "s1", "title": "edit leaf", "status": "pending"}]
        before = orch._ticket_state["subgoal"]
        r = apply_redirect("Instead fix mid() performance in hub")
        assert r["success"]
        assert r["changed"]
        assert r["subgoal"] != before
        assert "REDIRECT" in (r["plan"][0].get("title") or "")
        assert get_ticket_state()["subgoal"] == r["subgoal"]


class TestAutoCompact:
    def test_summary_structure(self, tmp_path):
        from coding.autocompact import build_coding_summary
        # create fake edit log
        ww = tmp_path / ".ww"
        ww.mkdir()
        (ww / "edit_log.jsonl").write_text(
            '{"path": "a.py", "tool": "coding_edit_symbol"}\n',
            encoding="utf-8",
        )
        s = build_coding_summary(
            goal="fix a",
            files_touched=["a.py"],
            test_status={"success": False, "summary": "1 failed", "fingerprint": "abc"},
            open_issues=["need replan"],
            project_root=str(tmp_path),
            max_tokens=400,
        )
        assert s["edit_log_preserved"] is True
        assert "goal:" in s["summary"]
        assert "Files touched" in s["summary"] or "files" in s["summary"].lower()
        assert s["token_estimate"] <= 500
        # edit log still exists
        assert (ww / "edit_log.jsonl").is_file()

    def test_should_trigger_near_budget(self):
        from coding.autocompact import should_autocompact, autocompact_messages
        assert should_autocompact(current_tokens=9000, max_tokens=10000, ratio=0.85)
        assert not should_autocompact(current_tokens=100, max_tokens=10000, ratio=0.85)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "fix bug " * 5000},
            {"role": "assistant", "content": "ok " * 5000},
        ]
        r = autocompact_messages(msgs, goal="fix", force=True)
        assert r["triggered"] is True
        assert r["edit_log_preserved"] is True
        assert r["summary"] is not None


class TestDefaults:
    def test_require_test_default_on(self):
        from coding.policy import get_causal_state
        st = get_causal_state()
        old = os.environ.get("WW_CODING_REQUIRE_TEST")
        try:
            os.environ.pop("WW_CODING_REQUIRE_TEST", None)
            # re-read via method (reads env each time)
            assert st.require_test_for_ticket() is True
        finally:
            if old is None:
                os.environ.pop("WW_CODING_REQUIRE_TEST", None)
            else:
                os.environ["WW_CODING_REQUIRE_TEST"] = old

    def test_samples_default_zero(self):
        from coding.harness import coding_sample_repair
        old = os.environ.get("WW_CODING_SAMPLES")
        try:
            os.environ.pop("WW_CODING_SAMPLES", None)
            r = coding_sample_repair("/tmp/x.py", "err")
            assert r.get("enabled") is False
        finally:
            if old is None:
                os.environ.pop("WW_CODING_SAMPLES", None)
            else:
                os.environ["WW_CODING_SAMPLES"] = old

    def test_pm_version_0_10(self):
        from coding import PM_VERSION, get_status
        assert PM_VERSION == "0.11.0"
        st = get_status()
        assert st["version"] == "0.11.0"
        assert "orchestrator" in st["modules"]
        assert "mode" in st["modules"]
        assert "autocompact" in st["modules"]
        assert "model_route" in st["modules"]
        assert "loop_bridge" in st["modules"]
        assert st["defaults"]["require_test"] is True

    def test_tools_registered(self):
        from coding import get_all_tools, register_tools
        from tools.registry import ToolRegistry
        tools = {t["name"]: t for t in get_all_tools()}
        assert "coding_run_ticket" in tools
        assert "coding_redirect" in tools
        assert "coding_autocompact" in tools
        assert tools["coding_run_ticket"].get("permission") == "requires_approval"
        assert tools["coding_redirect"].get("permission") == "safe"
        reg = ToolRegistry()
        n = register_tools(reg)
        assert n > 20
        assert reg._tools["coding_edit_symbol"].permission == "requires_approval"
        assert reg._tools["coding_run_ticket"].permission == "requires_approval"


class TestOrchestratorTools:
    def test_redirect_tool_handler(self):
        from coding.orchestrator import get_orchestrator_tools, reset_ticket_state
        reset_ticket_state()
        tools = {t["name"]: t for t in get_orchestrator_tools()}
        h = tools["coding_redirect"]["handler"]
        r = h(message="now work on hub_entry instead")
        assert r["success"]
        assert r["subgoal"]
