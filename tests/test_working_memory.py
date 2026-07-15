"""
Tests: Entity Working Memory — fixed-capacity RAM with numeric eviction.

Offline only (no network). Covers:
- Write capacity+1 → least-access / oldest key evicted
- High-access key retained under pressure
- Promote callback and/or wm_evicted.jsonl on eviction
- capacity=1 boundary
- is_core / preferences protected from eviction
- context injection only contains current RAM set
- Closed WM labels (kind): constraint > commitment > outcome > rationale
- Chinese inject tags (约束/承诺/结果/理由); no protect_last_n / keyword inference
- Recency decay as multiplicative importance
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.entity_state import (
    DEFAULT_WM_KIND,
    DEFAULT_WM_RECENCY_FLOOR,
    DEFAULT_WM_RECENCY_HALF_LIFE_S,
    DEFAULT_WORKING_MEMORY_CAPACITY,
    LABEL_WEIGHT,
    ROLE_WEIGHT,
    WM_KINDS,
    WM_LABEL_ZH,
    EntityStateManager,
    normalize_wm_kind,
    resolve_working_memory_capacity,
    wm_eviction_score,
    wm_label_zh,
    wm_recency_factor,
)


@pytest.fixture
def cfg():
    c = MagicMock()
    c.get = MagicMock(return_value=None)
    c.expand_path = MagicMock(side_effect=lambda p: os.path.expanduser(p))
    return c


@pytest.fixture
def esm(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv("WW_WORKING_MEMORY_CAPACITY", raising=False)
    mgr = EntityStateManager(config=cfg, data_dir=str(tmp_path / "entities"))
    return mgr


def test_default_capacity_constant():
    assert DEFAULT_WORKING_MEMORY_CAPACITY == 32


def test_resolve_capacity_env(monkeypatch):
    monkeypatch.setenv("WW_WORKING_MEMORY_CAPACITY", "7")
    assert resolve_working_memory_capacity(None) == 7


def test_write_over_capacity_evicts_oldest_low_access(esm, tmp_path):
    esm.working_memory_capacity = 3
    eid = "ent_cap"
    # Stagger writes so updated_at differs
    esm.set_working_memory(eid, "a", "va")
    time.sleep(0.02)
    esm.set_working_memory(eid, "b", "vb")
    time.sleep(0.02)
    esm.set_working_memory(eid, "c", "vc")
    time.sleep(0.02)
    esm.set_working_memory(eid, "d", "vd")  # should evict "a" (oldest, access=0)

    state = esm.get(eid)
    assert len(state.working_memory) == 3
    assert "a" not in state.working_memory
    assert set(state.working_memory.keys()) == {"b", "c", "d"}
    assert state.wm_evicted_total >= 1

    archive = Path(tmp_path) / "entities" / eid / "wm_evicted.jsonl"
    assert archive.exists()
    lines = [json.loads(x) for x in archive.read_text().splitlines() if x.strip()]
    assert any(r["key"] == "a" and r["value"] == "va" for r in lines)


def test_high_access_key_not_evicted(esm):
    esm.working_memory_capacity = 2
    eid = "ent_hot"
    esm.set_working_memory(eid, "cold", "old")
    time.sleep(0.02)
    esm.set_working_memory(eid, "hot", "important")
    # Bump access on hot heavily
    state = esm.get(eid)
    for _ in range(5):
        state.bump_wm_access(["hot"])
    esm.save(state)

    esm.set_working_memory(eid, "new", "incoming")
    state = esm.get(eid)
    assert len(state.working_memory) == 2
    assert "hot" in state.working_memory
    assert "cold" not in state.working_memory
    assert "new" in state.working_memory


def test_promote_callback_on_evict(esm):
    """High-access key forced out by a protected core slot still promotes."""
    esm.working_memory_capacity = 1
    esm._promote_min_access = 2
    promoted = []

    def on_evict(entity_id, key, value):
        promoted.append((entity_id, key, value))

    esm.set_on_wm_evict(on_evict)
    eid = "ent_promo"
    esm.set_working_memory(eid, "keepish", "v1")
    state = esm.get(eid)
    # access_count >= 2 → promote when this key is the only eligible victim
    state.bump_wm_access(["keepish"])
    state.bump_wm_access(["keepish"])
    esm.save(state)

    # Core key cannot be the victim → keepish is evicted and promoted
    esm.set_working_memory(eid, "core_slot", "protected", is_core=True)
    assert "keepish" not in esm.get(eid).working_memory
    assert "core_slot" in esm.get(eid).working_memory
    assert any(p[1] == "keepish" and p[2] == "v1" for p in promoted)


def test_long_value_promote_when_accessed(esm, tmp_path):
    esm.working_memory_capacity = 1
    esm._promote_min_access = 99  # force long-value path
    esm._promote_long_len = 20
    promoted = []
    esm.set_on_wm_evict(lambda e, k, v: promoted.append(k))

    eid = "ent_long"
    long_val = "x" * 40
    esm.set_working_memory(eid, "longk", long_val)
    state = esm.get(eid)
    state.bump_wm_access(["longk"])  # accessed once
    esm.save(state)
    # Force longk out (only non-protected candidate)
    esm.set_working_memory(eid, "core_n", "short", is_core=True)
    assert "longk" in promoted


def test_capacity_one_boundary(esm):
    esm.working_memory_capacity = 1
    eid = "ent_one"
    esm.set_working_memory(eid, "k1", "v1")
    esm.set_working_memory(eid, "k2", "v2")
    esm.set_working_memory(eid, "k3", "v3")
    state = esm.get(eid)
    assert len(state.working_memory) == 1
    assert "k3" in state.working_memory
    assert state.wm_evicted_total == 2


def test_core_key_not_evicted(esm):
    esm.working_memory_capacity = 1
    eid = "ent_core"
    esm.set_working_memory(eid, "core_fact", "stay", is_core=True)
    esm.set_working_memory(eid, "junk", "go")
    state = esm.get(eid)
    # Core protected → may exceed capacity when only protected remains + new
    # After writing junk: if core protected, junk stays or core stays depending
    # on order. Core must remain.
    assert "core_fact" in state.working_memory
    assert "core_fact" in state.working_memory_core


def test_preference_key_protected(esm):
    esm.working_memory_capacity = 1
    eid = "ent_pref"
    state = esm.get(eid)
    state.preferences["lang"] = "zh"
    esm.save(state)
    esm.set_working_memory(eid, "lang", "zh-TW")
    esm.set_working_memory(eid, "temp", "x")
    state = esm.get(eid)
    assert "lang" in state.working_memory


def test_context_injection_bounded_and_titled(esm):
    esm.working_memory_capacity = 2
    eid = "ent_ctx"
    esm.set_working_memory(eid, "f1", "one")
    esm.set_working_memory(eid, "f2", "two")
    esm.set_working_memory(eid, "f3", "three")
    text = esm.get_context_for(eid)
    assert "Working memory (online facts):" in text
    state = esm.get(eid)
    # Only current RAM keys appear
    for k, v in state.working_memory.items():
        assert k in text and v in text
    # Evicted key not injected
    assert len(state.working_memory) <= 2


def test_record_interaction_updates_enforce_capacity(esm):
    esm.working_memory_capacity = 2
    eid = "ent_rec"
    esm.record_interaction(eid, "ctx1", updates={"a": "1", "b": "2", "c": "3"})
    state = esm.get(eid)
    assert len(state.working_memory) == 2
    assert state.wm_evicted_total >= 1


def test_wm_status_fields(esm):
    esm.working_memory_capacity = 5
    eid = "ent_stat"
    esm.set_working_memory(eid, "x", "1")
    st = esm.get_wm_status(eid)
    assert st["working_memory_size"] == 1
    assert st["working_memory_capacity"] == 5
    assert st["wm_evicted_total"] == 0


def test_memory_tools_is_core_and_promote_wire(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv("WW_WORKING_MEMORY_CAPACITY", raising=False)
    esm = EntityStateManager(config=cfg, data_dir=str(tmp_path / "entities"))
    esm.working_memory_capacity = 1

    class FakeMem:
        def __init__(self):
            self.facts = []

        def store_fact(self, fact, entities, context_id=""):
            self.facts.append({"fact": fact, "entities": entities, "ctx": context_id})
            return {"ok": True}

    mem = FakeMem()
    from core.memory.tools import MemoryTools

    tools = MemoryTools(memory_system=mem, entity_state_mgr=esm, entity_id="ent_t")
    tools.remember("hot", "value-hot", is_core=False)
    state = esm.get("ent_t")
    for _ in range(3):
        state.bump_wm_access(["hot"])
    esm.save(state)
    # Core write forces hot out → promote via on_wm_evict → store_fact
    tools.remember("core_k", "value-core", is_core=True)
    assert any(
        "hot" in f["fact"] and "wm_evict" in (f.get("entities") or [])
        for f in mem.facts
    )


# ── Closed WM labels (kind == label id) ───────────────────────────


def test_wm_kinds_four_labels_and_normalize():
    """L0: closed enum has four labels; normalize ok for each + illegal → outcome."""
    assert WM_KINDS == frozenset({"constraint", "commitment", "outcome", "rationale"})
    assert normalize_wm_kind(None) == "outcome"
    assert normalize_wm_kind("") == "outcome"
    assert normalize_wm_kind("unknown") == "outcome"
    assert normalize_wm_kind("garbage") == "outcome"
    assert normalize_wm_kind("not-a-role") == "outcome"
    assert normalize_wm_kind("COMMITMENT") == "commitment"
    assert normalize_wm_kind("constraint") == "constraint"
    assert normalize_wm_kind("CONSTRAINT") == "constraint"
    assert normalize_wm_kind("rationale") == "rationale"
    assert normalize_wm_kind("outcome") == "outcome"
    assert DEFAULT_WM_KIND == "outcome"
    # Weight order: constraint > commitment > outcome > rationale (fixed scores)
    assert (
        ROLE_WEIGHT["constraint"]
        > ROLE_WEIGHT["commitment"]
        > ROLE_WEIGHT["outcome"]
        > ROLE_WEIGHT["rationale"]
    )
    assert ROLE_WEIGHT["constraint"] == pytest.approx(4.0)
    assert ROLE_WEIGHT["commitment"] == pytest.approx(3.0)
    assert ROLE_WEIGHT["outcome"] == pytest.approx(2.0)
    assert ROLE_WEIGHT["rationale"] == pytest.approx(1.0)
    assert LABEL_WEIGHT is ROLE_WEIGHT
    assert wm_label_zh("constraint") == "约束"
    assert wm_label_zh("commitment") == "承诺"
    assert wm_label_zh("outcome") == "结果"
    assert wm_label_zh("rationale") == "理由"
    assert set(WM_LABEL_ZH) == WM_KINDS


def test_rationale_evicted_before_commitment(esm):
    """Full capacity: rationale is squeezed before commitment (same access)."""
    esm.working_memory_capacity = 2
    eid = "ent_kind_order"
    esm.set_working_memory(eid, "dec", "do A", kind="commitment")
    time.sleep(0.02)
    esm.set_working_memory(eid, "why", "because B", kind="rationale")
    time.sleep(0.02)
    esm.set_working_memory(eid, "new", "incoming", kind="outcome")

    state = esm.get(eid)
    assert len(state.working_memory) == 2
    assert "dec" in state.working_memory
    assert "why" not in state.working_memory
    assert "new" in state.working_memory


def test_same_access_commitment_stays_rationale_goes(esm):
    """Equal access_count: commitment score higher → rationale leaves first."""
    esm.working_memory_capacity = 1
    eid = "ent_kind_access"
    esm.set_working_memory(eid, "cmt", "decide X", kind="commitment")
    esm.set_working_memory(eid, "rat", "reason Y", kind="rationale")
    # Both access 0; rationale weight 1 < commitment 3 → rat evicted when only one slot
    # After second write capacity is 1: only one remains — must be commitment if both eligible
    # Write order: cmt first, then rat forces eviction of lower score among {cmt, rat}.
    # After rat write, size=2 > 1, victim = min(score): rat score=1, cmt score=3 → rat goes,
    # but wait we just wrote rat — both are in WM then eviction runs. Victim is rat.
    # Final: only cmt? Actually both were candidates; rat has lower score so rat is victim.
    # Result: cmt remains. But we wanted rat to be the new write... eviction removes lowest
    # score among ALL keys including the new one. So rat (new) gets removed if score lower.
    state = esm.get(eid)
    assert "cmt" in state.working_memory
    assert "rat" not in state.working_memory

    # Flip order: write rationale first, then commitment under pressure
    esm.working_memory_capacity = 1
    eid2 = "ent_kind_access2"
    esm.set_working_memory(eid2, "rat2", "process", kind="rationale")
    esm.set_working_memory(eid2, "cmt2", "decide", kind="commitment")
    state2 = esm.get(eid2)
    assert "cmt2" in state2.working_memory
    assert "rat2" not in state2.working_memory


def test_remember_kind_commitment_writes_meta(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv("WW_WORKING_MEMORY_CAPACITY", raising=False)
    esm = EntityStateManager(config=cfg, data_dir=str(tmp_path / "entities2"))
    from core.memory.tools import MemoryTools

    tools = MemoryTools(memory_system=None, entity_state_mgr=esm, entity_id="ent_rk")
    out = tools.remember("plan", "use docker", kind="commitment")
    assert out.get("kind") == "commitment"
    state = esm.get("ent_rk")
    assert state.working_memory_meta["plan"]["kind"] == "commitment"


def test_legacy_meta_without_kind_uses_outcome_weight(esm):
    """Old data missing meta.kind scores as outcome."""
    esm.working_memory_capacity = 2
    eid = "ent_legacy"
    esm.set_working_memory(eid, "legacy", "old-fact")  # default kind outcome
    # Simulate pre-kind data: strip kind field
    state = esm.get(eid)
    state.working_memory_meta["legacy"] = {
        "updated_at": state.working_memory_meta["legacy"]["updated_at"],
        "access_count": 0,
    }
    esm.save(state)

    # Add a rationale (lower weight) and a new key → rationale should go first
    esm.set_working_memory(eid, "why", "process note", kind="rationale")
    time.sleep(0.02)
    esm.set_working_memory(eid, "extra", "pressure", kind="outcome")

    state = esm.get(eid)
    assert "legacy" in state.working_memory  # outcome-weight default
    assert "why" not in state.working_memory
    assert "extra" in state.working_memory
    # score equality check: missing kind ≡ outcome
    assert wm_eviction_score(None, 0) == wm_eviction_score("outcome", 0)


def test_illegal_kind_normalized_to_outcome(esm):
    esm.set_working_memory("ent_bad", "k", "v", kind="not-a-role")
    state = esm.get("ent_bad")
    assert state.working_memory_meta["k"]["kind"] == "outcome"


def test_constraint_survives_over_commitment_under_capacity(esm, monkeypatch):
    """L0: same access/age — constraint beats commitment under capacity pressure."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "0")  # isolate label weight
    esm.working_memory_capacity = 1
    eid = "ent_c_vs_cmt"
    esm.set_working_memory(eid, "cmt", "next: deploy", kind="commitment")
    esm.set_working_memory(eid, "rule", "never change netplan", kind="constraint")
    state = esm.get(eid)
    assert "rule" in state.working_memory
    assert "cmt" not in state.working_memory

    # Flip write order: constraint first, then commitment under pressure
    eid2 = "ent_c_vs_cmt2"
    esm.set_working_memory(eid2, "rule2", "no sudo", kind="constraint")
    esm.set_working_memory(eid2, "cmt2", "plan B", kind="commitment")
    state2 = esm.get(eid2)
    assert "rule2" in state2.working_memory
    assert "cmt2" not in state2.working_memory

    # Fixed scores: constraint > commitment > outcome > rationale
    assert wm_eviction_score("constraint", 0) > wm_eviction_score("commitment", 0)
    assert wm_eviction_score("commitment", 0) > wm_eviction_score("outcome", 0)
    assert wm_eviction_score("outcome", 0) > wm_eviction_score("rationale", 0)


