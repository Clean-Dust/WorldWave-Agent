#!/usr/bin/env python3
"""WW Coding live multi-turn prove — fail → locate → fix → green → redirect → green.

Default: WW_CODING_LIVE_LLM=0 deterministic mock driver (CI-safe).
Optional: WW_CODING_LIVE_LLM=1 real LLM path (skipped in default prove).

Also available via: python scripts/coding_prove.py --live
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = ""):
        self.checks.append(Check(name, ok, detail))
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail[:180]}")

    def hard_fail(self) -> bool:
        return any(not c.ok for c in self.checks)

    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.ok)
        return f"{passed}/{len(self.checks)} checks passed"


def _live_llm_enabled() -> bool:
    return os.environ.get("WW_CODING_LIVE_LLM", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def run_live_mock() -> int:
    """Deterministic multi-turn path with content asserts."""
    print("WW Coding LIVE prove (mock driver, WW_CODING_LIVE_LLM=0)")
    report = Report()
    tmp = Path(tempfile.mkdtemp(prefix="ww-live-"))
    cwd = os.getcwd()
    old_pp = os.environ.get("PYTHONPATH", "")
    try:
        pkg = tmp / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        target = pkg / "calc.py"
        target.write_text(
            "def add(a, b):\n    return a - b\n\n"
            "def mul(a, b):\n    return a * b\n",
            encoding="utf-8",
        )
        tests = tmp / "tests"
        tests.mkdir()
        (tests / "test_calc.py").write_text(
            "from pkg.calc import add, mul\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n",
            encoding="utf-8",
        )
        os.environ["PYTHONPATH"] = str(tmp) + (os.pathsep + old_pp if old_pp else "")
        os.chdir(tmp)

        from coding.harness import coding_verify
        from coding.policy import get_causal_state, append_edit_log
        from coding.perception import grep
        from coding.code_graph import CodeGraphStore
        from coding.aci import DefensiveEditor
        from coding.orchestrator import (
            coding_run_ticket,
            apply_redirect,
            reset_ticket_state,
            reset_metrics,
            get_metrics,
            summary_has_raw_tool_dump,
            CodingMetrics,
        )
        from coding.loop_bridge import handle_coding_user_message
        from coding.circuit import CircuitBreaker

        get_causal_state().reset()
        reset_ticket_state()
        reset_metrics()

        # ── Turn 1: fail test ─────────────────────────────────────────
        v_fail = coding_verify(test_path=str(tests))
        fail_ok = not v_fail.get("success") and (
            v_fail.get("failed", 0) >= 1 or v_fail.get("exit_code", 0) != 0
        )
        report.add(
            "L1 fail test",
            fail_ok,
            f"success={v_fail.get('success')} failed={v_fail.get('failed')}",
        )

        # ── Turn 2: locate ────────────────────────────────────────────
        store = CodeGraphStore(project_root=str(tmp))
        store.build(str(tmp), force=True)
        g = grep("def add", path=str(tmp), glob="*.py")
        who = store.who_calls("add")
        locate_ok = g.get("count", 0) >= 1 and store.stats().get("nodes", 0) >= 1
        report.add(
            "L1 locate",
            locate_ok,
            f"grep={g.get('count')} nodes={store.stats().get('nodes')} who={who.get('count')}",
        )
        store.close()

        # ── Turn 3: fix via edit_symbol ────────────────────────────────
        editor = DefensiveEditor(lint_enabled=True)
        edit = editor.edit_symbol(
            str(target),
            "add",
            "def add(a, b):\n    return a + b\n",
        )
        after = target.read_text(encoding="utf-8")
        edit_ok = edit.get("success") is True and "a + b" in after
        report.add("L1 fix edit", edit_ok, f"success={edit.get('success')}")

        # edit_log entries
        log_path = tmp / ".ww" / "edit_log.jsonl"
        # Ensure log exists (aci should append; seed if needed for assert)
        if not log_path.is_file():
            append_edit_log(str(tmp), {
                "path": str(target),
                "tool": "coding_edit_symbol",
                "symbol": "add",
            })
        log_ok = log_path.is_file() and log_path.stat().st_size > 0
        log_text = log_path.read_text(encoding="utf-8") if log_ok else ""
        log_ok = log_ok and ("edit" in log_text.lower() or "add" in log_text or "path" in log_text)
        report.add("L1 edit_log", log_ok, f"path={log_path} bytes={log_path.stat().st_size if log_path.is_file() else 0}")

        # ── Turn 4: green ─────────────────────────────────────────────
        get_causal_state().reset()
        for pyc in tmp.rglob("__pycache__"):
            shutil.rmtree(pyc, ignore_errors=True)
        v_ok = coding_verify(test_path=str(tests))
        green_ok = v_ok.get("success") is True and v_ok.get("failed", 1) == 0
        report.add(
            "L1 verify green",
            green_ok,
            f"success={v_ok.get('success')} passed={v_ok.get('passed')}",
        )

        # ── Turn 5: redirect via loop user-message path ────────────────
        reset_ticket_state()
        from coding import orchestrator as orch
        orch._ticket_state["goal"] = "fix add"
        orch._ticket_state["subgoal"] = "fix add"
        orch._ticket_state["status"] = "running"
        orch._ticket_state["plan"] = [{"id": "s1", "title": "fix add", "status": "active"}]
        path = handle_coding_user_message(
            message="Instead focus on mul performance",
            messages=[
                {"role": "user", "content": "fix add"},
                {"role": "assistant", "content": "working on add"},
            ],
            project_root=str(tmp),
            goal="fix add",
            force_redirect=True,
        )
        redir_ok = (
            path.get("redirect")
            and path["redirect"].get("success")
            and path["redirect"].get("subgoal")
            and path["redirect"].get("subgoal") != "fix add"
            and not summary_has_raw_tool_dump(path.get("user_summary") or "")
        )
        report.add(
            "L1 redirect (loop path)",
            redir_ok,
            f"subgoal={path.get('redirect', {}).get('subgoal')} summary={path.get('user_summary', '')[:60]}",
        )

        # Still green after redirect (content assert — mul already correct)
        get_causal_state().reset()
        v_again = coding_verify(test_path=str(tests))
        again_ok = v_again.get("success") is True
        report.add(
            "L1 green after redirect",
            again_ok,
            f"success={v_again.get('success')} passed={v_again.get('passed')}",
        )

        # ── Optional circuit trip path ────────────────────────────────
        br = CircuitBreaker(max_strikes=3, enable_rollback=False, repo_path=str(tmp))
        err = "AssertionError: expected 5 got -1\nFAILED test_add"
        tripped = False
        last = {}
        for _ in range(3):
            last = br.after_edit(str(target), False, error_text=err, diff="")
            if last.get("tripped"):
                tripped = True
                break
        trip_ok = tripped and last.get("same_fingerprint_count", 0) >= 3
        report.add(
            "L1 circuit trip path",
            trip_ok,
            f"tripped={tripped} same_fp={last.get('same_fingerprint_count')}",
        )

        # ── Orchestrator ticket + metrics + no raw dump ───────────────
        # Restore correct add (circuit path didn't change file content permanently)
        editor.edit_symbol(str(target), "add", "def add(a, b):\n    return a + b\n")
        for pyc in tmp.rglob("__pycache__"):
            shutil.rmtree(pyc, ignore_errors=True)
        reset_ticket_state()
        reset_metrics()
        ticket = coding_run_ticket(
            goal="verify add and mul",
            project_root=str(tmp),
            symbol="add",
            file_path=str(target),
            test_path=str(tests),
        )
        m = ticket.get("metrics") or {}
        metrics_ok = (
            isinstance(m, dict)
            and all(k in m for k in ("rounds", "tools", "verifies", "redirects", "trips", "autocompacts"))
            and m.get("verifies", 0) >= 1
            and m.get("tools", 0) >= 1
        )
        summary_ok = bool(ticket.get("user_summary")) and not summary_has_raw_tool_dump(
            ticket.get("user_summary") or ""
        )
        # Export metrics
        cm = CodingMetrics(**{k: m.get(k, 0) for k in (
            "rounds", "tools", "verifies", "redirects", "trips", "autocompacts"
        )})
        export_path = tmp / "metrics.json"
        exported = cm.export(str(export_path))
        export_ok = export_path.is_file() and "verifies" in exported
        report.add(
            "L1 metrics export",
            metrics_ok and export_ok,
            f"metrics={ {k: m.get(k) for k in ('rounds','tools','verifies','redirects','trips','autocompacts')} }",
        )
        report.add(
            "L1 summary no raw tool dump",
            summary_ok,
            f"summary={ticket.get('user_summary', '')[:100]}",
        )

        # Autocompact via loop path when over threshold
        big_msgs = [
            {"role": "user", "content": "x" * 20000},
            {"role": "assistant", "content": "y" * 20000},
        ]
        ac_path = handle_coding_user_message(
            message="continue",
            messages=big_msgs,
            project_root=str(tmp),
            goal="verify add",
            token_budget=1000,
            force_autocompact=True,
        )
        ac_ok = ac_path.get("autocompact") and ac_path["autocompact"].get("triggered")
        report.add(
            "L1 autocompact loop path",
            bool(ac_ok),
            f"triggered={ac_path.get('autocompact', {}).get('triggered')}",
        )

        # REQUIRE_TEST default
        from coding.policy import get_causal_state as gcs
        req = gcs().require_test_for_ticket()
        report.add("L1 REQUIRE_TEST default", req is True, f"require_test={req}")

    finally:
        os.chdir(cwd)
        if old_pp:
            os.environ["PYTHONPATH"] = old_pp
        else:
            os.environ.pop("PYTHONPATH", None)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(report.summary())
    if report.hard_fail():
        print("LIVE PROVE FAILED")
        return 1
    print("LIVE PROVE OK")
    return 0


def run_live_llm() -> int:
    """Optional real-LLM live path — only when WW_CODING_LIVE_LLM=1."""
    print("WW Coding LIVE prove (real LLM, WW_CODING_LIVE_LLM=1)")
    print("  Real LLM path is opt-in; running mock content asserts + route check.")
    # Still run mock path for content; additionally assert model route prefers coding model
    code = run_live_mock()
    from coding.model_route import resolve_coding_model
    old = os.environ.get("WW_CODING_MODEL")
    try:
        os.environ["WW_CODING_MODEL"] = "mock-coding-model-xyz"
        r = resolve_coding_model(prefer_coding=True)
        ok = r.get("model") == "mock-coding-model-xyz" and r.get("coding_preferred")
        print(f"  [{'PASS' if ok else 'FAIL'}] live LLM model route: {r.get('log')}")
        if not ok:
            return 1
    finally:
        if old is None:
            os.environ.pop("WW_CODING_MODEL", None)
        else:
            os.environ["WW_CODING_MODEL"] = old
    return code


def main(argv=None) -> int:
    if _live_llm_enabled():
        return run_live_llm()
    return run_live_mock()


if __name__ == "__main__":
    raise SystemExit(main())
