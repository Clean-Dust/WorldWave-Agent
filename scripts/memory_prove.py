#!/usr/bin/env python3
"""WW Memory prove harness — mechanism + product modes.

Modes:
  --mechanism   L0 unit tests + L1 capacity/GC/promote (isolated data_dir)
  --product     A1–A4 natural write/read on live server (no harness cheating)
  --all         mechanism then product (default if no flags)

Cheat detection for --product:
  Harness MUST NOT call POST /ww/memory store or POST /ww/identity/state/*.
  Only /ww/run for agent write/read, and read-only inspection (search/recall/stats/get).

Exit 0 only if all selected checks pass.
"""
from __future__ import annotations

import argparse
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
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# APIs the product harness is forbidden to use for planting facts
BANNED_WRITE_PATHS = (
    "/ww/memory",  # only when action=store — enforced in post()
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    path_label: str = "n/a"


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", path_label: str = "n/a"):
        self.checks.append(Check(name, ok, detail, path_label))

    def table(self) -> str:
        lines = [
            "| Check | Result | Path | Detail |",
            "|-------|--------|------|--------|",
        ]
        for c in self.checks:
            lines.append(
                f"| {c.name} | {'PASS' if c.ok else 'FAIL'} | {c.path_label} | {c.detail[:140]} |"
            )
        return "\n".join(lines)

    def hard_fail(self) -> bool:
        return any(not c.ok for c in self.checks)


def _archive_lines(data_dir: str) -> int:
    p = Path(data_dir) / "archive.jsonl"
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


def _core_ids(hip) -> set:
    return {a.atom_id for a in hip.all() if getattr(a, "is_core", False)}


def run_l0(report: Report) -> None:
    if os.environ.get("WW_PROVE_SKIP_L0") == "1":
        report.add("L0 offline unit suite", True, "skipped via WW_PROVE_SKIP_L0=1")
        return
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
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    ok = r.returncode == 0 and "passed" in (r.stdout + r.stderr)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    report.add("L0 offline unit suite", ok, " | ".join(tail))


def run_b1_capacity(report: Report) -> None:
    from core.memory.atom import MemoryAtom
    from core.memory.hippocampus import Hippocampus

    td = tempfile.mkdtemp(prefix="ww-mem-b1-")
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
        for i in range(40):
            hip.store(
                MemoryAtom(
                    content=f"junk-noise-{i}-" + ("x" * 20),
                    importance=0.05,
                    is_core=False,
                    source="prove",
                )
            )
        for _ in range(10):
            hip._force_evict_oldest()
        missing = set(core_ids) - _core_ids(hip)
        arch = _archive_lines(td)
        ok = not missing and arch > 0
        report.add(
            "B1 protect under capacity",
            ok,
            f"missing={list(missing)[:3]} archive={arch} total={hip._count()}",
            "MemorySystem atoms",
        )
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_b2_gc(report: Report) -> None:
    from core.memory.atom import MemoryAtom
    from core.memory.hippocampus import Hippocampus
    from core.memory.sleep import SleepConsolidation

    td = tempfile.mkdtemp(prefix="ww-mem-b2-")
    try:
        hip = Hippocampus(cap=50, data_dir=td)
        sleep = SleepConsolidation(
            data_dir=td, gc_salience_threshold=0.1, gc_age_days=30.0
        )
        old = time.time() - 86400 * 40
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

        def sal(a):
            return 0.01 if a.atom_id == junk.atom_id else 0.9

        n = sleep._phase_gc(hip.all(), hip, salience_fn=sal)
        after = {a.atom_id for a in hip.all()}
        ok = (
            junk.atom_id not in after
            and keep_hi.atom_id in after
            and keep_core.atom_id in after
            and n >= 1
            and _archive_lines(td) >= 1
        )
        report.add(
            "B2 Phase5 GC rules",
            ok,
            f"gc_removed={n} junk_gone={junk.atom_id not in after} "
            f"hi_kept={keep_hi.atom_id in after} core_kept={keep_core.atom_id in after}",
            "MemorySystem atoms",
        )
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_promote(report: Report) -> None:
    from core.memory.atom import MemoryAtom, maybe_promote_core
    from core.memory.hippocampus import Hippocampus

    td = tempfile.mkdtemp(prefix="ww-mem-promo-")
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
        report.add(
            "Promote + protect under stress",
            ok,
            f"promoted={promoted} survived={still is not None}",
            "MemorySystem atoms",
        )
    finally:
        shutil.rmtree(td, ignore_errors=True)


class LiveClient:
    """HTTP client with cheat detection for product mode."""

    def __init__(self, base: str, key: str, product_mode: bool = False):
        self.base = base.rstrip("/")
        self.key = key
        self.product_mode = product_mode
        self.cheat_log: List[str] = []

    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        timeout: float = 180,
    ) -> Any:
        if self.product_mode:
            self._check_cheat(method, path, body)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-API-Key": self.key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:500]
            raise RuntimeError(f"HTTP {e.code} {path}: {err_body}") from e

    def _check_cheat(self, method: str, path: str, body: Optional[dict]) -> None:
        if method.upper() != "POST":
            return
        # Forbidden: plant via identity state
        if path.startswith("/ww/identity/state"):
            msg = f"CHEAT: POST {path} forbidden in product mode"
            self.cheat_log.append(msg)
            raise RuntimeError(msg)
        # Forbidden: direct memory store
        if path.rstrip("/").endswith("/ww/memory") or path == "/ww/memory":
            action = (body or {}).get("action", "recall")
            if action == "store":
                msg = "CHEAT: POST /ww/memory action=store forbidden in product mode"
                self.cheat_log.append(msg)
                raise RuntimeError(msg)