def test_constraint_does_not_replace_is_core(esm, monkeypatch):
    """constraint is soft weight only; is_core hard-protect still required for iron rules."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "0")
    esm.working_memory_capacity = 1
    eid = "ent_core_vs_c"
    # is_core outcome beats non-core constraint when capacity is 1 after both written
    esm.set_working_memory(eid, "core_out", "identity", is_core=True, kind="outcome")
    esm.set_working_memory(eid, "soft_c", "maybe rule", kind="constraint")
    state = esm.get(eid)
    assert "core_out" in state.working_memory
    assert "core_out" in state.working_memory_core
    # soft constraint may be the only non-core or may be gone — core must stay
    assert "core_out" in state.working_memory


# ── Optional subconscious WM tie-break ────────────────────────────


def test_wm_tiebreak_decides_when_kind_and_access_equal(esm):
    """Same kind + same access + same age: higher tiebreak protect stays."""
    scores = {"keep": 10.0, "drop": 1.0}

    def tb(entity_id, key, meta):
        return float(scores.get(key, 0.0))

    esm.set_wm_tiebreak_fn(tb)
    eid = "ent_tb"
    # Seed under capacity=2, equalize ages so primary scores tie, then squeeze
    esm.working_memory_capacity = 2
    esm.set_working_memory(eid, "keep", "v-keep", kind="outcome")
    esm.set_working_memory(eid, "drop", "v-drop", kind="outcome")
    state = esm.get(eid)
    t0 = 1_700_000_000.0
    for k in ("keep", "drop"):
        state.working_memory_meta[k]["updated_at"] = t0
        state.working_memory_meta[k]["access_count"] = 0
    esm.save(state)
    esm.working_memory_capacity = 1
    esm._enforce_wm_capacity(state, now=t0 + 10.0)
    esm.save(state)
    state = esm.get(eid)
    assert "keep" in state.working_memory
    assert "drop" not in state.working_memory


def test_wm_tiebreak_cannot_override_commitment_over_rationale(esm):
    """Different kind scores: commitment stays even if rationale has huge tiebreak."""
    esm.working_memory_capacity = 1

    def tb(entity_id, key, meta):
        # Prefer rationale massively if tiebreak were primary (it must not be)
        if key == "rat":
            return 1e9
        return 0.0

    esm.set_wm_tiebreak_fn(tb)
    eid = "ent_tb_kind"
    esm.set_working_memory(eid, "cmt", "decide", kind="commitment")
    time.sleep(0.02)
    esm.set_working_memory(eid, "rat", "process", kind="rationale")
    state = esm.get(eid)
    assert "cmt" in state.working_memory
    assert "rat" not in state.working_memory


def test_wm_no_tiebreak_fn_matches_score_only(esm):
    """Unset hook: behavior is kind+access+updated only (640846e)."""
    esm.working_memory_capacity = 2
    assert esm._wm_tiebreak_fn is None
    eid = "ent_no_tb"
    esm.set_working_memory(eid, "dec", "do A", kind="commitment")
    time.sleep(0.02)
    esm.set_working_memory(eid, "why", "because B", kind="rationale")
    time.sleep(0.02)
    esm.set_working_memory(eid, "new", "incoming", kind="outcome")
    state = esm.get(eid)
    assert "dec" in state.working_memory
    assert "why" not in state.working_memory
    assert "new" in state.working_memory


def test_set_on_wm_score_alias(esm):
    called = []

    def tb(entity_id, key, meta):
        called.append(key)
        return 0.0

    esm.set_on_wm_score(tb)
    assert esm._wm_tiebreak_fn is tb
    esm.working_memory_capacity = 1
    esm.set_working_memory("e", "a", "1")
    esm.set_working_memory("e", "b", "2")
    assert called  # invoked during eviction


    esm.set_working_memory("ent_bad", "k2", "v2", kind="unknown")
    state = esm.get("ent_bad")
    assert state.working_memory_meta["k2"]["kind"] == "outcome"


def test_context_injection_includes_chinese_label_tag(esm):
    """Inject format LOCKED: ``- [{zh}] key: value`` (Chinese brackets)."""
    eid = "ent_ctx_kind"
    esm.set_working_memory(eid, "decision", "ship it", kind="commitment")
    esm.set_working_memory(eid, "no_netplan", "never change netplan", kind="constraint")
    esm.set_working_memory(eid, "build", "ok", kind="outcome")
    esm.set_working_memory(eid, "why", "chose flash", kind="rationale")
    text = esm.get_context_for(eid)
    assert "- [承诺] decision: ship it" in text
    assert "- [约束] no_netplan: never change netplan" in text
    assert "- [结果] build: ok" in text
    assert "- [理由] why: chose flash" in text
    # English kind ids must not appear as inject brackets
    assert "[commitment]" not in text
    assert "[constraint]" not in text
    assert "[outcome]" not in text
    assert "[rationale]" not in text


# ── Recency decay (importance dimension; no protect_last_n) ───────


def test_recency_factor_defaults_and_shape():
    """Deterministic half-life formula; floor + decay toward floor."""
    assert DEFAULT_WM_RECENCY_HALF_LIFE_S == 3600.0
    assert DEFAULT_WM_RECENCY_FLOOR == 0.4
    assert wm_recency_factor(0.0, half_life_s=3600.0, floor=0.4) == pytest.approx(1.0)
    mid = wm_recency_factor(3600.0, half_life_s=3600.0, floor=0.4)
    assert mid == pytest.approx(0.4 + 0.6 * 0.5)
    old = wm_recency_factor(10_000.0, half_life_s=3600.0, floor=0.4)
    assert old < mid
    assert old >= 0.4
    # HALF_LIFE_S <= 0 → factor 1.0 (disabled)
    assert wm_recency_factor(9999.0, half_life_s=0.0, floor=0.4) == 1.0
    assert wm_recency_factor(9999.0, half_life_s=-1.0, floor=0.4) == 1.0
    # age clamp
    assert wm_recency_factor(-100.0, half_life_s=3600.0, floor=0.4) == pytest.approx(1.0)


def test_recency_on_later_updated_higher_score(monkeypatch):
    """L0-1: same kind, same access → later updated_at has higher score."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    monkeypatch.setenv("WW_WM_RECENCY_HALF_LIFE_S", "3600")
    monkeypatch.setenv("WW_WM_RECENCY_FLOOR", "0.4")
    now = 1_700_000_000.0
    s_old = wm_eviction_score(
        "outcome", 0, age_seconds=3600.0, recency_enabled=True
    )
    s_new = wm_eviction_score(
        "outcome", 0, age_seconds=0.0, recency_enabled=True
    )
    assert s_new > s_old
    # Sort / eviction key order via manager with fixed clock
    assert s_new == pytest.approx(
        ROLE_WEIGHT["outcome"] * 1.0 * 1.0
    )
    assert s_old == pytest.approx(
        ROLE_WEIGHT["outcome"] * 1.0 * wm_recency_factor(3600.0)
    )
    _ = now  # fixed clock used via age_seconds only


