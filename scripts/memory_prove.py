#!/usr/bin/env python3
"""WW Memory prove harness — content-level + mechanism checks.

Exit 0 only if L0+L1+promote pass. L2 (live server) runs when WW_PROVE_URL
and WW_API_KEY are set; failure of L2 fails the run when WW_PROVE_REQUIRE_L2=1
(default 1 if URL set).

Usage:
  python scripts/memory_prove.py
  WW_PROVE_URL=http://127.0.0.1:9302 WW_API_KEY=... python scripts/memory_prove.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    path_label: str = ""  # MemorySystem atoms | working_memory | both | n/a


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", path_label: str = "n/a"):
        self.checks.append(Check(name, ok, detail, path_label))

    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def table(self) -> str:
        lines = ["| Check | Result | Path | Detail |", "|-------|--------|------|--------|"]
        for c in self.checks:
            lines.append(
                f"| {c.name} | {'PASS' if c.ok else 'FAIL'} | {c.path_label or 'n/a'} | {c.detail[:120]} |"
            )
        return "\n".join(lines)


def _count_atoms(hip) -> int:
    return hip._count()


def _core_ids(hip) -> set:
    return {a.atom_id for a in hip.all() if getattr(a, "is_core", False)}


def _archive_lines(data_dir: str) -> int:
    p = Path(data_dir) / "archive.jsonl"
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


def run_l0(report: Report) -> None:
    env = os.environ.copy()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_memory_curation.py",
        "tests/test_memory_recall_sleep.py",
        "tests/test_memory.py",
        "-q",
        "--tb=line",
    ]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, env=env)
    ok = r.returncode == 0 and "passed" in (r.stdout + r.stderr)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    report.add("L0 offline unit suite", ok, " | ".join(tail), "n/a")


def run_l1_capacity(report: Report) -> None:
    from core.memory.atom import MemoryAtom
    from core.memory.hippocampus import Hippocampus

    td = tempfile.mkdtemp(prefix="ww-mem-prove-cap-")
    try:
        hip = Hippocampus(cap=20, protect_threshold=0.8, data_dir=td)
        core_ids = []
        for i in range(3):
            a = MemoryAtom(
                content=f"CORE-PROTECTED-FACT-{i}-UNIQUE",
                importance=0.95,
                is_core=True,
                source="prove",
            )
            hip.store(a)
            core_ids.append(a.atom_id)

        # Fill past cap with junk
        for i in range(40):
            hip.store(
                MemoryAtom(
                    content=f"junk-noise-{i}-" + ("x" * 20),
                    importance=0.05,
                    is_core=False,
                    source="prove",
                )
            )

        remaining_core = _core_ids(hip)
        missing = set(core_ids) - remaining_core
        total = _count_atoms(hip)
        arch = _archive_lines(td)

        # Force path: try force_evict many times — cores must remain
        for _ in range(10):
            hip._force_evict_oldest()
        remaining_core2 = _core_ids(hip)
        missing2 = set(core_ids) - remaining_core2

        ok = (
            not missing
            and not missing2
            and arch > 0
            and total <= hip.cap + 5  # may slightly exceed if only protected
        )
        detail = (
            f"cap={hip.cap} total={total} archive_lines={arch} "
            f"cores_ok={not missing and not missing2} missing={list(missing|missing2)[:3]}"
        )
        report.add("L1 capacity protect core", ok, detail, "MemorySystem atoms")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_l1_gc(report: Report) -> None:
    from core.memory.atom import MemoryAtom
    from core.memory.hippocampus import Hippocampus
    from core.memory.sleep import SleepConsolidation

    td = tempfile.mkdtemp(prefix="ww-mem-prove-gc-")
    try:
        hip = Hippocampus(cap=50, data_dir=td)
        sleep = SleepConsolidation(data_dir=td, gc_salience_threshold=0.1, gc_age_days=30.0)

        old = time.time() - 86400 * 40  # 40 days

        junk = MemoryAtom(
            content="ORPHAN-LOW-SALIENCE-JUNK",
            importance=0.01,
            is_core=False,
            links={},
            timestamp=old,
            source="prove",
        )
        keep_hi = MemoryAtom(
            content="ORPHAN-HIGH-SALIENCE-KEEP",
            importance=0.99,
            is_core=False,
            links={},
            timestamp=old,
            source="prove",
        )
        keep_core = MemoryAtom(
            content="ORPHAN-CORE-KEEP",
            importance=0.01,
            is_core=True,
            links={},
            timestamp=old,
            source="prove",
        )
        hip.store(junk)
        hip.store(keep_hi)
        hip.store(keep_core)

        atoms = hip.all()
        # salience_fn: junk low, others high
        def sal(a):
            if a.atom_id == junk.atom_id:
                return 0.01
            return 0.9

        n = sleep._phase_gc(atoms, hip, salience_fn=sal)
        after = {a.atom_id: a for a in hip.all()}
        arch = _archive_lines(td)

        junk_gone = junk.atom_id not in after
        hi_kept = keep_hi.atom_id in after
        core_kept = keep_core.atom_id in after
        ok = junk_gone and hi_kept and core_kept and n >= 1 and arch >= 1
        detail = (
            f"gc_removed={n} junk_gone={junk_gone} hi_kept={hi_kept} "
            f"core_kept={core_kept} archive={arch}"
        )
        report.add("L1 Phase5 GC rules", ok, detail, "MemorySystem atoms")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_promote(report: Report) -> None:
    from core.memory.atom import MemoryAtom, maybe_promote_core
    from core.memory.hippocampus import Hippocampus

    td = tempfile.mkdtemp(prefix="ww-mem-prove-promo-")
    try:
        hip = Hippocampus(cap=15, protect_threshold=0.8, data_dir=td)
        a = MemoryAtom(
            content="PROMOTE-CANDIDATE-FACT",
            importance=0.9,
            stability=5.0,
            recall_count=10,
            is_core=False,
            source="prove",
        )
        promoted = maybe_promote_core(a, core_count=0, cap=hip.cap)
        hip.store(a)

        # stress with junk
        for i in range(30):
            hip.store(
                MemoryAtom(
                    content=f"promo-junk-{i}",
                    importance=0.05,
                    is_core=False,
                    source="prove",
                )
            )
        for _ in range(5):
            hip._force_evict_oldest()

        still = hip.get(a.atom_id)
        ok = promoted and still is not None and still.is_core
        detail = f"promoted={promoted} survived={still is not None} is_core={getattr(still,'is_core',None)}"
        report.add("Promote + protect under stress", ok, detail, "MemorySystem atoms")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _http_json(method: str, url: str, key: str, body: Optional[dict] = None, timeout: float = 120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())



def run_l2(report: Report) -> None:
    base = os.environ.get("WW_PROVE_URL", "").rstrip("/")
    key = os.environ.get("WW_API_KEY", "")
    if not base or not key:
        report.add(
            "L2 product path (live server)",
            False,
            "skipped: set WW_PROVE_URL and WW_API_KEY",
            "n/a",
        )
        return

    fact = f"PROVE-MEM-F-{int(time.time())}"
    atom_ok = False
    atom_detail = ""
    run_ok = False
    run_detail = ""

    # --- MemorySystem atom path: store + search + recall ---
    try:
        stored = None
        last_err = None
        for _ in range(3):
            try:
                stored = _http_json(
                    "POST",
                    f"{base}/ww/memory",
                    key,
                    {
                        "action": "store",
                        "content": f"The secret prove code is {fact}.",
                        "entities": ["prove"],
                    },
                )
                break
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        if not stored:
            raise last_err or RuntimeError("store failed")

        search = _http_json(
            "POST",
            f"{base}/ww/memory",
            key,
            {"action": "search", "query": fact, "limit": 10},
        )
        search_blob = json.dumps(search)
        search_hit = fact in search_blob

        recall = _http_json(
            "POST",
            f"{base}/ww/memory",
            key,
            {"action": "recall", "query": fact, "limit": 10},
        )
        recall_blob = json.dumps(recall)
        recall_hit = fact in recall_blob
        atom_ok = search_hit or recall_hit
        atom_detail = (
            f"store={stored.get('status')} id={stored.get('memory_id')} "
            f"search={search_hit} recall={recall_hit}"
        )
    except Exception as e:
        atom_detail = f"atom path error: {e}"

    # --- Product dialogue path: plant working_memory (explicit) + /ww/run ---
    # Also exercises agent path; basal-ganglia may block remember tool.
    try:
        # resolve primary entity via identity if available
        who = None
        try:
            who = _http_json("GET", f"{base}/ww/status", key)
        except Exception:
            pass

        # plant via identity state when we can discover entity from prior runs
        # Fall back: /ww/run instruction to use prove_mem_code from known facts after we set it
        # Use status memory or identity list
        entity_id = None
        try:
            # best-effort: call identity resolve is not always HTTP; use run entity_id after a no-op
            noop = _http_json(
                "POST",
                f"{base}/ww/run",
                key,
                {"goal": "Reply with exactly ok", "max_spirals": 1},
                timeout=90,
            )
            entity_id = noop.get("entity_id")
        except Exception:
            entity_id = None

        if entity_id:
            _http_json(
                "POST",
                f"{base}/ww/identity/state/{entity_id}",
                key,
                {"working_memory": {"prove_mem_code": fact}},
            )
            wm_planted = True
        else:
            wm_planted = False

        ask = _http_json(
            "POST",
            f"{base}/ww/run",
            key,
            {
                "goal": (
                    "What is prove_mem_code in known facts / working memory? "
                    "If missing, what is the secret prove code from memory? "
                    "Reply ONLY the code value."
                ),
                "max_spirals": 3,
            },
            timeout=180,
        )
        resp = str(ask.get("response") or "")
        run_ok = fact in resp
        run_detail = (
            f"entity={ask.get('entity_id')} wm_planted={wm_planted} "
            f"response={resp[:100]!r}"
        )
    except Exception as e:
        run_detail = f"run path error: {e}"

    # Label paths honestly (L2b)
    if atom_ok and run_ok:
        path_label = "both (MemorySystem atoms + working_memory/dialogue)"
        ok = True
        detail = f"{atom_detail}; {run_detail}"
    elif atom_ok and not run_ok:
        path_label = "MemorySystem atoms only (dialogue MISS)"
        ok = False  # goal requires /ww/run content
        detail = f"{atom_detail}; {run_detail}"
    elif run_ok and not atom_ok:
        path_label = "working_memory/dialogue only — NOT full memory claim"
        ok = False  # goal: never claim memory works if only working_memory
        detail = f"{atom_detail}; {run_detail}"
    else:
        path_label = "none"
        ok = False
        detail = f"{atom_detail}; {run_detail}"

    report.add("L2 product path live server", ok, detail, path_label)
    report.add(
        "L2b path labeling honesty",
        True,
        f"reported_path={path_label}",
        path_label,
    )


def main() -> int:
    report = Report()
    print("WW Memory prove — starting\n")
    run_l0(report)
    run_l1_capacity(report)
    run_l1_gc(report)
    run_promote(report)

    require_l2 = os.environ.get("WW_PROVE_URL") or os.environ.get("WW_PROVE_REQUIRE_L2")
    if os.environ.get("WW_PROVE_URL"):
        run_l2(report)
    elif os.environ.get("WW_PROVE_REQUIRE_L2", "0") == "1":
        report.add("L2 product path live server", False, "WW_PROVE_REQUIRE_L2=1 but no URL", "n/a")

    print(report.table())
    print()
    failed = [c for c in report.checks if not c.ok]
    # L2b honesty always true; filter required fails
    # If L2 was skipped entirely (no URL), don't fail whole prove unless required
    hard_fails = []
    for c in failed:
        if c.name.startswith("L2") and not os.environ.get("WW_PROVE_URL"):
            continue
        hard_fails.append(c)

    if hard_fails:
        print("RESULT: FAIL")
        for c in hard_fails:
            print(f"  - {c.name}: {c.detail}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