def run_product(report: Report) -> None:
    base = os.environ.get("WW_PROVE_URL", "").rstrip("/")
    key = os.environ.get("WW_API_KEY", "")
    if not base or not key:
        report.add(
            "A1–A4 product path",
            False,
            "set WW_PROVE_URL and WW_API_KEY",
            "n/a",
        )
        return

    client = LiveClient(base, key, product_mode=True)
    key_name = "prove_product_code"
    value = f"PROD-MEM-{int(time.time())}"

    # --- A1 Natural write via /ww/run only ---
    a1_ok = False
    a1_detail = ""
    blocked_explicit = False
    try:
        plant = client.request(
            "POST",
            "/ww/run",
            {
                "goal": (
                    f"You MUST call the remember tool now: "
                    f"key={key_name} value={value}. "
                    f"Do not skip the tool. After the tool returns success, "
                    f"reply with exactly REMEMBERED:{value}"
                ),
                "max_spirals": 5,
            },
            timeout=240,
        )
        plant_resp = str(plant.get("response") or "")
        plant_lower = plant_resp.lower()
        if "basal" in plant_lower or "blocked" in plant_lower or "n-score" in plant_lower:
            blocked_explicit = True
            # A3: one alternate path retry
            plant2 = client.request(
                "POST",
                "/ww/run",
                {
                    "goal": (
                        f"Previous remember may have been blocked. "
                        f"Try remember again with key={key_name} value={value}. "
                        f"If tools are blocked, say BLOCKED clearly. "
                        f"If stored, reply REMEMBERED:{value}"
                    ),
                    "max_spirals": 5,
                },
                timeout=240,
            )
            plant_resp = str(plant2.get("response") or "")
            plant_lower = plant_resp.lower()
            if "basal" in plant_lower or "blocked" in plant_lower:
                blocked_explicit = True

        # Read-only inspect: WM via status? Use recall_mine through run is write-ish.
        # Inspect atoms via search (read-only for product if not store)
        search = client.request(
            "POST",
            "/ww/memory",
            {"action": "search", "query": value, "limit": 10},
        )
        atom_hit = value in json.dumps(search)

        # Inspect entity working memory via a read-only run that only recalls
        # We cannot POST identity state; use /ww/run recall question after A1
        # For A1 storage proof: atom_hit OR ask later finds it (A2)
        # Immediate WM check: ask once
        probe = client.request(
            "POST",
            "/ww/run",
            {
                "goal": (
                    f"What is {key_name}? Reply ONLY the value, or UNKNOWN."
                ),
                "max_spirals": 3,
            },
            timeout=180,
        )
        probe_resp = str(probe.get("response") or "")
        wm_or_ctx_hit = value in probe_resp

        stored_somewhere = atom_hit or wm_or_ctx_hit
        silent_fail = (
            not stored_somewhere
            and not blocked_explicit
            and "remembered" in plant_lower
        )

        a1_ok = stored_somewhere and not silent_fail
        a1_detail = (
            f"plant={plant_resp[:80]!r} atom_hit={atom_hit} "
            f"probe_hit={wm_or_ctx_hit} blocked_explicit={blocked_explicit} "
            f"entity={plant.get('entity_id')}"
        )
        report.add(
            "A1 natural write (tools only)",
            a1_ok,
            a1_detail,
            "agent tools" if a1_ok else "none",
        )
    except Exception as e:
        report.add("A1 natural write (tools only)", False, f"error: {e}")
        report.add("A2 natural read", False, "skipped: A1 failed")
        report.add("A3 no silent BG fail", False, f"error: {e}")
        report.add("A4 atom path real", False, "skipped: A1 failed")
        if client.cheat_log:
            report.add("C cheat detection", False, "; ".join(client.cheat_log))
        else:
            report.add("C cheat detection", True, "no banned write APIs used")
        return

    # --- A3 ---
    a3_ok = not (
        not (atom_hit if "atom_hit" in dir() else False)
        and not (wm_or_ctx_hit if "wm_or_ctx_hit" in dir() else False)
        and not blocked_explicit
        and "remembered" in plant_lower
    )
    # clearer:
    a3_ok = True
    if not a1_ok and not blocked_explicit:
        # failed without explicit block message
        if "remembered" in plant_lower or plant_resp.strip() == "":
            a3_ok = False
    if not a1_ok and blocked_explicit:
        a3_ok = True  # explicit block is OK for A3 (not silent); A1 still fails
    report.add(
        "A3 no basal-ganglia silent fail",
        a3_ok,
        f"blocked_explicit={blocked_explicit} a1_ok={a1_ok} plant={plant_resp[:60]!r}",
    )

    # --- A2 Natural read (new request, no re-plant) ---
    try:
        ask = client.request(
            "POST",
            "/ww/run",
            {
                "goal": (
                    f"What is {key_name} in known facts or working memory? "
                    f"Reply ONLY the value."
                ),
                "max_spirals": 3,
            },
            timeout=180,
        )
        ask_resp = str(ask.get("response") or "")
        a2_ok = value in ask_resp
        report.add(
            "A2 natural read (new request)",
            a2_ok,
            f"response={ask_resp[:100]!r} entity={ask.get('entity_id')}",
            "dialogue" if a2_ok else "none",
        )
    except Exception as e:
        report.add("A2 natural read (new request)", False, f"error: {e}")
        a2_ok = False

    # --- A4 Atom path must contain V ---
    try:
        search2 = client.request(
            "POST",
            "/ww/memory",
            {"action": "search", "query": value, "limit": 10},
        )
        recall2 = client.request(
            "POST",
            "/ww/memory",
            {"action": "recall", "query": value, "limit": 10},
        )
        atom_ok = value in json.dumps(search2) or value in json.dumps(recall2)
        path_label = (
            "both"
            if atom_ok and a2_ok
            else ("MemorySystem atoms" if atom_ok else ("WM-only FAIL" if a2_ok else "none"))
        )
        # default FAIL if only WM
        a4_ok = atom_ok
        report.add(
            "A4 atom path real",
            a4_ok,
            f"search/recall hit={atom_ok} (WM-only is FAIL by default)",
            path_label,
        )
    except Exception as e:
        report.add("A4 atom path real", False, f"error: {e}")

    report.add(
        "C cheat detection",
        len(client.cheat_log) == 0,
        "clean" if not client.cheat_log else "; ".join(client.cheat_log),
    )


