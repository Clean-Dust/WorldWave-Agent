"""
Tests: Memory curation safety — protect core/immutable, GC rules, promote is_core.

Offline only (no network). Covers:
- FIFO / force eviction skip core and immutable
- Force at full capacity with only protected atoms does not delete them
- GC skips core; GC skips high-salience orphan
- GC reclaims low-salience old orphan (archives first)
- maybe_promote_core under conditions; respects cap
"""

import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, ".")

from core.memory.atom import MemoryAtom, maybe_promote_core
from core.memory.hippocampus import Hippocampus
from core.memory.sleep import SleepConsolidation


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="ww_curation_")
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _store_many(h: Hippocampus, n: int, **kwargs):
    atoms = []
    base_ts = time.time() - n * 10
    for i in range(n):
        a = MemoryAtom(
            content=f"mem-{i}-{kwargs.get('tag', 'x')}",
            importance=kwargs.get("importance", 0.1),
            timestamp=base_ts + i,
            is_core=kwargs.get("is_core", False),
            is_immutable=kwargs.get("is_immutable", False),
        )
        h.store(a)
        atoms.append(a)
    return atoms


# ══════════════════════════════════════════════
# Eviction protection
# ══════════════════════════════════════════════


class TestEvictionProtection:
    def test_fifo_skips_core_and_immutable(self, tmp_dir):
        h = Hippocampus(cap=3, protect_threshold=0.8, data_dir=tmp_dir)
        core = MemoryAtom(
            content="core fact", importance=0.1, timestamp=time.time() - 1000,
            is_core=True,
        )
        imm = MemoryAtom(
            content="immutable code", importance=0.1, timestamp=time.time() - 900,
            is_immutable=True,
        )
        junk = MemoryAtom(
            content="junk", importance=0.1, timestamp=time.time() - 800,
        )
        h.store(core)
        h.store(imm)
        h.store(junk)
        assert len(h) == 3

        # Cap full: store should FIFO-evict junk only (oldest unprotected low-imp)
        h.store(MemoryAtom(content="new", importance=0.2, timestamp=time.time()))
        ids = {a.atom_id for a in h.all()}
        assert core.atom_id in ids
        assert imm.atom_id in ids
        assert junk.atom_id not in ids

    def test_force_skips_core_and_immutable(self, tmp_dir):
        h = Hippocampus(cap=2, protect_threshold=0.8, data_dir=tmp_dir)
        core = MemoryAtom(
            content="core", importance=0.9, timestamp=time.time() - 100,
            is_core=True,
        )
        junk = MemoryAtom(
            content="junk", importance=0.9, timestamp=time.time() - 50,
        )
        h.store(core)
        h.store(junk)
        # Both high importance → fifo fails; force should only remove junk
        ok = h._force_evict_oldest()
        assert ok is True
        ids = {a.atom_id for a in h.all()}
        assert core.atom_id in ids
        assert junk.atom_id not in ids
        # Archived
        archive = os.path.join(tmp_dir, "archive.jsonl")
        assert os.path.isfile(archive)

    def test_force_at_full_only_protected_is_noop(self, tmp_dir):
        h = Hippocampus(cap=2, protect_threshold=0.5, data_dir=tmp_dir)
        a = MemoryAtom(
            content="core-a", importance=0.1, timestamp=time.time() - 20,
            is_core=True,
        )
        b = MemoryAtom(
            content="imm-b", importance=0.1, timestamp=time.time() - 10,
            is_immutable=True,
        )
        h.store(a)
        h.store(b)
        before = {x.atom_id for x in h.all()}
        assert len(before) == 2

        # Force must not delete protected
        assert h._force_evict_oldest() is False
        after = {x.atom_id for x in h.all()}
        assert after == before

        # store at cap still emits capacity path and never deletes protected
        c = MemoryAtom(content="extra", importance=0.1, timestamp=time.time())
        result = h.store(c)
        ids = {x.atom_id for x in h.all()}
        assert a.atom_id in ids
        assert b.atom_id in ids
        assert c.atom_id in ids  # allowed to exceed cap rather than delete core
        assert len(h) == 3
        assert result is not None
        assert result["trigger"] == "capacity_reached"


# ══════════════════════════════════════════════
# Phase 5 GC
# ══════════════════════════════════════════════


