"""coding/harness.py — Execution-grounded verify, replan, sample repair, worktree.

Tools:
  coding_verify, coding_sample_repair, coding_adversarial_tests,
  coding_replan, coding_worktree_start, coding_worktree_finish
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from coding.microcompact import fingerprint as text_fingerprint
from coding.policy import record_verify_result, get_causal_state


# ── Verify ────────────────────────────────────────────────────────────

def coding_verify(
    test_path: str = None,
    extra_args: List[str] = None,
    framework: str = "pytest",
) -> Dict:
    """Run tests → structured pass/fail + fingerprint + summary."""
    from coding.circuit import TestRunner, ErrorFingerprint

    runner = TestRunner(framework)
    result = runner.run(test_path, extra_args=extra_args)
    output = result.get("output", "") or ""
    fp = ErrorFingerprint.fingerprint(output) if not result.get("success") else text_fingerprint(
        f"pass:{result.get('passed', 0)}:{result.get('failed', 0)}"
    )
    summary_parts = []
    if result.get("success"):
        summary_parts.append(f"PASS: {result.get('passed', 0)} passed")
    else:
        summary_parts.append(
            f"FAIL: {result.get('passed', 0)} passed, {result.get('failed', 0)} failed"
        )
        if result.get("errors"):
            summary_parts.append("; ".join(result["errors"][:3]))
    out = {
        "success": bool(result.get("success")),
        "passed": result.get("passed", 0),
        "failed": result.get("failed", 0),
        "total": result.get("total", 0),
        "exit_code": result.get("exit_code", -1),
        "fingerprint": fp,
        "summary": " | ".join(summary_parts),
        "errors": result.get("errors", [])[:20],
        "output": output[-3000:] if output else "",
        "framework": framework,
        "test_path": test_path,
    }
    if result.get("error") and not output:
        out["error"] = result["error"]
        out["success"] = False
        out["fingerprint"] = ErrorFingerprint.fingerprint(str(result["error"]))
    record_verify_result(out)
    return out


# ── Sample repair ─────────────────────────────────────────────────────

def coding_sample_repair(
    filepath: str,
    error_text: str = "",
    hint: str = "",
) -> Dict:
    """Multi-sample repair scaffold. WW_CODING_SAMPLES=k (default 0 = disabled)."""
    k = int(os.environ.get("WW_CODING_SAMPLES", "0") or "0")
    if k <= 0:
        return {
            "enabled": False,
            "message": (
                "coding_sample_repair disabled (WW_CODING_SAMPLES=0). "
                "Set WW_CODING_SAMPLES=k (k>0) to generate k repair candidate scaffolds."
            ),
            "samples": [],
        }
    if not os.path.isfile(filepath):
        return {"enabled": True, "error": f"File not found: {filepath}", "samples": []}

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    from coding.circuit import ErrorFingerprint
    from coding.perception import explain_failure

    fp = ErrorFingerprint.fingerprint(error_text or content)
    explanation = explain_failure(error_text) if error_text else {"bullets": [], "summary": ""}
    samples = []
    for i in range(k):
        samples.append({
            "index": i,
            "strategy": [
                "minimal_local_fix",
                "widen_guard_clauses",
                "refactor_extract_helper",
                "add_type_coercion",
                "defensive_defaults",
            ][i % 5],
            "fingerprint": fp,
            "hint": hint or explanation.get("summary", ""),
            "guidance": (
                f"Candidate {i + 1}/{k}: apply strategy "
                f"{['minimal_local_fix','widen_guard_clauses','refactor_extract_helper','add_type_coercion','defensive_defaults'][i % 5]}. "
                f"Failure bullets: {explanation.get('bullets', [])[:3]}"
            ),
            "filepath": filepath,
        })
    return {
        "enabled": True,
        "k": k,
        "fingerprint": fp,
        "samples": samples,
        "message": f"Generated {k} repair sample scaffolds for {filepath}",
    }


# ── Adversarial tests ─────────────────────────────────────────────────

def coding_adversarial_tests(
    target_path: str,
    function_name: str = None,
    output_path: str = None,
    write: bool = False,
) -> Dict:
    """Draft edge-case adversarial tests (opt-in write)."""
    target_path = os.path.abspath(os.path.expanduser(target_path))
    if not os.path.isfile(target_path):
        return {"error": f"File not found: {target_path}"}

    import ast
    with open(target_path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"error": f"Cannot parse: {e}"}

    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if function_name and node.name != function_name:
                continue
            if node.name.startswith("_") and node.name != function_name:
                continue
            funcs.append(node.name)

    if not funcs:
        return {"error": "No functions found to test", "draft": ""}

    mod = os.path.splitext(os.path.basename(target_path))[0]
    lines = [
        '"""Adversarial / edge-case tests (auto-drafted by coding_adversarial_tests)."""',
        "import pytest",
        f"# Target: {target_path}",
        "",
    ]
    for fn in funcs[:10]:
        lines.append(f"def test_{fn}_none_input():")
        lines.append(f"    # Edge: None / missing")
        lines.append(f"    # from {mod} import {fn}")
        lines.append(f"    # with pytest.raises((TypeError, ValueError, AttributeError)):")
        lines.append(f"    #     {fn}(None)")
        lines.append(f"    pass  # draft — wire import and assertions")
        lines.append("")
        lines.append(f"def test_{fn}_empty_input():")
        lines.append(f"    # Edge: empty string / empty list")
        lines.append(f"    pass  # draft")
        lines.append("")
        lines.append(f"def test_{fn}_large_input():")
        lines.append(f"    # Edge: large payload")
        lines.append(f"    pass  # draft")
        lines.append("")

    draft = "\n".join(lines)
    written = None
    if write:
        if not output_path:
            d = os.path.dirname(target_path)
            output_path = os.path.join(d, f"test_adversarial_{mod}.py")
        output_path = os.path.abspath(output_path)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(draft)
        written = output_path
        try:
            from coding.policy import record_coding_write, append_edit_log, find_project_root
            record_coding_write(output_path, "adversarial_tests")
            append_edit_log(find_project_root(output_path), {
                "tool": "coding_adversarial_tests",
                "path": output_path,
                "success": True,
            })
        except Exception:
            pass

    return {
        "success": True,
        "functions": funcs[:10],
        "draft": draft,
        "written": written,
        "message": "Opt-in adversarial test draft" + (f" written to {written}" if written else " (not written; set write=true)"),
    }


# ── Replan ────────────────────────────────────────────────────────────

_plan_state: Dict = {
    "subgoals": [],
    "failures": [],
    "version": 0,
}


def coding_replan(
    plan_state: Dict = None,
    failure_fingerprints: List[str] = None,
    goal: str = "",
    notes: str = "",
    explain: Dict = None,
) -> Dict:
    """Produce new subgoals from plan state + failure fingerprints.

    PM 0.10: optional *explain* from coding_explain_failure is folded into
    replan subgoals and plan_state so the next edit has failure context.
    """
    global _plan_state
    state = plan_state or _plan_state
    fps = failure_fingerprints or []
    explain = explain or {}
    if not fps:
        # Pull from circuit breaker history
        try:
            from coding.circuit import get_breaker
            st = get_breaker().get_status()
            for fp, info in (st.get("tracked_files") or {}).items():
                if info.get("tripped"):
                    fps.append(f"tripped:{fp}")
        except Exception:
            pass

    # Collect recent failure fingerprints from causal/verify
    causal = get_causal_state().to_dict()
    last_v = causal.get("last_verify") or {}
    if last_v.get("fingerprint") and not last_v.get("success"):
        fps.append(last_v["fingerprint"])

    # Dedup fingerprints → cluster
    unique_fps = list(dict.fromkeys(fps))
    subgoals = []
    if goal:
        subgoals.append({"id": "g0", "title": f"Clarify goal: {goal[:120]}", "status": "pending"})
    subgoals.append({
        "id": "g1",
        "title": "Rebuild perception: coding_repo_map + coding_grep on failure sites",
        "status": "pending",
    })
    subgoals.append({
        "id": "g2",
        "title": "coding_graph_who_calls / blast_radius on failing symbols",
        "status": "pending",
    })
    # explain_failure context into replan
    bullets = list(explain.get("bullets") or [])
    if explain.get("summary") or bullets:
        title = explain.get("summary") or (bullets[0] if bullets else "failure analysis")
        subgoals.append({
            "id": "g_explain",
            "title": f"From explain_failure: {str(title)[:160]}",
            "status": "pending",
            "explain_bullets": bullets[:8],
            "priority": "high",
        })
    if unique_fps:
        subgoals.append({
            "id": "g3",
            "title": f"Address {len(unique_fps)} distinct failure fingerprint(s): {', '.join(unique_fps[:5])}",
            "status": "pending",
            "fingerprints": unique_fps[:10],
        })
        # Same fingerprint thrashing → suggest handoff
        if len(unique_fps) == 1 and len(fps) >= 3:
            subgoals.append({
                "id": "g_handoff",
                "title": "Circuit-style handoff: same fingerprint thrice — write handoff report, stop thrashing",
                "status": "pending",
                "priority": "high",
            })
    subgoals.append({
        "id": "g4",
        "title": "Apply minimal coding_edit_symbol fix; avoid broad rewrites",
        "status": "pending",
    })
    subgoals.append({
        "id": "g5",
        "title": "coding_verify until green; then allow commit",
        "status": "pending",
    })
    if notes:
        subgoals.append({"id": "g_notes", "title": f"Notes: {notes[:200]}", "status": "pending"})

    new_state = {
        "subgoals": subgoals,
        "failures": unique_fps,
        "explain": {
            "summary": explain.get("summary") or "",
            "bullets": bullets[:8],
        },
        "version": int(state.get("version", 0)) + 1,
        "goal": goal or state.get("goal", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "previous_subgoals": state.get("subgoals", [])[:20],
    }
    _plan_state = new_state
    return {
        "success": True,
        "plan_state": new_state,
        "subgoals": subgoals,
        "failure_fingerprints": unique_fps,
        "explain_used": bool(explain.get("summary") or bullets),
        "message": f"Replanned with {len(subgoals)} subgoals from {len(unique_fps)} failure fingerprint(s)",
    }


# ── Worktree ──────────────────────────────────────────────────────────

_worktrees: Dict[str, Dict] = {}


def coding_worktree_start(branch: str = None, path: str = None, base: str = "HEAD") -> Dict:
    """Optional git worktree for isolated edits."""
    if not shutil.which("git"):
        return {"error": "git not available", "optional": True}
    # Find repo root
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {"error": "Not a git repository", "optional": True}
        repo = r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"error": str(e), "optional": True}

    branch = branch or f"ww-work/{uuid.uuid4().hex[:8]}"
    if path is None:
        path = os.path.join(tempfile.gettempdir(), f"ww-wt-{uuid.uuid4().hex[:8]}")
    path = os.path.abspath(path)

    # Create branch + worktree
    try:
        # Ensure base exists
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, path, base],
            capture_output=True, text=True, timeout=30, cwd=repo, check=False,
        )
        # If branch exists, try without -b
        if not os.path.isdir(path):
            r2 = subprocess.run(
                ["git", "worktree", "add", path, branch],
                capture_output=True, text=True, timeout=30, cwd=repo,
            )
            if r2.returncode != 0:
                return {
                    "success": False,
                    "error": r2.stderr or r2.stdout,
                    "optional": True,
                }
    except subprocess.TimeoutExpired:
        return {"error": "git worktree timed out", "optional": True}

    meta = {
        "path": path,
        "branch": branch,
        "repo": repo,
        "base": base,
        "started_at": time.time(),
    }
    _worktrees[path] = meta
    return {"success": True, "worktree": meta, "optional": True}


def coding_worktree_finish(
    path: str,
    action: str = "remove",
    merge_into: str = None,
) -> Dict:
    """Finish a worktree: remove or merge then remove."""
    if not shutil.which("git"):
        return {"error": "git not available", "optional": True}
    path = os.path.abspath(path)
    meta = _worktrees.get(path, {})
    repo = meta.get("repo")
    if not repo:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10, cwd=path,
            )
            repo = r.stdout.strip() if r.returncode == 0 else os.getcwd()
        except Exception:
            repo = os.getcwd()

    result = {"path": path, "action": action, "optional": True}
    if action == "merge" and merge_into and meta.get("branch"):
        m = subprocess.run(
            ["git", "merge", meta["branch"]],
            capture_output=True, text=True, timeout=30, cwd=repo,
        )
        result["merge"] = {
            "success": m.returncode == 0,
            "output": (m.stdout or m.stderr)[:500],
            "into": merge_into,
        }

    rm = subprocess.run(
        ["git", "worktree", "remove", "--force", path],
        capture_output=True, text=True, timeout=30, cwd=repo,
    )
    result["remove"] = {
        "success": rm.returncode == 0,
        "output": (rm.stdout or rm.stderr)[:500],
    }
    _worktrees.pop(path, None)
    result["success"] = result["remove"]["success"]
    return result


def get_harness_tools() -> List[Dict]:
    return [
        {
            "name": "coding_verify",
            "description": "Run tests and return structured pass/fail + fingerprint + summary. Grounds edits in execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_path": {"type": "string", "description": "Optional test path"},
                    "extra_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra pytest args",
                    },
                    "framework": {"type": "string", "default": "pytest"},
                },
            },
            "handler": lambda test_path=None, extra_args=None, framework="pytest": coding_verify(
                test_path, extra_args, framework
            ),
            "category": "code_repair",
            "permission": "requires_approval",
        },
        {
            "name": "coding_sample_repair",
            "description": "Generate k repair candidate scaffolds. Disabled unless WW_CODING_SAMPLES=k>0.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "error_text": {"type": "string"},
                    "hint": {"type": "string"},
                },
                "required": ["filepath"],
            },
            "handler": lambda filepath, error_text="", hint="": coding_sample_repair(
                filepath, error_text, hint
            ),
            "category": "code_repair",
            "permission": "safe",
        },
        {
            "name": "coding_adversarial_tests",
            "description": "Draft edge-case adversarial tests for a module. Set write=true to write the draft file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_path": {"type": "string"},
                    "function_name": {"type": "string"},
                    "output_path": {"type": "string"},
                    "write": {"type": "boolean", "default": False},
                },
                "required": ["target_path"],
            },
            "handler": lambda target_path, function_name=None, output_path=None, write=False: coding_adversarial_tests(
                target_path, function_name, output_path, write
            ),
            "category": "code_repair",
            "permission": "requires_approval",
        },
        {
            "name": "coding_replan",
            "description": "Replan coding subgoals from plan state and failure fingerprints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_state": {"type": "object", "description": "Prior plan state dict"},
                    "failure_fingerprints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "goal": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
            "handler": lambda plan_state=None, failure_fingerprints=None, goal="", notes="",
                              explain=None: coding_replan(
                plan_state, failure_fingerprints, goal, notes, explain=explain
            ),
            "category": "code_planning",
            "permission": "safe",
        },
        {
            "name": "coding_worktree_start",
            "description": "Optional: create a git worktree for isolated coding work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {"type": "string"},
                    "path": {"type": "string"},
                    "base": {"type": "string", "default": "HEAD"},
                },
            },
            "handler": lambda branch=None, path=None, base="HEAD": coding_worktree_start(branch, path, base),
            "category": "code_aci",
            "permission": "requires_approval",
        },
        {
            "name": "coding_worktree_finish",
            "description": "Optional: remove (or merge then remove) a coding worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "action": {"type": "string", "enum": ["remove", "merge"], "default": "remove"},
                    "merge_into": {"type": "string"},
                },
                "required": ["path"],
            },
            "handler": lambda path, action="remove", merge_into=None: coding_worktree_finish(
                path, action, merge_into
            ),
            "category": "code_aci",
            "permission": "requires_approval",
        },
    ]