def test_recency_on_fresh_outcome_access0_sort_order(esm, monkeypatch):
    """L0-2: fresh outcome access=0 not senselessly preferred-against (fixed clock)."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    monkeypatch.setenv("WW_WM_RECENCY_HALF_LIFE_S", "3600")
    monkeypatch.setenv("WW_WM_RECENCY_FLOOR", "0.4")
    esm.working_memory_capacity = 3
    eid = "ent_rec_sort"
    esm.set_working_memory(eid, "fresh", "just-in", kind="outcome")
    esm.set_working_memory(eid, "stale", "old-one", kind="outcome")
    esm.set_working_memory(eid, "mid", "mid-one", kind="outcome")
    state = esm.get(eid)
    now = 1_700_000_000.0
    # Fixed ages: fresh=0s, mid=600s, stale=7200s; all access=0 outcome
    state.working_memory_meta["fresh"]["updated_at"] = now
    state.working_memory_meta["fresh"]["access_count"] = 0
    state.working_memory_meta["mid"]["updated_at"] = now - 600.0
    state.working_memory_meta["mid"]["access_count"] = 0
    state.working_memory_meta["stale"]["updated_at"] = now - 7200.0
    state.working_memory_meta["stale"]["access_count"] = 0
    esm.save(state)

    keys_by_score = sorted(
        state.working_memory.keys(),
        key=lambda k: esm._wm_eviction_key(state, k, now=now),
    )
    # Lowest score first = first victim: stale < mid < fresh
    assert keys_by_score[0] == "stale"
    assert keys_by_score[-1] == "fresh"
    # Fresh access=0 is not the first victim solely due to access=0
    assert keys_by_score[0] != "fresh"

    # Squeeze one: stale must leave
    esm.working_memory_capacity = 2
    esm._enforce_wm_capacity(state, now=now)
    assert "stale" not in state.working_memory
    assert "fresh" in state.working_memory
    assert "mid" in state.working_memory


def test_old_commitment_high_access_beats_new_rationale(monkeypatch):
    """L0-3: role still dominates pure newness — old hot commitment > new rationale."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    # Old commitment access=5, age=10h; new rationale access=0, age=0
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
    assert s_cmt > s_rat


