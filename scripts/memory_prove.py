#!/usr/bin/env python3
"""WW Memory prove harness — mechanism + product modes.

Modes:
  --mechanism   L0 + L1 capacity/GC/promote + B3–B7 WM + B-topic/summary/atom/hippo/ltm/dream
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
        "tests/test_working_memory.py",
        "tests/test_memory_vnext.py",
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


def run_b3_working_memory(report: Report) -> None:
    """B3: isolated EntityStateManager capacity eviction (no LLM)."""
    from unittest.mock import MagicMock

    from core.entity_state import EntityStateManager

    td = tempfile.mkdtemp(prefix="ww-mem-b3-")
    prev_cap = os.environ.get("WW_WORKING_MEMORY_CAPACITY")
    try:
        os.environ["WW_WORKING_MEMORY_CAPACITY"] = "3"
        cfg = MagicMock()
        cfg.get = MagicMock(return_value=None)
        cfg.expand_path = MagicMock(side_effect=lambda p: os.path.expanduser(p))
        esm = EntityStateManager(config=cfg, data_dir=td)
        assert esm.working_memory_capacity == 3

        promoted: List[tuple] = []
        esm.set_on_wm_evict(lambda e, k, v: promoted.append((e, k, v)))
        esm._promote_min_access = 2

        eid = "ent_b3"
        esm.set_working_memory(eid, "a", "va")
        time.sleep(0.01)
        esm.set_working_memory(eid, "b", "vb")
        time.sleep(0.01)
        esm.set_working_memory(eid, "c", "vc")
        # Hot-access "b" so cold "a" is preferred victim
        st = esm.get(eid)
        for _ in range(4):
            st.bump_wm_access(["b"])
        esm.save(st)
        time.sleep(0.01)
        esm.set_working_memory(eid, "d", "vd")  # capacity+1

        st = esm.get(eid)
        size_ok = len(st.working_memory) == 3
        hot_kept = "b" in st.working_memory
        cold_gone = "a" not in st.working_memory
        archive = Path(td) / eid / "wm_evicted.jsonl"
        arch_ok = archive.exists() and any(
            "a" in line for line in archive.read_text(encoding="utf-8").splitlines()
        )

        # Force-promote path on a fresh entity (high-access key vs core slot)
        esm.working_memory_capacity = 1
        eid2 = "ent_b3_promo"
        esm.set_working_memory(eid2, "promo_key", "promo_val")
        st2 = esm.get(eid2)
        for _ in range(3):
            st2.bump_wm_access(["promo_key"])
        esm.save(st2)
        esm.set_working_memory(eid2, "core_only", "x", is_core=True)
        promo_ok = any(p[1] == "promo_key" for p in promoted)
        st2 = esm.get(eid2)

        ok = size_ok and hot_kept and cold_gone and arch_ok and promo_ok
        report.add(
            "B3 entity working memory capacity",
            ok,
            f"size_ok={size_ok} hot_kept={hot_kept} cold_gone={cold_gone} "
            f"arch={arch_ok} promo={promo_ok} wm_evicted={st.wm_evicted_total}+{st2.wm_evicted_total}",
            "EntityStateManager WM",
        )
    except Exception as e:
        report.add(
            "B3 entity working memory capacity",
            False,
            f"error: {e}",
            "EntityStateManager WM",
        )
    finally:
        if prev_cap is None:
            os.environ.pop("WW_WORKING_MEMORY_CAPACITY", None)
        else:
            os.environ["WW_WORKING_MEMORY_CAPACITY"] = prev_cap
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


def run_b4_wm_kind_eviction(report: Report) -> None:
    """B4: WM kind-weighted eviction on single-system LabeledFactStore (no LLM)."""
    from core.entity_state import normalize_wm_kind, wm_eviction_score
    from core.memory.labeled_wm import LabeledFactStore

    td = tempfile.mkdtemp(prefix="ww-mem-b4-")
    prev_cap = os.environ.get("WW_WORKING_MEMORY_CAPACITY")
    try:
        os.environ["WW_WORKING_MEMORY_CAPACITY"] = "2"
        store = LabeledFactStore(data_dir=td, capacity=2)

        eid = "ent_b4"
        store.set(eid, "dec", "do A", kind="commitment")
        time.sleep(0.01)
        store.set(eid, "why", "because B", kind="rationale")
        time.sleep(0.01)
        store.set(eid, "new", "incoming", kind="outcome")

        facts = store.get_facts(eid)
        size_ok = len(facts) == 2
        cmt_kept = "dec" in facts
        rat_gone = "why" not in facts
        new_kept = "new" in facts

        score_ok = wm_eviction_score("commitment", 0) > wm_eviction_score(
            "rationale", 0
        )
        norm_ok = normalize_wm_kind("nope") == "outcome"
        legacy_ok = wm_eviction_score(None, 1) == wm_eviction_score("outcome", 1)

        meta_kind = (store.get_meta(eid).get("dec") or {}).get("kind")
        meta_ok = meta_kind == "commitment"

        ok = (
            size_ok
            and cmt_kept
            and rat_gone
            and new_kept
            and score_ok
            and norm_ok
            and legacy_ok
            and meta_ok
        )
        report.add(
            "B4 WM kind-weighted eviction",
            ok,
            f"size={size_ok} cmt={cmt_kept} rat_gone={rat_gone} new={new_kept} "
            f"score={score_ok} norm={norm_ok} legacy={legacy_ok} meta={meta_ok}",
            "LabeledFactStore / v-next",
        )
    except Exception as e:
        report.add(
            "B4 WM kind-weighted eviction",
            False,
            f"error: {e}",
            "LabeledFactStore / v-next",
        )
    finally:
        if prev_cap is None:
            os.environ.pop("WW_WORKING_MEMORY_CAPACITY", None)
        else:
            os.environ["WW_WORKING_MEMORY_CAPACITY"] = prev_cap
        shutil.rmtree(td, ignore_errors=True)


def run_b5_wm_tiebreak_switch(report: Report) -> None:
    """B5: optional WM subconscious tie-break on LabeledFactStore (single system)."""
    from core.memory.labeled_wm import LabeledFactStore

    td = tempfile.mkdtemp(prefix="ww-mem-b5-")
    try:
        store = LabeledFactStore(data_dir=td, capacity=1)

        # --- OFF (no hook): same kind/access → oldest updated_at loses ---
        store.set_tiebreak_fn(None)
        eid_off = "ent_b5_off"
        store.set(eid_off, "old", "v-old", kind="outcome")
        time.sleep(0.02)
        store.set(eid_off, "new", "v-new", kind="outcome")
        facts_off = store.get_facts(eid_off)
        off_ok = (
            len(facts_off) == 1
            and "new" in facts_off
            and "old" not in facts_off
            and store._tiebreak_fn is None
        )
        report.add(
            "B5 WM tiebreak off (= kind/access/updated only)",
            off_ok,
            f"keys={list(facts_off.keys())} hook={store._tiebreak_fn}",
            "LabeledFactStore / v-next",
        )

        # --- ON: same kind/access/age; higher protect stays ---
        protect = {"keep": 9.0, "drop": 0.0}

        def tb(entity_id, key, meta):
            return float(protect.get(key, 0.0))

        store.set_tiebreak_fn(tb)
        eid_on = "ent_b5_on"
        store.capacity = 2
        store.set(eid_on, "keep", "v-keep", kind="outcome")
        store.set(eid_on, "drop", "v-drop", kind="outcome")
        t0 = 1_700_000_000.0
        with store._lock:
            for k in ("keep", "drop"):
                store._meta[eid_on][k]["updated_at"] = t0
                store._meta[eid_on][k]["access_count"] = 0
            store._save(eid_on)
        store.capacity = 1
        store.enforce_capacity(eid_on, now=t0 + 5.0)
        facts_on = store.get_facts(eid_on)
        on_ok = (
            "keep" in facts_on
            and "drop" not in facts_on
            and store._tiebreak_fn is not None
        )
        report.add(
            "B5 WM tiebreak on (numeric protect breaks ties)",
            on_ok,
            f"keys={list(facts_on.keys())}",
            "LabeledFactStore / v-next",
        )

        # --- ON still cannot override commitment > rationale ---
        store.set_tiebreak_fn(lambda e, k, m: 1e9 if k == "rat" else 0.0)
        store.capacity = 1
        eid_kind = "ent_b5_kind"
        store.set(eid_kind, "cmt", "decide", kind="commitment")
        time.sleep(0.01)
        store.set(eid_kind, "rat", "process", kind="rationale")
        facts_k = store.get_facts(eid_kind)
        kind_ok = "cmt" in facts_k and "rat" not in facts_k
        report.add(
            "B5 WM tiebreak cannot override commitment>rationale",
            kind_ok,
            f"keys={list(facts_k.keys())}",
            "LabeledFactStore / v-next",
        )
    except Exception as e:
        report.add(
            "B5 WM tiebreak switch",
            False,
            f"error: {e}",
            "LabeledFactStore / v-next",
        )
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_b6_wm_recency(report: Report) -> None:
    """B6: WM recency decay on LabeledFactStore (single system; fixed clock)."""
    from core.entity_state import ROLE_WEIGHT, wm_eviction_score, wm_recency_factor
    from core.memory.labeled_wm import LabeledFactStore

    td = tempfile.mkdtemp(prefix="ww-mem-b6-")
    prev = {
        k: os.environ.get(k)
        for k in (
            "WW_WM_RECENCY_ENABLED",
            "WW_WM_RECENCY_HALF_LIFE_S",
            "WW_WM_RECENCY_FLOOR",
            "WW_WORKING_MEMORY_CAPACITY",
        )
    }
    try:
        os.environ["WW_WM_RECENCY_HALF_LIFE_S"] = "3600"
        os.environ["WW_WM_RECENCY_FLOOR"] = "0.4"
        os.environ["WW_WORKING_MEMORY_CAPACITY"] = "2"

        store = LabeledFactStore(data_dir=td, capacity=2)

        s_new = wm_eviction_score(
            "outcome", 0, age_seconds=0.0, recency_enabled=True
        )
        s_old = wm_eviction_score(
            "outcome", 0, age_seconds=3600.0, recency_enabled=True
        )
        factor_ok = (
            s_new > s_old
            and abs(s_new - ROLE_WEIGHT["outcome"] * 1.0) < 1e-9
            and abs(
                s_old
                - ROLE_WEIGHT["outcome"]
                * wm_recency_factor(3600.0, half_life_s=3600.0, floor=0.4)
            )
            < 1e-9
        )

        s_cmt = wm_eviction_score(
            "commitment",
            5,
            age_seconds=10 * 3600.0,
            recency_enabled=True,
            half_life_s=3600.0,
            floor=0.4,
        )
        s_rat = wm_eviction_score(
            "rationale",
            0,
            age_seconds=0.0,
            recency_enabled=True,
            half_life_s=3600.0,
            floor=0.4,
        )
        role_ok = s_cmt > s_rat

        os.environ["WW_WM_RECENCY_ENABLED"] = "1"
        eid_on = "ent_b6_on"
        store.set(eid_on, "fresh", "new-fact", kind="outcome")
        store.set(eid_on, "stale", "old-fact", kind="outcome")
        now = 1_700_000_000.0
        with store._lock:
            store._meta[eid_on]["fresh"]["updated_at"] = now
            store._meta[eid_on]["fresh"]["access_count"] = 0
            store._meta[eid_on]["stale"]["updated_at"] = now - 7200.0
            store._meta[eid_on]["stale"]["access_count"] = 0
            store._save(eid_on)
        store.capacity = 1
        store.enforce_capacity(eid_on, now=now)
        facts_on = store.get_facts(eid_on)
        on_ok = "fresh" in facts_on and "stale" not in facts_on

        os.environ["WW_WM_RECENCY_ENABLED"] = "0"
        store.capacity = 2
        eid_off = "ent_b6_off"
        store.set(eid_off, "a", "va", kind="outcome")
        store.set(eid_off, "b", "vb", kind="outcome")
        t0 = 1_700_000_100.0
        with store._lock:
            store._meta[eid_off]["a"]["updated_at"] = t0
            store._meta[eid_off]["a"]["access_count"] = 0
            store._meta[eid_off]["b"]["updated_at"] = t0 + 50.0
            store._meta[eid_off]["b"]["access_count"] = 0
            store._save(eid_off)
        base_eq = abs(
            wm_eviction_score("outcome", 0, age_seconds=100.0, recency_enabled=False)
            - wm_eviction_score("outcome", 0, age_seconds=0.0, recency_enabled=False)
        ) < 1e-12
        store.capacity = 1
        store.enforce_capacity(eid_off, now=t0 + 100.0)
        facts_off = store.get_facts(eid_off)
        off_ok = base_eq and "b" in facts_off and "a" not in facts_off

        import core.memory.labeled_wm as lwm_mod

        no_pin = "protect_last_n(" not in Path(lwm_mod.__file__).read_text(
            encoding="utf-8"
        )

        ok = factor_ok and role_ok and on_ok and off_ok and no_pin
        report.add(
            "B6 WM recency decay (on vs off, fixed clock)",
            ok,
            f"factor={factor_ok} role={role_ok} on={on_ok} off={off_ok} "
            f"no_pin={no_pin} s_new={s_new:.4f} s_old={s_old:.4f} "
            f"s_cmt={s_cmt:.4f} s_rat={s_rat:.4f}",
            "LabeledFactStore / v-next",
        )
    except Exception as e:
        report.add(
            "B6 WM recency decay (on vs off, fixed clock)",
            False,
            f"error: {e}",
            "LabeledFactStore / v-next",
        )
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(td, ignore_errors=True)


def run_b7_wm_label_order(report: Report) -> None:
    """B7: four labels on MemoryVNext/LabeledFactStore — single system inject."""
    from core.entity_state import (
        ROLE_WEIGHT,
        WM_KINDS,
        normalize_wm_kind,
        wm_eviction_score,
        wm_label_zh,
    )
    from core.memory.vnext import MemoryVNext

    td = tempfile.mkdtemp(prefix="ww-mem-b7-")
    prev = {
        k: os.environ.get(k)
        for k in ("WW_WORKING_MEMORY_CAPACITY", "WW_WM_RECENCY_ENABLED")
    }
    try:
        os.environ["WW_WORKING_MEMORY_CAPACITY"] = "3"
        os.environ["WW_WM_RECENCY_ENABLED"] = "0"  # isolate label weights

        kinds_ok = WM_KINDS == frozenset(
            {"constraint", "commitment", "outcome", "rationale"}
        )
        weight_ok = (
            ROLE_WEIGHT["constraint"]
            > ROLE_WEIGHT["commitment"]
            > ROLE_WEIGHT["outcome"]
            > ROLE_WEIGHT["rationale"]
            and abs(ROLE_WEIGHT["constraint"] - 4.0) < 1e-9
            and abs(ROLE_WEIGHT["commitment"] - 3.0) < 1e-9
            and abs(ROLE_WEIGHT["outcome"] - 2.0) < 1e-9
            and abs(ROLE_WEIGHT["rationale"] - 1.0) < 1e-9
        )
        score_ok = (
            wm_eviction_score("constraint", 0)
            > wm_eviction_score("commitment", 0)
            > wm_eviction_score("outcome", 0)
            > wm_eviction_score("rationale", 0)
        )
        norm_ok = (
            normalize_wm_kind("constraint") == "constraint"
            and normalize_wm_kind("nope") == "outcome"
        )
        zh_ok = (
            wm_label_zh("constraint") == "约束"
            and wm_label_zh("commitment") == "承诺"
            and wm_label_zh("outcome") == "结果"
            and wm_label_zh("rationale") == "理由"
        )

        mv = MemoryVNext(data_dir=td, start_dreaming=False)
        try:
            mv.facts.capacity = 3
            eid = "ent_b7"
            mv.set_entity(eid)
            mv.remember("rule", "never change netplan", kind="constraint", entity_id=eid)
            mv.remember("plan", "use docker", kind="commitment", entity_id=eid)
            mv.remember("fact", "tests passed", kind="outcome", entity_id=eid)
            mv.remember("why", "chose flash", kind="rationale", entity_id=eid)
            facts = mv.facts.get_facts(eid)
            rat_gone = "why" not in facts
            others_kept = (
                "rule" in facts
                and "plan" in facts
                and "fact" in facts
                and len(facts) == 3
            )

            mv.facts.capacity = 1
            eid2 = "ent_b7_c"
            mv.remember("cmt", "next step", kind="commitment", entity_id=eid2)
            mv.remember("rule2", "no sudo", kind="constraint", entity_id=eid2)
            facts2 = mv.facts.get_facts(eid2)
            c_beats_cmt = "rule2" in facts2 and "cmt" not in facts2

            eid3 = "ent_b7_inj"
            mv.facts.capacity = 4
            mv.remember(
                "no_netplan",
                "never change netplan",
                kind="constraint",
                entity_id=eid3,
            )
            inj = mv.inject_for_turn(entity_id=eid3)
            inject_ok = (
                "- [约束] no_netplan: never change netplan" in inj
                and "[constraint]" not in inj
            )

            ok = (
                kinds_ok
                and weight_ok
                and score_ok
                and norm_ok
                and zh_ok
                and rat_gone
                and others_kept
                and c_beats_cmt
                and inject_ok
            )
            report.add(
                "B7 WM label order constraint>commitment>outcome>rationale",
                ok,
                f"kinds={kinds_ok} weight={weight_ok} score={score_ok} norm={norm_ok} "
                f"zh={zh_ok} rat_gone={rat_gone} kept={others_kept} c>cmt={c_beats_cmt} "
                f"inject={inject_ok}",
                "MemoryVNext labeled facts",
            )
        finally:
            mv.close()
    except Exception as e:
        report.add(
            "B7 WM label order constraint>commitment>outcome>rationale",
            False,
            f"error: {e}",
            "MemoryVNext labeled facts",
        )
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(td, ignore_errors=True)


def run_b_topic(report: Report) -> None:
    """B-topic: switch moves A to STM; WM holds only B."""
    from core.memory.vnext import MemoryVNext

    td = tempfile.mkdtemp(prefix="ww-mem-btopic-")
    try:
        mv = MemoryVNext(data_dir=td, start_dreaming=False)
        try:
            mv.ingest_turn(
                "user",
                "Plan the Stripe payment migration carefully with canaries.",
                new_topic=True,
            )
            tid_a = mv.wm.active.topic_id
            mv.switch_topic(title="Weekend hiking near Tahoe trails")
            ok = (
                mv.topic_stm.get(tid_a) is not None
                and mv.wm.active is not None
                and mv.wm.active.topic_id != tid_a
                and "Stripe" not in (mv.wm.active.body_text() or "")
            )
            report.add(
                "B-topic switch parks A in STM",
                ok,
                f"stm_has_a={mv.topic_stm.get(tid_a) is not None} "
                f"wm_id={mv.wm.active.topic_id[:8] if mv.wm.active else None}",
                "MemoryVNext topic WM",
            )
        finally:
            mv.close()
    except Exception as e:
        report.add("B-topic switch parks A in STM", False, f"error: {e}")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_b_summary(report: Report) -> None:
    """B-summary: digests never re-compressed; travel with body on switch."""
    from core.memory.topic import WorkingTopicStore

    td = tempfile.mkdtemp(prefix="ww-mem-bsum-")
    parked = []
    try:
        store = WorkingTopicStore(
            data_dir=td,
            token_budget=180,
            on_switch=lambda t: parked.append(t),
        )
        for i in range(16):
            store.append_turn(
                "user",
                f"Turn {i}: " + ("payment infrastructure details " * 12),
            )
        dig_n = len(store.active.digests) if store.active else 0
        dig_ids = [d.digest_id for d in (store.active.digests if store.active else [])]
        for i in range(8):
            store.append_turn(
                "assistant",
                f"Reply {i}: " + ("more context about APIs " * 10),
            )
        still = all(
            any(d.digest_id == did for d in store.active.digests)
            for did in dig_ids
        ) if store.active and dig_ids else dig_n == 0
        store.switch_topic(title="B independent")
        travel = bool(parked) and len(parked[0].digests) >= dig_n and dig_n >= 1
        ok = dig_n >= 1 and still and travel
        report.add(
            "B-summary digests stable + travel",
            ok,
            f"digests={dig_n} still={still} travel={travel}",
            "WorkingTopicStore",
        )
    except Exception as e:
        report.add("B-summary digests stable + travel", False, f"error: {e}")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_b_atom(report: Report) -> None:
    """B-atom: dual timestamps + Updates supersede current vs historical."""
    from core.memory.atom_nets import AtomNetStore, MemoryAtomV2

    td = tempfile.mkdtemp(prefix="ww-mem-batom-")
    try:
        store = AtomNetStore(data_dir=td)
        t0 = time.time() - 1000
        old = MemoryAtomV2(
            content="Alex works at Google",
            logical_net="world",
            learned_at=t0,
            valid_from=t0,
            entities=["Alex"],
        )
        new = MemoryAtomV2(
            content="Alex joined Stripe as PM",
            logical_net="world",
            learned_at=time.time(),
            entities=["Alex", "Stripe"],
        )
        store.add(old)
        store.add(new)
        store.updates(new, old)
        cur = store.current_truth("Alex")
        hist = store.historical("Alex")
        ok = (
            not old.is_currently_valid
            and new.is_currently_valid
            and any("Stripe" in a.content for a in cur)
            and any(a.atom_id == old.atom_id for a in hist)
            and old.learned_at == t0
            and new.learned_at >= t0
        )
        report.add(
            "B-atom dual-ts Updates supersede",
            ok,
            f"cur={[a.content[:30] for a in cur]} hist_n={len(hist)}",
            "AtomNetStore",
        )
    except Exception as e:
        report.add("B-atom dual-ts Updates supersede", False, f"error: {e}")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_b_hippo_promote(report: Report) -> None:
    """B-hippo-promote: leave hippo (purge/promote) always extracts atoms."""
    from core.memory.atom_nets import MemoryAtomV2
    from core.memory.topic import Topic
    from core.memory.topic_stm import TopicHippocampus

    td = tempfile.mkdtemp(prefix="ww-mem-bhippo-")
    extracted = []
    try:
        def extract(topic):
            atoms = [
                MemoryAtomV2(
                    content=f"from-{topic.topic_id[:6]} {topic.title}",
                    topic_id=topic.topic_id,
                )
            ]
            extracted.extend(atoms)
            return atoms

        hip = TopicHippocampus(
            data_dir=td, cap=2, atom_extract=extract, on_promote=lambda t, a: None
        )
        a = Topic(title="Disposable kitchen inventory checklist item")
        a.append_turn("user", "Disposable kitchen inventory checklist item for weekend.")
        hip.admit(a)
        pr = hip.purge(a.topic_id)
        purge_ok = pr.get("ok") and pr.get("atoms_extracted", 0) >= 1

        b = Topic(title="Prefer dark mode permanently in editor settings")
        b.append_turn("user", "Prefer dark mode permanently in editor settings for focus.")
        b.relevance = 1.0
        b.recall_count = 5
        hip.admit(b)
        extracted.clear()
        prom = hip.promote(b.topic_id, force=True)
        promote_ok = prom.get("ok") and prom.get("atoms_extracted", 0) >= 1
        ok = bool(purge_ok and promote_ok)
        report.add(
            "B-hippo-promote extract on leave",
            ok,
            f"purge={purge_ok} promote={promote_ok}",
            "TopicHippocampus",
        )
    except Exception as e:
        report.add("B-hippo-promote extract on leave", False, f"error: {e}")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_b_ltm_tier(report: Report) -> None:
    """B-ltm-tier: ww:// tiers Abstract/Overview/Detail + immutable events."""
    from core.memory.ltm_vfs import ContentTier, ImmutableLTMError, LTMVFS

    td = tempfile.mkdtemp(prefix="ww-mem-bltm-")
    try:
        ltm = LTMVFS(data_dir=td)
        body = "Lesson one about canary deploys. " * 40
        uri = ltm.write("experiences", body, title="canary", name="canary")
        abs_t = ltm.read(uri, tier=ContentTier.ABSTRACT)
        det = ltm.read(uri, tier=ContentTier.DETAIL)
        ev = ltm.write("events", "Shipped v-next", title="ship", name="ship")
        immut = False
        try:
            ltm.update(ev, "rewrite")
        except ImmutableLTMError:
            immut = True
        ok = (
            uri.startswith("ww://")
            and len(abs_t) < len(det)
            and immut
            and ("canary" in det.lower() or "Lesson" in det)
        )
        report.add(
            "B-ltm-tier progressive + immutable",
            ok,
            f"uri={uri[:40]} abs={len(abs_t)} det={len(det)} immut={immut}",
            "LTMVFS",
        )
    except Exception as e:
        report.add("B-ltm-tier progressive + immutable", False, f"error: {e}")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_b_dream(report: Report) -> None:
    """B-dream: enqueue is non-blocking; empty store is cheap no-op."""
    from core.memory.atom_nets import AtomNetStore, MemoryAtomV2
    from core.memory.dreaming import DreamingWorker
    from core.memory.ltm_vfs import LTMVFS

    td = tempfile.mkdtemp(prefix="ww-mem-bdream-")
    prev = os.environ.get("WW_DREAMING_ENABLED")
    try:
        os.environ["WW_DREAMING_ENABLED"] = "1"
        store = AtomNetStore(data_dir=td)
        ltm = LTMVFS(data_dir=td)
        worker = DreamingWorker(atom_store=store, ltm=ltm, auto_start=True)
        try:
            t0 = time.time()
            empty = worker.enqueue("full")
            elapsed = time.time() - t0
            store.add(
                MemoryAtomV2(
                    content="Nebula uses React 18 for checkout UI",
                    logical_net="world",
                    entities=["Nebula", "React"],
                )
            )
            t1 = time.time()
            q = worker.enqueue("full")
            elapsed2 = time.time() - t1
            # Hot path still works without waiting
            hits = store.current_truth("React")
            worker.wait_empty(timeout=3.0)
            ok = (
                empty.get("queued") is True
                and q.get("queued") is True
                and elapsed < 0.5
                and elapsed2 < 0.5
                and any("React" in a.content for a in hits)
            )
            report.add(
                "B-dream async non-blocking",
                ok,
                f"empty_q={empty} elapsed={elapsed:.3f} hits={len(hits)}",
                "DreamingWorker",
            )
        finally:
            worker.stop()
    except Exception as e:
        report.add("B-dream async non-blocking", False, f"error: {e}")
    finally:
        if prev is None:
            os.environ.pop("WW_DREAMING_ENABLED", None)
        else:
            os.environ["WW_DREAMING_ENABLED"] = prev
        shutil.rmtree(td, ignore_errors=True)


def run_mechanism(report: Report) -> None:
    run_l0(report)
    run_b1_capacity(report)
    run_b2_gc(report)
    run_promote(report)
    run_b3_working_memory(report)
    run_b4_wm_kind_eviction(report)
    run_b5_wm_tiebreak_switch(report)
    run_b6_wm_recency(report)
    run_b7_wm_label_order(report)
    run_b_topic(report)
    run_b_summary(report)
    run_b_atom(report)
    run_b_hippo_promote(report)
    run_b_ltm_tier(report)
    run_b_dream(report)


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
    ap.add_argument(
        "--mechanism", action="store_true", help="L0 + B1 + B2 + promote + B3 WM"
    )
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
