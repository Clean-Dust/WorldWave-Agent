#!/usr/bin/env python3
"""WW Coding head-to-head vs external CLIs (F2b, PM 0.13).

Detects `claude` / `codex` on PATH. If none available: exit 0 with
JSON `skipped=true`, `external_claim=forbidden` (does not fail CI).

If available: best-effort run of ≥10 hard subset arena tasks in sandbox
vs that CLI. Limitations are documented in the report — external CLIs
have different tool surfaces and auth; results are informational.

Usage:
  python scripts/coding_h2h.py
  python scripts/coding_h2h.py --suite hard_subset10
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "results" / "coding_h2h"
TASKS = ROOT / "tests" / "fixtures" / "coding_arena" / "tasks"


def _which_cli() -> Dict[str, Optional[str]]:
    return {
        "claude": shutil.which("claude"),
        "codex": shutil.which("codex"),
    }


def _hard_subset(n: int = 10) -> List[str]:
    ids: List[str] = []
    if not TASKS.is_dir():
        return ids
    # Prefer hard / adversarial / realrepo
    scored = []
    for child in TASKS.iterdir():
        meta_path = child / "task.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        score = 0
        if meta.get("hard"):
            score += 3
        if meta.get("adversarial"):
            score += 2
        if meta.get("realrepo") or "realrepo" in (meta.get("tags") or []):
            score += 2
        if meta.get("supports_redirect"):
            score += 1
        scored.append((score, meta.get("id") or child.name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [tid for _, tid in scored[:n]]


def _skip_report(reason: str, clis: Dict[str, Optional[str]]) -> int:
    payload = {
        "skipped": True,
        "external_claim": "forbidden",
        "reason": reason,
        "clis_detected": clis,
        "pm": "0.13.0-endpoint",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "No claude/codex CLI on PATH — cannot claim exceeds external agents.",
            "F2b is optional for engineering endpoint; F2a SB1 remains required.",
            "CI must stay green without external CLIs or API keys.",
        ],
        "suite": [],
        "results": [],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "latest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md = RESULTS / "latest.md"
    md.write_text(
        "# Coding H2H (PM 0.13)\n\n"
        f"- skipped: true\n"
        f"- external_claim: forbidden\n"
        f"- reason: {reason}\n"
        f"- clis: `{json.dumps(clis)}`\n\n"
        "Do **not** put exceeds Claude Code / Codex language in README.\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2))
    print(f"Wrote {path}")
    return 0


def _run_ww_task(task_id: str) -> Dict[str, Any]:
    """Best-effort WW closed-book or mock on one task (respects WW_ARENA_LLM)."""
    import importlib.util
    import tempfile

    arena_path = ROOT / "scripts" / "coding_arena.py"
    spec = importlib.util.spec_from_file_location("ww_arena_h2h", arena_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["ww_arena_h2h"] = mod
    spec.loader.exec_module(mod)
    tasks = mod.load_tasks(mod.find_tasks_root(), only=[task_id])
    if not tasks:
        return {"task_id": task_id, "pass": False, "error": "task not found"}
    t = tasks[0]
    td = Path(tempfile.mkdtemp(prefix="ww-h2h-"))
    t0 = time.time()
    try:
        llm = os.environ.get("WW_ARENA_LLM", "0").strip().lower() in ("1", "true", "yes", "on")
        if llm:
            wr = mod.run_ww_llm_agent(t, td)
        else:
            wr = mod.run_ww_mock_agent(t, td)
        return {
            "task_id": task_id,
            "agent": "ww",
            "pass": bool(wr.pass_at_1),
            "wall_s": round(time.time() - t0, 3),
            "mode": wr.mode,
            "gold_applied": wr.gold_applied,
            "rounds": wr.tool_rounds,
        }
    except Exception as e:
        return {
            "task_id": task_id,
            "agent": "ww",
            "pass": False,
            "error": str(e)[:300],
            "wall_s": round(time.time() - t0, 3),
        }
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _run_external_stub(cli_name: str, cli_path: str, task_id: str) -> Dict[str, Any]:
    """Best-effort external CLI invocation.

    Full sandbox parity is not guaranteed: we only probe that the CLI starts
    and exits. Real closed-book external scoring requires operator auth +
    per-product flags documented here as limitations.
    """
    t0 = time.time()
    # Non-destructive version probe only unless WW_H2H_EXTERNAL_RUN=1
    if os.environ.get("WW_H2H_EXTERNAL_RUN", "0").strip() not in ("1", "true", "yes", "on"):
        try:
            proc = subprocess.run(
                [cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return {
                "task_id": task_id,
                "agent": cli_name,
                "pass": None,
                "skipped_task_run": True,
                "version_probe_rc": proc.returncode,
                "version_out": ((proc.stdout or "") + (proc.stderr or ""))[:200],
                "wall_s": round(time.time() - t0, 3),
                "note": "Set WW_H2H_EXTERNAL_RUN=1 to attempt task sandboxes (auth required).",
            }
        except Exception as e:
            return {
                "task_id": task_id,
                "agent": cli_name,
                "pass": False,
                "error": str(e)[:200],
                "wall_s": round(time.time() - t0, 3),
            }
    # Optional full run path (operator-enabled): feed goal only — best effort
    meta = json.loads((TASKS / task_id / "task.json").read_text(encoding="utf-8"))
    goal = meta.get("goal") or task_id
    try:
        proc = subprocess.run(
            [cli_path, "-p", goal[:500]],
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("WW_H2H_TASK_TIMEOUT", "120") or "120"),
            cwd=str(TASKS / task_id / "scaffold"),
        )
        return {
            "task_id": task_id,
            "agent": cli_name,
            "pass": None,  # cannot auto-score without harness adapter
            "rc": proc.returncode,
            "wall_s": round(time.time() - t0, 3),
            "note": "External output not auto-scored against hidden tests in this harness revision.",
        }
    except Exception as e:
        return {
            "task_id": task_id,
            "agent": cli_name,
            "pass": False,
            "error": str(e)[:200],
            "wall_s": round(time.time() - t0, 3),
        }


def run(suite: str = "hard_subset10") -> int:
    clis = _which_cli()
    available = {k: v for k, v in clis.items() if v}
    if not available:
        return _skip_report("no claude/codex on PATH", clis)

    n = 10
    if suite.startswith("hard_subset"):
        try:
            n = int(suite.replace("hard_subset", "") or "10")
        except ValueError:
            n = 10
    task_ids = _hard_subset(max(10, n))
    if len(task_ids) < 10:
        return _skip_report(f"fewer than 10 hard tasks available ({len(task_ids)})", clis)

    print(f"WW Coding H2H — suite={suite} tasks={len(task_ids)} clis={list(available)}")
    ww_results = []
    ext_results = []
    for tid in task_ids:
        print(f"  [WW] {tid}")
        ww_results.append(_run_ww_task(tid))
        for name, path in available.items():
            print(f"  [{name}] {tid}")
            ext_results.append(_run_external_stub(name, path, tid))

    ww_pass = sum(1 for r in ww_results if r.get("pass") is True)
    # External pass only counts when explicitly scored True
    ext_scored = [r for r in ext_results if r.get("pass") is True or r.get("pass") is False]
    claim = "forbidden"
    if ext_scored and all(r.get("pass") is not None for r in ext_scored):
        # Only allow claim language if every external scored and WW >= each
        by_agent: Dict[str, List[bool]] = {}
        for r in ext_scored:
            by_agent.setdefault(r["agent"], []).append(bool(r.get("pass")))
        ww_rate = ww_pass / max(1, len(ww_results))
        ok_all = True
        for agent, passes in by_agent.items():
            if ww_rate < (sum(passes) / max(1, len(passes))):
                ok_all = False
        claim = "allowed" if ok_all else "forbidden"

    # Default: version-probe only → claim remains forbidden
    if any(r.get("skipped_task_run") for r in ext_results):
        claim = "forbidden"

    payload = {
        "skipped": False,
        "external_claim": claim,
        "clis_detected": clis,
        "suite": task_ids,
        "ww_results": ww_results,
        "external_results": ext_results,
        "ww_pass": ww_pass,
        "ww_n": len(ww_results),
        "pm": "0.13.0-endpoint",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "External CLIs differ in tools, auth, and sandbox; auto-scoring is best-effort.",
            "Default mode only probes --version unless WW_H2H_EXTERNAL_RUN=1.",
            "Hidden tests remain arena-side; do not leak to external prompts beyond goal.",
            "README may claim exceeds only when external_claim=allowed after full scored run.",
        ],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "latest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (RESULTS / "latest.md").write_text(
        "# Coding H2H (PM 0.13)\n\n"
        f"- skipped: false\n"
        f"- external_claim: {claim}\n"
        f"- WW pass: {ww_pass}/{len(ww_results)}\n"
        f"- clis: `{json.dumps(clis)}`\n"
        f"- suite: {task_ids}\n",
        encoding="utf-8",
    )
    print(json.dumps({k: payload[k] for k in (
        "skipped", "external_claim", "ww_pass", "ww_n", "clis_detected"
    )}, indent=2))
    print(f"Wrote {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="WW vs claude/codex H2H (F2b)")
    p.add_argument("--suite", default="hard_subset10", help="hard_subset10 (default)")
    args = p.parse_args(argv)
    return run(suite=args.suite)


if __name__ == "__main__":
    sys.exit(main())