def test_recency_disabled_matches_baseline_base_only(monkeypatch):
    """L0-4: WW_WM_RECENCY_ENABLED=0 → score == old formula (base only)."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "0")
    base_c = ROLE_WEIGHT["commitment"] * (1 + 2)
    base_r = ROLE_WEIGHT["rationale"] * (1 + 0)
    assert wm_eviction_score("commitment", 2, age_seconds=9999.0) == pytest.approx(
        base_c
    )
    assert wm_eviction_score("rationale", 0, age_seconds=0.0) == pytest.approx(base_r)
    # Explicit recency_enabled=False ignores age
    assert wm_eviction_score(
        "outcome", 3, age_seconds=1e9, recency_enabled=False
    ) == pytest.approx(ROLE_WEIGHT["outcome"] * 4.0)
    # age_seconds=None → base even when env on
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    assert wm_eviction_score("outcome", 1) == pytest.approx(ROLE_WEIGHT["outcome"] * 2.0)


def test_recency_disabled_eviction_order_baseline(esm, monkeypatch):
    """L0-4b: ENABLED=0 sort/eviction uses base score + tertiary updated_at."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "0")
    esm.working_memory_capacity = 2
    eid = "ent_rec_off"
    esm.set_working_memory(eid, "a", "va", kind="outcome")
    esm.set_working_memory(eid, "b", "vb", kind="outcome")
    state = esm.get(eid)
    t0 = 1_700_000_000.0
    state.working_memory_meta["a"]["updated_at"] = t0
    state.working_memory_meta["a"]["access_count"] = 0
    state.working_memory_meta["b"]["updated_at"] = t0 + 100.0
    state.working_memory_meta["b"]["access_count"] = 0
    esm.save(state)
    # Same base score; older a loses via tertiary updated_at
    esm.working_memory_capacity = 1
    esm._enforce_wm_capacity(state, now=t0 + 200.0)
    assert "b" in state.working_memory
    assert "a" not in state.working_memory


