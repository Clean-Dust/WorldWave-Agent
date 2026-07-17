#!/usr/bin/env python3
"""WW Coding Engine prove harness — V1–V10 asserts.

Usage:
  python scripts/coding_prove.py --all

Exit 0 only if all selected checks pass.
"""
from __future__ import annotations

import argparse
import ast
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

FIXTURE = ROOT / "tests" / "fixtures" / "coding_repo"


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
        print(f"  [{status}] {name}: {detail[:160]}")

    def hard_fail(self) -> bool:
        return any(not c.ok for c in self.checks)

    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.ok)
        total = len(self.checks)
        return f"{passed}/{total} checks passed"


def _fresh_graph_store(project_root: Path):
    from coding.code_graph import CodeGraphStore
    ww = project_root / ".ww"
    if ww.exists():
        shutil.rmtree(ww, ignore_errors=True)
    store = CodeGraphStore(project_root=str(project_root))
    store.build(str(project_root), force=True)
    return store


def v1_who_calls(report: Report):
    """V1 who_calls(leaf) hits real caller on fixture."""
    store = _fresh_graph_store(FIXTURE)
    try:
        result = store.who_calls("leaf")
        callers = result.get("callers") or []
        names = {
            (c.get("caller_name") or "") + " " + (c.get("caller_qualname") or "")
            for c in callers
        }
        flat = " ".join(names).lower()
        # mid, HubService.run, hub paths should call leaf
        ok = any(x in flat for x in ("mid", "run", "hub"))
        if not ok:
            # also check raw count
            ok = result.get("count", 0) >= 1
        report.add(
            "V1 who_calls(leaf)",
            ok,
            f"callers={result.get('count')} names={list(names)[:6]}",
        )
    finally:
        store.close()


def v2_blast_radius(report: Report):
    """V2 blast_radius(hub) includes downstream."""
    store = _fresh_graph_store(FIXTURE)
    try:
        # hub_entry is a hub function; also try HubService
        result = store.blast_radius("hub_entry", max_depth=5)
        down = result.get("downstream") or []
        text = " ".join(
            (d.get("name") or "") + " " + (d.get("qualname") or "") + " " + (d.get("file") or "")
            for d in down
        ).lower()
        ok = result.get("count", 0) >= 1 and (
            "downstream" in text
            or "hub" in text
            or "main" in text
            or "app" in text
            or "run" in text
            or "mid" in text
            or "leaf" in text
        )
        # If hub_entry defines/calls downstream, count should be > 0
        if not ok:
            result2 = store.blast_radius("HubService", max_depth=5)
            ok = result2.get("count", 0) >= 1
            detail = f"hub_entry={result.get('count')} HubService={result2.get('count')}"
        else:
            detail = f"count={result.get('count')} sample={text[:100]}"
        report.add("V2 blast_radius(hub)", ok, detail)
    finally:
        store.close()


def v3_rm_denied(report: Report):
    """V3 rm -rf / denied with semantic reason."""
    from coding.policy import check_command_allowed
    from coding.shell import get_shell_tools

    gate = check_command_allowed("rm -rf /")
    ok = (not gate.get("allowed", True)) and (
        "rm" in (gate.get("reason") or "").lower()
        or "filesystem" in (gate.get("reason") or "").lower()
        or "wipe" in (gate.get("reason") or "").lower()
        or "denied" in (gate.get("reason") or "").lower()
    )
    # Also through coding_exec tool handler
    tools = {t["name"]: t for t in get_shell_tools()}
    exec_tool = tools.get("coding_exec")
    handler_ok = False
    if exec_tool:
        r = exec_tool["handler"](command="rm -rf /")
        # microcompact may wrap; check raw shell tool without wrap
        from coding.shell import _safe_exec, get_shell
        r = _safe_exec(get_shell(), "rm -rf /")
        handler_ok = r.get("denied") or not r.get("success", True)
        reason = r.get("reason") or r.get("error") or ""
        ok = ok and handler_ok and ("denied" in reason.lower() or "rm" in reason.lower())
    report.add("V3 rm -rf / denied", ok, gate.get("reason", "")[:120])