class TestPhase5GC:
    def test_gc_skips_core(self, tmp_dir):
        h = Hippocampus(cap=50, data_dir=tmp_dir)
        old = time.time() - 86400 * 60
        core = MemoryAtom(
            content="important core orphan",
            timestamp=old,
            importance=0.01,
            is_core=True,
            links={},
        )
        h.store(core)
        se = SleepConsolidation(
            data_dir=tmp_dir,
            gc_salience_threshold=0.99,
            gc_age_days=1.0,
        )
        removed = se._phase_gc(
            h.all(), h, salience_fn=lambda a: 0.0,
        )
        assert removed == 0
        assert h.get(core.atom_id) is not None

    def test_gc_skips_high_salience_orphan(self, tmp_dir):
        h = Hippocampus(cap=50, data_dir=tmp_dir)
        old = time.time() - 86400 * 60
        atom = MemoryAtom(
            content="salient orphan",
            timestamp=old,
            importance=0.01,
            links={},
        )
        h.store(atom)
        se = SleepConsolidation(
            data_dir=tmp_dir,
            gc_salience_threshold=0.5,
            gc_age_days=1.0,
        )
        removed = se._phase_gc(
            h.all(), h, salience_fn=lambda a: 0.9,  # high salience
        )
        assert removed == 0
        assert h.get(atom.atom_id) is not None

    def test_gc_reclaims_low_salience_old_orphan(self, tmp_dir):
        h = Hippocampus(cap=50, data_dir=tmp_dir)
        old = time.time() - 86400 * 60
        atom = MemoryAtom(
            content="stale junk orphan",
            timestamp=old,
            importance=0.01,
            links={},
        )
        h.store(atom)
        se = SleepConsolidation(
            data_dir=tmp_dir,
            gc_salience_threshold=0.5,
            gc_age_days=1.0,
        )
        removed = se._phase_gc(
            h.all(), h, salience_fn=lambda a: 0.01,
        )
        assert removed == 1
        assert h.get(atom.atom_id) is None
        archive = os.path.join(tmp_dir, "archive.jsonl")
        assert os.path.isfile(archive)
        lines = [ln for ln in open(archive, encoding="utf-8") if ln.strip()]
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["atom"]["atom_id"] == atom.atom_id
        assert entry.get("reason") == "gc"

    def test_gc_skips_immutable(self, tmp_dir):
        h = Hippocampus(cap=50, data_dir=tmp_dir)
        old = time.time() - 86400 * 60
        imm = MemoryAtom(
            content="code hash",
            timestamp=old,
            importance=0.0,
            is_immutable=True,
            links={},
        )
        h.store(imm)
        se = SleepConsolidation(data_dir=tmp_dir, gc_age_days=1.0, gc_salience_threshold=1.0)
        removed = se._phase_gc(h.all(), h, salience_fn=lambda a: 0.0)
        assert removed == 0
        assert h.get(imm.atom_id) is not None

    def test_gc_skips_linked_atom(self, tmp_dir):
        h = Hippocampus(cap=50, data_dir=tmp_dir)
        old = time.time() - 86400 * 60
        atom = MemoryAtom(
            content="linked",
            timestamp=old,
            importance=0.0,
            links={"other": 0.5},
        )
        h.store(atom)
        se = SleepConsolidation(data_dir=tmp_dir, gc_age_days=1.0, gc_salience_threshold=1.0)
        removed = se._phase_gc(h.all(), h, salience_fn=lambda a: 0.0)
        assert removed == 0


# ══════════════════════════════════════════════
# Core promotion
# ══════════════════════════════════════════════


class TestPromoteCore:
    def test_promote_under_conditions(self):
        atom = MemoryAtom(
            content="well recalled",
            importance=0.9,
            stability=1.0,
            recall_count=5,
        )
        assert maybe_promote_core(atom, core_count=0, cap=100) is True
        assert atom.is_core is True

    def test_promote_via_stability(self):
        atom = MemoryAtom(
            content="stable memory",
            importance=0.2,
            stability=5.0,
            recall_count=10,
        )
        assert maybe_promote_core(atom, core_count=0, cap=100) is True
        assert atom.is_core is True

    def test_promote_respects_cap(self):
        atom = MemoryAtom(
            content="candidate",
            importance=0.95,
            recall_count=20,
            stability=8.0,
        )
        # cap=100 → max cores = min(20, 10) = 10
        assert maybe_promote_core(atom, core_count=10, cap=100) is False
        assert atom.is_core is False

    def test_promote_requires_recall(self):
        atom = MemoryAtom(
            content="fresh",
            importance=0.95,
            recall_count=1,
            stability=5.0,
        )
        assert maybe_promote_core(atom, core_count=0, cap=100) is False

    def test_promote_skips_already_core(self):
        atom = MemoryAtom(content="already", is_core=True, recall_count=100)
        assert maybe_promote_core(atom, core_count=0, cap=100) is False

    def test_promote_skips_immutable(self):
        atom = MemoryAtom(
            content="code",
            is_immutable=True,
            importance=1.0,
            recall_count=100,
            stability=10.0,
        )
        assert maybe_promote_core(atom, core_count=0, cap=100) is False

    def test_promote_persisted_via_hippocampus(self, tmp_dir):
        h = Hippocampus(cap=50, data_dir=tmp_dir)
        atom = MemoryAtom(
            content="promote me",
            importance=0.9,
            stability=4.0,
            recall_count=6,
        )
        h.store(atom)
        core_n = h.count_core()
        loaded = h.get(atom.atom_id)
        # get bumps recall_count in DB; use in-memory atom for promotion check
        atom.recall_count = max(atom.recall_count, 6)
        assert maybe_promote_core(atom, core_count=core_n, cap=h.cap)
        h.update(atom.atom_id, is_core=True)
        again = h.get(atom.atom_id)
        assert again is not None
        assert again.is_core is True
        assert h.count_core() >= 1