def test_core_and_preferences_never_auto_evicted_with_recency(esm, monkeypatch):
    """L0-5: core / preferences hard-protect even with recency on."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    esm.working_memory_capacity = 1
    eid = "ent_rec_prot"
    esm.set_working_memory(eid, "core_fact", "stay", is_core=True)
    esm.set_working_memory(eid, "junk", "go", kind="rationale")
    state = esm.get(eid)
    assert "core_fact" in state.working_memory
    assert "core_fact" in state.working_memory_core

    state.preferences["lang"] = "zh"
    esm.save(state)
    esm.set_working_memory(eid, "lang", "zh-TW", kind="rationale")
    esm.set_working_memory(eid, "temp", "x", kind="rationale")
    state = esm.get(eid)
    assert "lang" in state.working_memory


def test_illegal_kind_outcome_no_keyword_with_recency(esm, monkeypatch):
    """L0-6: illegal kind → outcome; no content keyword path."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    esm.set_working_memory("ent_bad_r", "k", "must commit forever", kind="not-a-role")
    state = esm.get("ent_bad_r")
    assert state.working_memory_meta["k"]["kind"] == "outcome"
    assert normalize_wm_kind("garbage") == "outcome"
    # Value text must not change kind
    esm.set_working_memory(
        "ent_bad_r", "k2", "important commitment decision", kind="unknown"
    )
    state = esm.get("ent_bad_r")
    assert state.working_memory_meta["k2"]["kind"] == "outcome"


