"""
Tests: Entity Working Memory — fixed-capacity RAM with numeric eviction.

Offline only (no network). Covers:
- Write capacity+1 → least-access / oldest key evicted
- High-access key retained under pressure
- Promote callback and/or wm_evicted.jsonl on eviction
- capacity=1 boundary
- is_core / preferences protected from eviction
- context injection only contains current RAM set
- Memory roles (kind): commitment / rationale / outcome weighted eviction
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
    DEFAULT_WORKING_MEMORY_CAPACITY,
    ROLE_WEIGHT,
    EntityStateManager,
    normalize_wm_kind,
    resolve_working_memory_capacity,
    wm_eviction_score,
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


# ── Memory roles (kind) ──────────────────────────────────────────


def test_normalize_wm_kind():
    assert normalize_wm_kind(None) == "outcome"
    assert normalize_wm_kind("") == "outcome"
    assert normalize_wm_kind("unknown") == "outcome"
    assert normalize_wm_kind("garbage") == "outcome"
    assert normalize_wm_kind("COMMITMENT") == "commitment"
    assert normalize_wm_kind("rationale") == "rationale"
    assert normalize_wm_kind("outcome") == "outcome"
    assert DEFAULT_WM_KIND == "outcome"
    assert ROLE_WEIGHT["commitment"] > ROLE_WEIGHT["outcome"] > ROLE_WEIGHT["rationale"]


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


# ── Optional subconscious WM tie-break ────────────────────────────


def test_wm_tiebreak_decides_when_kind_and_access_equal(esm):
    """Same kind + same access: higher tiebreak protect stays, lower leaves."""
    esm.working_memory_capacity = 1
    scores = {"keep": 10.0, "drop": 1.0}

    def tb(entity_id, key, meta):
        return float(scores.get(key, 0.0))

    esm.set_wm_tiebreak_fn(tb)
    eid = "ent_tb"
    esm.set_working_memory(eid, "keep", "v-keep", kind="outcome")
    time.sleep(0.02)
    esm.set_working_memory(eid, "drop", "v-drop", kind="outcome")
    # Both outcome/access=0 → primary score ties; drop has lower protect → victim
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


def test_context_injection_includes_kind_tag(esm):
    eid = "ent_ctx_kind"
    esm.set_working_memory(eid, "decision", "ship it", kind="commitment")
    text = esm.get_context_for(eid)
    assert "[commitment]" in text
    assert "decision" in text
    assert "ship it" in text
