"""
Tests: Entity Working Memory — fixed-capacity RAM with numeric eviction.

Offline only (no network). Covers:
- Write capacity+1 → least-access / oldest key evicted
- High-access key retained under pressure
- Promote callback and/or wm_evicted.jsonl on eviction
- capacity=1 boundary
- is_core / preferences protected from eviction
- context injection only contains current RAM set
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.entity_state import (
    DEFAULT_WORKING_MEMORY_CAPACITY,
    EntityStateManager,
    resolve_working_memory_capacity,
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