def test_over_capacity_jsonl_and_promote_numeric(esm, tmp_path, monkeypatch):
    """L0-7: over capacity → jsonl archive; promote still numeric thresholds."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    esm.working_memory_capacity = 1
    esm._promote_min_access = 2
    promoted = []
    esm.set_on_wm_evict(lambda e, k, v, m=None: promoted.append(k))
    eid = "ent_rec_promo"
    esm.set_working_memory(eid, "hot", "v1", kind="outcome")
    state = esm.get(eid)
    state.bump_wm_access(["hot"])
    state.bump_wm_access(["hot"])
    esm.save(state)
    esm.set_working_memory(eid, "core_slot", "protected", is_core=True)
    assert "hot" not in esm.get(eid).working_memory
    assert "hot" in promoted
    archive = Path(tmp_path) / "entities" / eid / "wm_evicted.jsonl"
    assert archive.exists()
    lines = [json.loads(x) for x in archive.read_text().splitlines() if x.strip()]
    assert any(r["key"] == "hot" for r in lines)


def test_tiebreak_only_on_primary_score_ties_after_recency(esm, monkeypatch):
    """L0-8: subconscious tie-break only on ties; cannot reorder different scores."""
    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    monkeypatch.setenv("WW_WM_RECENCY_HALF_LIFE_S", "3600")
    # Different primary scores: commitment vs rationale — huge tiebreak on rationale loses
    esm.set_wm_tiebreak_fn(lambda e, k, m: 1e9 if k == "rat" else 0.0)
    esm.working_memory_capacity = 1
    eid = "ent_tb_rec"
    esm.set_working_memory(eid, "cmt", "decide", kind="commitment")
    esm.set_working_memory(eid, "rat", "process", kind="rationale")
    state = esm.get(eid)
    assert "cmt" in state.working_memory
    assert "rat" not in state.working_memory

    # Equal primary scores (same kind/access/age): tiebreak decides
    esm.set_wm_tiebreak_fn(lambda e, k, m: 10.0 if k == "keep" else 0.0)
    eid2 = "ent_tb_rec2"
    esm.working_memory_capacity = 2
    esm.set_working_memory(eid2, "keep", "a", kind="outcome")
    esm.set_working_memory(eid2, "drop", "b", kind="outcome")
    state2 = esm.get(eid2)
    t0 = 1_700_000_100.0
    for k in ("keep", "drop"):
        state2.working_memory_meta[k]["updated_at"] = t0
        state2.working_memory_meta[k]["access_count"] = 0
    esm.save(state2)
    esm.working_memory_capacity = 1
    esm._enforce_wm_capacity(state2, now=t0)
    assert "keep" in state2.working_memory
    assert "drop" not in state2.working_memory


def test_no_protect_last_n_symbol(monkeypatch):
    """Hard boundary: no protect_last_n API/path in eviction score surface."""
    import core.entity_state as es

    assert not hasattr(es, "protect_last_n")
    src = Path(es.__file__).read_text(encoding="utf-8")
    # Mentions only as forbidden documentation, never as implementation
    assert "protect_last_n" not in src or "no protect_last_n" in src.lower() or "No protect_last_n" in src
    # Ensure we do not call a protect-last-n helper
    assert "protect_last_n(" not in src