def run_mechanism(report: Report) -> None:
    run_l0(report)
    run_b1_capacity(report)
    run_b2_gc(report)
    run_promote(report)


def _live_client() -> LiveClient:
    base = os.environ.get("WW_PROVE_URL", "").rstrip("/")
    key = os.environ.get("WW_API_KEY", "")
    if not base or not key:
        raise RuntimeError("set WW_PROVE_URL and WW_API_KEY")
    return LiveClient(base, key, product_mode=True)


def run_restart(report: Report) -> None:
    """Product write → restart ww.service → natural read (no re-plant)."""
    if os.environ.get("WW_PROVE_ALLOW_RESTART") != "1":
        report.add(
            "R1 restart persistence",
            False,
            "set WW_PROVE_ALLOW_RESTART=1 to enable (restarts ww.service)",
            "n/a",
        )
        return
    try:
        client = _live_client()
    except Exception as e:
        report.add("R1 restart persistence", False, str(e))
        return

    key_name = "prove_restart_code"
    value = f"RESTART-MEM-{int(time.time())}"
    try:
        plant = client.request(
            "POST",
            "/ww/run",
            {
                "goal": (
                    f"Call remember tool: key={key_name} value={value}. "
                    f"Reply REMEMBERED:{value} after success."
                ),
                "max_spirals": 5,
            },
            timeout=240,
        )
        plant_ok = value in str(plant.get("response") or "")
        # restart service
        r = subprocess.run(
            ["systemctl", "--user", "restart", "ww.service"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            report.add(
                "R1 restart persistence",
                False,
                f"systemctl restart failed: {r.stderr[:120]}",
            )
            return
        # wait health
        base = os.environ["WW_PROVE_URL"].rstrip("/")
        key = os.environ["WW_API_KEY"]
        healthy = False
        for _ in range(30):
            try:
                req = urllib.request.Request(
                    base + "/ww/health",
                    headers={"X-API-Key": key},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        healthy = True
                        break
            except Exception:
                time.sleep(1)
        if not healthy:
            report.add("R1 restart persistence", False, "service not healthy after restart")
            return
        # re-create client (same URL)
        client2 = LiveClient(base, key, product_mode=True)
        ask = client2.request(
            "POST",
            "/ww/run",
            {
                "goal": f"What is {key_name}? Reply ONLY the value.",
                "max_spirals": 3,
            },
            timeout=180,
        )
        resp = str(ask.get("response") or "")
        ok = plant_ok and value in resp
        report.add(
            "R1 restart persistence",
            ok,
            f"plant_ok={plant_ok} after_restart={resp[:80]!r}",
            "dialogue+service",
        )
        # atoms after restart
        search = client2.request(
            "POST",
            "/ww/memory",
            {"action": "search", "query": value, "limit": 5},
        )
        report.add(
            "R1 atom survives restart",
            value in json.dumps(search),
            f"search_hit={value in json.dumps(search)}",
            "MemorySystem atoms",
        )
    except Exception as e:
        report.add("R1 restart persistence", False, f"error: {e}")


def run_narrative(report: Report) -> None:
    """Multi-turn script: set preference, distract, recall."""
    try:
        client = _live_client()
    except Exception as e:
        report.add("N1 multi-turn narrative", False, str(e))
        return

    fav = f"NARR-{int(time.time()) % 100000}"
    try:
        t1 = client.request(
            "POST",
            "/ww/run",
            {
                "goal": (
                    f"Remember my favorite color code is {fav} using the remember tool "
                    f"with key=favorite_color_code. Confirm with REMEMBERED:{fav}."
                ),
                "max_spirals": 5,
            },
            timeout=240,
        )
        t2 = client.request(
            "POST",
            "/ww/run",
            {
                "goal": "What is 17*19? Reply with only the number.",
                "max_spirals": 2,
            },
            timeout=120,
        )
        t3 = client.request(
            "POST",
            "/ww/run",
            {
                "goal": (
                    "What is my favorite_color_code? Reply ONLY the code value."
                ),
                "max_spirals": 3,
            },
            timeout=180,
        )
        r1 = str(t1.get("response") or "")
        r3 = str(t3.get("response") or "")
        ok = fav in r1 and fav in r3
        report.add(
            "N1 multi-turn narrative",
            ok,
            f"t1={r1[:50]!r} t2={str(t2.get('response'))[:30]!r} t3={r3[:50]!r}",
            "dialogue multi-turn",
        )
        search = client.request(
            "POST",
            "/ww/memory",
            {"action": "search", "query": fav, "limit": 5},
        )
        report.add(
            "N1 narrative atom present",
            fav in json.dumps(search),
            f"atom_hit={fav in json.dumps(search)}",
            "MemorySystem atoms",
        )
    except Exception as e:
        report.add("N1 multi-turn narrative", False, f"error: {e}")


def run_telegram_channel(report: Report) -> None:
    """Simulate Telegram owner channel via /ww/run platform=telegram (same identity path)."""
    owner = os.environ.get("WW_OWNER_TELEGRAM_ID", "").strip()
    if not owner:
        report.add(
            "T1 telegram identity path",
            False,
            "set WW_OWNER_TELEGRAM_ID for telegram channel prove",
            "n/a",
        )
        return
    try:
        client = _live_client()
    except Exception as e:
        report.add("T1 telegram identity path", False, str(e))
        return

    key_name = "prove_tg_code"
    value = f"TG-MEM-{int(time.time())}"
    try:
        plant = client.request(
            "POST",
            "/ww/run",
            {
                "goal": (
                    f"Call remember: key={key_name} value={value}. "
                    f"Reply REMEMBERED:{value}."
                ),
                "max_spirals": 5,
                "platform": "telegram",
                "user_id": owner,
                "chat_id": owner,
            },
            timeout=240,
        )
        ask = client.request(
            "POST",
            "/ww/run",
            {
                "goal": f"What is {key_name}? Reply ONLY the value.",
                "max_spirals": 3,
                "platform": "telegram",
                "user_id": owner,
                "chat_id": owner,
            },
            timeout=180,
        )
        # cross-check via http primary (same owner entity under single-user)
        ask_http = client.request(
            "POST",
            "/ww/run",
            {
                "goal": f"What is {key_name}? Reply ONLY the value.",
                "max_spirals": 3,
                "platform": "http",
            },
            timeout=180,
        )
        pr = str(plant.get("response") or "")
        ar = str(ask.get("response") or "")
        hr = str(ask_http.get("response") or "")
        ok_tg = value in pr and value in ar
        ok_cross = value in hr
        report.add(
            "T1 telegram identity path",
            ok_tg,
            f"plant={pr[:40]!r} ask={ar[:40]!r} entity={ask.get('entity_id')}",
            "telegram platform",
        )
        report.add(
            "T1 telegram→http same timeline",
            ok_cross,
            f"http_ask={hr[:40]!r}",
            "cross-entry",
        )
        search = client.request(
            "POST",
            "/ww/memory",
            {"action": "search", "query": value, "limit": 5},
        )
        report.add(
            "T1 telegram atom present",
            value in json.dumps(search),
            f"atom_hit={value in json.dumps(search)}",
            "MemorySystem atoms",
        )
    except Exception as e:
        report.add("T1 telegram identity path", False, f"error: {e}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="WW Memory prove harness")
    ap.add_argument("--mechanism", action="store_true", help="L0 + B1 + B2 + promote")
    ap.add_argument("--product", action="store_true", help="A1–A4 live product path")
    ap.add_argument("--restart", action="store_true", help="write → restart service → read")
    ap.add_argument("--narrative", action="store_true", help="multi-turn distraction recall")
    ap.add_argument("--telegram", action="store_true", help="telegram platform identity path")
    ap.add_argument(
        "--all",
        action="store_true",
        help="mechanism + product (+ restart/narrative/telegram if env allows)",
    )
    ap.add_argument(
        "--auto",
        action="store_true",
        help="all automated suites: mechanism, product, narrative, telegram; restart if ALLOW",
    )
    args = ap.parse_args(argv)

    if args.auto:
        args.mechanism = True
        args.product = True
        args.narrative = True
        args.telegram = True
        args.restart = True
    elif args.all:
        args.mechanism = True
        args.product = True
    elif not any(
        [
            args.mechanism,
            args.product,
            args.restart,
            args.narrative,
            args.telegram,
        ]
    ):
        args.mechanism = True
        args.product = True

    report = Report()
    print("WW Memory prove — starting\n")
    if args.mechanism:
        run_mechanism(report)
    if args.product:
        run_product(report)
    if args.narrative:
        run_narrative(report)
    if args.telegram:
        run_telegram_channel(report)
    if args.restart:
        run_restart(report)

    print(report.table())
    print()
    if report.hard_fail():
        print("RESULT: FAIL")
        for c in report.checks:
            if not c.ok:
                print(f"  - {c.name}: {c.detail}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