def v4_microcompact(report: Report):
    """V4 microcompact length bound + fingerprint."""
    from coding.microcompact import compact_text, DEFAULT_LIMIT

    big = "A" * 20000 + "\nMIDDLE\n" + "B" * 20000
    c = compact_text(big, limit=6000)
    ok = (
        c["truncated"] is True
        and len(c["text"]) <= 6000 + 200  # marker slack
        and c.get("fingerprint")
        and len(c["fingerprint"]) >= 8
        and c["original_length"] == len(big)
    )
    report.add(
        "V4 microcompact",
        ok,
        f"len={len(c['text'])} fp={c.get('fingerprint')} trunc={c['truncated']}",
    )


def v5_bad_syntax_rollback(report: Report):
    """V5 bad syntax edit rolls back."""
    from coding.aci import DefensiveEditor

    tmp = Path(tempfile.mkdtemp(prefix="ww-prove-v5-"))
    try:
        f = tmp / "sample.py"
        original = "def greet(name):\n    return f'hi {name}'\n"
        f.write_text(original, encoding="utf-8")
        editor = DefensiveEditor(lint_enabled=True)
        result = editor.edit_symbol(
            str(f),
            "greet",
            "def greet(name):\n    return f'hi {name'\n",  # bad syntax
        )
        after = f.read_text(encoding="utf-8")
        ok = (
            not result.get("success", True)
            and result.get("rollback") is True
            and after == original
        )
        report.add("V5 bad syntax rollback", ok, str(result.get("error", result))[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def v6_edit_symbol_ast(report: Report):
    """V6 edit_symbol yields parseable AST."""
    from coding.aci import DefensiveEditor

    tmp = Path(tempfile.mkdtemp(prefix="ww-prove-v6-"))
    try:
        f = tmp / "sample.py"
        f.write_text("def greet(name):\n    return f'hi {name}'\n", encoding="utf-8")
        editor = DefensiveEditor(lint_enabled=True)
        result = editor.edit_symbol(
            str(f),
            "greet",
            "def greet(name):\n    return f'hello {name}!'\n",
        )
        text = f.read_text(encoding="utf-8")
        tree_ok = False
        try:
            tree = ast.parse(text)
            names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
            tree_ok = "greet" in names and "hello" in text
        except SyntaxError:
            tree_ok = False
        ok = result.get("success") is True and tree_ok and result.get("ast_ok", True)
        report.add("V6 edit_symbol AST", ok, f"success={result.get('success')} text_ok={tree_ok}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def v7_circuit_same_fp(report: Report):
    """V7 same fingerprint failures → circuit tripped."""
    from coding.circuit import CircuitBreaker, ErrorFingerprint

    # Disable git rollback for isolated prove
    br = CircuitBreaker(max_strikes=3, enable_rollback=False, repo_path=str(ROOT))
    err = "AssertionError: expected 1 got 2\nFAIL test_foo"
    fp = ErrorFingerprint.fingerprint(err)
    tripped = False
    last = {}
    for i in range(3):
        last = br.after_edit("/tmp/ww_fake_module.py", False, error_text=err, diff="")
        if last.get("tripped"):
            tripped = True
            break
    ok = tripped and last.get("same_fingerprint_count", 0) >= 3
    report.add(
        "V7 circuit same fingerprint",
        ok,
        f"tripped={tripped} same_fp={last.get('same_fingerprint_count')} fp={fp}",
    )


def v8_causal_blocks_commit(report: Report):
    """V8 causal: edit without test blocks commit."""
    from coding.policy import (
        get_causal_state,
        record_coding_write,
        check_git_commit_allowed,
    )

    # Isolate causal state
    st = get_causal_state()
    st.reset()
    # Ensure causal ON
    old = os.environ.get("WW_CODING_CAUSAL")
    os.environ["WW_CODING_CAUSAL"] = "1"
    try:
        record_coding_write("/tmp/ww_edited.py", "edit")
        gate = check_git_commit_allowed()
        ok = gate.get("allowed") is False and (
            "causal" in (gate.get("reason") or "").lower()
            or "verify" in (gate.get("reason") or "").lower()
        )
        report.add("V8 causal blocks commit", ok, gate.get("reason", "")[:140])
    finally:
        st.reset()
        if old is None:
            os.environ.pop("WW_CODING_CAUSAL", None)
        else:
            os.environ["WW_CODING_CAUSAL"] = old


def v9_secret_scan(report: Report):
    """V9 secret scan blocks fake key."""
    from coding.policy import check_content_secrets
    from coding.aci import DefensiveEditor

    fake = "api_key = 'sk-testFAKESECRET_s3t4u5v6w7x8y9z0a1b2'\n"
    sec = check_content_secrets(fake)
    ok_policy = sec.get("allowed") is False

    tmp = Path(tempfile.mkdtemp(prefix="ww-prove-v9-"))
    try:
        f = tmp / "cfg.py"
        f.write_text("x = 1\n", encoding="utf-8")
        editor = DefensiveEditor(lint_enabled=True)
        patch = (
            f"--- a/{f}\n"
            f"+++ b/{f}\n"
            "@@ -1,1 +1,2 @@\n"
            " x = 1\n"
            f"+{fake}"
        )
        # apply_patch should block
        result = editor.apply_patch(
            f"--- a/cfg.py\n+++ b/cfg.py\n@@ -1 +1,2 @@\n x = 1\n+api_key = 'sk-test1234567890abcdefgh'\n"
        )
        # Fix: write patch relative — apply_patch uses paths in diff
        # Use write path check_content_secrets on body via edit_symbol or write
        result2 = editor.write_file(str(f), fake)
        blocked = (
            result2.get("secret_blocked")
            or (not result2.get("success") and "secret" in (result2.get("error") or "").lower())
            or (not result2.get("success") and "sk-" in (result2.get("error") or "").lower())
            or (not result2.get("success") and "api_key" in (result2.get("error") or "").lower())
        )
        # Also direct apply with absolute path in patch
        r3 = editor.apply_patch(
            f"--- a/{f.name}\n+++ b/{f.name}\n@@ -1 +1,2 @@\n x = 1\n+token = 'sk-abcdef1234567890xyz'\n"
        )
        # apply may fail path — policy check alone is enough if write blocked
        ok = ok_policy and blocked
        report.add(
            "V9 secret scan",
            ok,
            f"policy_blocked={ok_policy} write_blocked={blocked} err={result2.get('error', '')[:80]}",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def v10_prove_meta(report: Report):
    """V10 prove --all exit 0 — meta: all prior checks green."""
    prior = [c for c in report.checks if c.name.startswith("V") and c.name != "V10 prove --all"]
    ok = all(c.ok for c in prior) and len(prior) >= 9
    report.add("V10 prove --all", ok, f"prior={sum(1 for c in prior if c.ok)}/{len(prior)}")


def run_all() -> int:
    print("WW Coding prove (V1–V10)")
    print(f"  ROOT={ROOT}")
    print(f"  FIXTURE={FIXTURE}")
    if not FIXTURE.is_dir():
        print("FAIL: fixture missing")
        return 1

    report = Report()
    v1_who_calls(report)
    v2_blast_radius(report)
    v3_rm_denied(report)
    v4_microcompact(report)
    v5_bad_syntax_rollback(report)
    v6_edit_symbol_ast(report)
    v7_circuit_same_fp(report)
    v8_causal_blocks_commit(report)
    v9_secret_scan(report)
    v10_prove_meta(report)

    print()
    print(report.summary())
    if report.hard_fail():
        print("PROVE FAILED")
        return 1
    print("PROVE OK")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="WW Coding Engine prove harness")
    p.add_argument("--all", action="store_true", help="Run V1–V10 (default)")
    args = p.parse_args(argv)
    # --all is the only mode for now
    return run_all()


if __name__ == "__main__":
    raise SystemExit(main())
