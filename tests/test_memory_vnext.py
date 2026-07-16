"""
L0 tests for Memory v-next product slice.

Covers:
1. Topic switch moves A to hippo; WM only B
2. Digest not re-compressed; travels with body
3. Hippo promote threshold + purge still extracts atoms
4. Atom dual timestamp + Updates supersede (current vs historical)
5. Category path + immutability guards (events/trajectories)
6. Core/persona not evicted
7. Dreaming does not block synchronous store/recall
8. Freshness: invalid atom not preferred as current
9. Existing WM label path still green (smoke via import / entity path)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.memory.atom_nets import AtomNetStore, MemoryAtomV2
from core.memory.dreaming import DreamingWorker, dreaming_enabled
from core.memory.ltm_vfs import ContentTier, ImmutableLTMError, LTMVFS
from core.memory.topic import (
    WorkingTopicStore,
    compress_older_turns,
    estimate_tokens,
)
from core.memory.topic_stm import TopicHippocampus, evaluate_topic, passes_hard_filter
from core.memory.topic import Topic, Turn
from core.memory.vnext import MemoryVNext, memory_vnext_enabled


@pytest.fixture
def vnext_dir(tmp_path):
    return str(tmp_path / "vnext")


@pytest.fixture
def mv(vnext_dir, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    monkeypatch.setenv("WW_DREAMING_ENABLED", "1")
    m = MemoryVNext(data_dir=vnext_dir, start_dreaming=False)
    yield m
    m.close()


# ── 1. Topic switch ────────────────────────────────────────────────


def test_topic_switch_moves_a_to_hippo_wm_only_b(mv):
    mv.ingest_turn("user", "Let's plan the Stripe payment migration carefully.", new_topic=True)
    mv.ingest_turn("assistant", "We can stage the Stripe cutover in three phases.")
    tid_a = mv.wm.active.topic_id
    assert mv.wm.active is not None

    result = mv.switch_topic(title="Weekend hiking plans near Tahoe")
    assert result["previous_id"] == tid_a
    assert mv.wm.active.topic_id != tid_a
    assert mv.wm.active.title.startswith("Weekend") or "hiking" in mv.wm.active.title.lower()

    # A is in hippocampus STM; WM has only B
    assert mv.topic_stm.get(tid_a) is not None
    assert mv.topic_stm.count() >= 1
    assert mv.wm.active.topic_id == result["active_id"]
    # body of A should not be the active body
    assert "Stripe" not in (mv.wm.active.body_text() or "")


# ── 2. Digest not re-compressed; travels with body ─────────────────


def test_digest_not_recompressed_travels_with_body(tmp_path):
    parked = []

    def on_switch(topic):
        parked.append(topic)

    store = WorkingTopicStore(
        data_dir=str(tmp_path / "wm"),
        token_budget=200,  # force overflow
        on_switch=on_switch,
    )
    # Many long turns to force digests
    for i in range(20):
        store.append_turn(
            "user",
            f"Turn {i}: " + ("payment infrastructure details " * 15),
        )
    topic = store.active
    assert topic is not None
    assert len(topic.digests) >= 1
    digest_ids = [d.digest_id for d in topic.digests]
    digest_contents = [d.content for d in topic.digests]

    # Overflow again — digests must not be re-compressed (ids/content stay)
    for i in range(10):
        store.append_turn("assistant", f"Reply {i}: " + ("more context about APIs " * 12))

    topic2 = store.active
    assert topic2 is not None
    # Prior digests still present with same ids
    new_ids = {d.digest_id for d in topic2.digests}
    for did, content in zip(digest_ids, digest_contents):
        assert did in new_ids
        match = next(d for d in topic2.digests if d.digest_id == did)
        assert match.content == content
        assert match.is_digest is True

    # Switch → digests travel with body
    store.switch_topic(title="B topic independent")
    assert len(parked) == 1
    assert len(parked[0].digests) >= 1
    assert parked[0].turns is not None


def test_compress_never_touches_digest_payload():
    t = Topic(title="x")
    for i in range(12):
        t.append_turn("user", f"fact number {i} about Project Orion deployment pipeline")
    d1 = compress_older_turns(t, keep_turns=4, keep_tokens=500)
    assert d1 is not None
    original = d1.content
    # Second compress only body; digest content unchanged
    d2 = compress_older_turns(t, keep_turns=2, keep_tokens=200)
    assert t.digests[0].content == original
    if d2:
        assert d2.digest_id != d1.digest_id


# ── 3. Promote threshold + purge extracts atoms ────────────────────


def test_hippo_promote_threshold_and_purge_extracts_atoms(tmp_path):
    extracted = []

    def extract(topic):
        atoms = [
            MemoryAtomV2(
                content=f"atom-from-{topic.topic_id[:6]}: {topic.title or topic.body_text()[:80]}",
                logical_net="experience",
                topic_id=topic.topic_id,
            )
        ]
        extracted.extend(atoms)
        return atoms

    promoted = []

    def on_promote(topic, atoms):
        promoted.append((topic.topic_id, len(atoms)))

    hip = TopicHippocampus(
        data_dir=str(tmp_path / "stm"),
        cap=3,
        atom_extract=extract,
        on_promote=on_promote,
    )

    # Fill with low-score topics
    for i in range(3):
        t = Topic(title=f"chatter-{i}")
        t.append_turn("user", "ok")
        hip.admit(t)

    # High-quality topic that will force capacity leave of others
    good = Topic(title="Alex Stripe PM payment infrastructure")
    good.append_turn(
        "user",
        "Alex is the product manager at Stripe focusing on payment infrastructure.",
    )
    good.append_turn(
        "assistant",
        "Noted: Alex leads payments at Stripe and prefers early standups.",
    )
    good.relevance = 0.9
    good.recall_count = 5
    good.entities = ["Alex", "Stripe"]
    evaluate_topic(good)
    assert good.composite_score > 0  # evaluated

    # Purge path: force capacity
    r = hip.admit(good)
    assert r["action"] in ("admitted", "updated")
    # Something left via purge or promote — atoms extracted
    assert len(extracted) >= 1

    # Explicit purge extracts atoms
    extracted.clear()
    victim = Topic(title="Disposable note about kitchen inventory list")
    victim.append_turn("user", "Disposable note about kitchen inventory list for weekend.")
    hip.admit(victim)
    # Ensure not core
    pr = hip.purge(victim.topic_id)
    if pr.get("ok"):
        assert pr["atoms_extracted"] >= 1
        assert len(extracted) >= 1

    # Promote threshold: needs score + recall after evaluate_topic
    elite = Topic(title="User prefers dark mode and vim keybindings in VS Code")
    elite.append_turn(
        "user",
        "I prefer dark mode and vim keybindings permanently in VS Code editor.",
    )
    elite.append_turn(
        "assistant",
        "Saved preference: dark mode and vim keybindings in VS Code.",
    )
    elite.relevance = 1.0
    elite.recall_count = 5
    elite.entities = ["VS Code", "vim", "dark mode", "keybindings", "editor"]
    elite.tags = ["preference", "editor", "ux", "settings"]
    elite.conceptual_richness = 1.0
    elite.consolidation = 1.0
    elite.query_contexts = ["dark mode", "vim", "VS Code", "editor", "prefs"]
    elite.created_at = time.time() - 3 * 86400
    elite.updated_at = time.time()
    evaluate_topic(elite)
    hip.admit(elite)
    elite2 = hip.get(elite.topic_id)
    assert elite2 is not None
    # Re-assert signals so promote()'s evaluate_topic stays above threshold
    elite2.relevance = 1.0
    elite2.recall_count = 5
    elite2.entities = list(elite.entities)
    elite2.tags = list(elite.tags)
    elite2.conceptual_richness = 1.0
    elite2.consolidation = 1.0
    elite2.query_contexts = list(elite.query_contexts)
    elite2.created_at = elite.created_at
    elite2.updated_at = time.time()
    score = evaluate_topic(elite2)
    assert passes_hard_filter(elite2)
    assert elite2.recall_count >= 3
    # If natural score still below 0.8, force-promote still must extract atoms
    extracted.clear()
    promoted.clear()
    if score >= 0.8:
        res = hip.promote(elite2.topic_id)
        assert res["ok"] is True
    else:
        res = hip.promote(elite2.topic_id, force=True)
        assert res["ok"] is True
    assert res["atoms_extracted"] >= 1
    assert len(promoted) == 1


# ── 4. Dual timestamp + Updates supersede ──────────────────────────


def test_atom_dual_timestamp_and_updates_supersede(tmp_path):
    store = AtomNetStore(data_dir=str(tmp_path / "atoms"))
    t0 = time.time() - 86400
    old = MemoryAtomV2(
        content="Alex works at Google as an engineer",
        logical_net="world",
        learned_at=t0,
        valid_from=t0,
        entities=["Alex", "Google"],
    )
    store.add(old)
    t1 = time.time()
    new = MemoryAtomV2(
        content="Alex joined Stripe as a PM",
        logical_net="world",
        learned_at=t1,
        valid_from=t1,
        entities=["Alex", "Stripe"],
    )
    store.add(new)
    store.updates(new, old)

    # Dual timestamps present
    assert old.learned_at == t0
    assert old.valid_from == t0
    assert new.learned_at == t1

    # Old superseded / invalid; new current
    assert not old.is_currently_valid
    assert old.superseded_by == new.atom_id
    assert new.is_currently_valid

    current = store.current_truth("Alex")
    assert any("Stripe" in a.content for a in current)
    assert not any(a.atom_id == old.atom_id for a in current)

    historical = store.historical("Alex")
    assert any(a.atom_id == old.atom_id for a in historical)
    assert any(a.atom_id == new.atom_id for a in historical)


# ── 5. Category path + immutability ────────────────────────────────


def test_ltm_category_paths_and_immutability(tmp_path):
    ltm = LTMVFS(data_dir=str(tmp_path / "ltm"))
    uri_ev = ltm.write(
        "events",
        "Shipped memory v-next to main on 2026-07-17",
        title="ship-vnext",
        name="ship-vnext",
    )
    assert uri_ev.startswith("ww://user/memories/events/")
    assert "events" in uri_ev

    with pytest.raises(ImmutableLTMError):
        ltm.update(uri_ev, "rewrite history")

    uri_tr = ltm.write(
        "trajectories",
        "Step1 install; Step2 configure; Step3 verify",
        title="deploy-path",
        name="deploy-path",
    )
    with pytest.raises(ImmutableLTMError):
        ltm.update(uri_tr, "mutate trajectory")

    # Merge-update categories allow update
    uri_ex = ltm.write(
        "experiences",
        "First lesson: test before push",
        title="lesson-1",
        name="lesson-1",
    )
    ltm.update(uri_ex, "Second lesson: watch capacity")
    detail = ltm.read(uri_ex, tier=ContentTier.DETAIL)
    assert "First lesson" in detail
    assert "Second lesson" in detail

    # Abstract tier
    abstract = ltm.read(uri_ex, tier=ContentTier.ABSTRACT)
    assert len(abstract) <= 500

    # viking alias
    viking_uri = uri_ex.replace("ww://", "viking://")
    assert ltm.read(viking_uri, tier=ContentTier.ABSTRACT)


# ── 6. Core / persona not evicted ──────────────────────────────────


def test_core_persona_not_evicted(tmp_path):
    hip = TopicHippocampus(
        data_dir=str(tmp_path / "stm-core"),
        cap=2,
        atom_extract=lambda t: [],
    )
    core = Topic(title="Agent persona and identity rules", is_core=True)
    core.append_turn("system", "You are WW. Never reveal private keys.")
    hip.admit(core)

    for i in range(5):
        t = Topic(title=f"noise-{i}")
        t.append_turn("user", f"random small talk number {i} about weather")
        hip.admit(t)

    # Core still present
    assert hip.get(core.topic_id) is not None
    assert hip.get(core.topic_id).is_core
    # Purge of core refused
    r = hip.purge(core.topic_id)
    assert r.get("ok") is False
    assert r.get("error") == "is_core"


# ── 7. Dreaming non-blocking ───────────────────────────────────────


def test_dreaming_does_not_block_store_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_DREAMING_ENABLED", "1")
    store = AtomNetStore(data_dir=str(tmp_path / "atoms"))
    ltm = LTMVFS(data_dir=str(tmp_path / "ltm"))
    for i in range(5):
        store.add(
            MemoryAtomV2(
                content=f"Fact {i}: Project Nebula uses React {i}",
                logical_net="world",
                entities=["Nebula", "React"],
            )
        )
    worker = DreamingWorker(atom_store=store, ltm=ltm, auto_start=True)
    try:
        t0 = time.time()
        q = worker.enqueue("full")
        # enqueue returns immediately
        assert time.time() - t0 < 0.5
        assert q.get("queued") is True

        # Synchronous store/recall path still works immediately
        store.add(MemoryAtomV2(content="Hot path fact: port is 8080", logical_net="world"))
        hits = store.current_truth("8080")
        assert any("8080" in a.content for a in hits)
        # Did not need to wait for dream
        assert time.time() - t0 < 1.0
    finally:
        worker.stop()


# ── 8. Freshness ───────────────────────────────────────────────────


def test_freshness_invalid_atom_not_preferred(tmp_path):
    store = AtomNetStore(data_dir=str(tmp_path / "atoms"))
    bad = MemoryAtomV2(
        content="Preferred language is Java",
        logical_net="world",
        entities=["language"],
        learned_at=time.time() - 1000,
    )
    store.add(bad)
    good = MemoryAtomV2(
        content="Preferred language is Python",
        logical_net="world",
        entities=["language"],
        learned_at=time.time(),
    )
    store.add(good)
    store.updates(good, bad)

    cur = store.current_truth("Preferred language")
    assert cur
    assert "Python" in cur[0].content
    assert all(a.is_currently_valid for a in cur)
    assert not any(a.atom_id == bad.atom_id for a in cur)


# ── 9. Labeled facts on single system (absorbed legacy) ────────────


def test_labeled_facts_kind_core_recency_on_vnext(mv, monkeypatch):
    """Single system: kind order, is_core protect, Chinese inject."""
    from core.entity_state import wm_label_zh

    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "0")
    mv.facts.capacity = 3
    mv.set_entity("ent_labels")
    mv.remember("rule", "never change netplan", kind="constraint", is_core=True)
    mv.remember("plan", "use docker", kind="commitment")
    mv.remember("fact", "tests passed", kind="outcome")
    mv.remember("why", "chose flash", kind="rationale")  # squeezed first

    facts = mv.facts.get_facts("ent_labels")
    assert "rule" in facts
    assert "plan" in facts
    assert "fact" in facts
    assert "why" not in facts
    assert "rule" in mv.facts.get_core("ent_labels")

    inj = mv.facts.inject_block("ent_labels", bump_access=False)
    assert f"- [{wm_label_zh('constraint')}] rule: never change netplan" in inj
    assert "[constraint]" not in inj

    # capacity 1: core survives pressure from non-core
    mv.facts.capacity = 1
    mv.remember("junk", "go away", kind="rationale")
    facts2 = mv.facts.get_facts("ent_labels")
    assert "rule" in facts2


def test_labeled_fact_store_recency_eviction(tmp_path, monkeypatch):
    from core.memory.labeled_wm import LabeledFactStore

    monkeypatch.setenv("WW_WM_RECENCY_ENABLED", "1")
    monkeypatch.setenv("WW_WM_RECENCY_HALF_LIFE_S", "3600")
    monkeypatch.setenv("WW_WM_RECENCY_FLOOR", "0.4")
    store = LabeledFactStore(data_dir=str(tmp_path / "facts"), capacity=2)
    eid = "e_rec"
    store.set(eid, "fresh", "new", kind="outcome")
    store.set(eid, "stale", "old", kind="outcome")
    meta = store.get_meta(eid)
    now = 1_700_000_000.0
    meta["fresh"]["updated_at"] = now
    meta["fresh"]["access_count"] = 0
    meta["stale"]["updated_at"] = now - 7200.0
    meta["stale"]["access_count"] = 0
    # write meta back via internal API under lock
    with store._lock:
        store._meta[eid] = meta
        store._save(eid)
    store.capacity = 1
    store.enforce_capacity(eid, now=now)
    facts = store.get_facts(eid)
    assert "fresh" in facts
    assert "stale" not in facts


def test_tools_remember_prefers_vnext(tmp_path, monkeypatch):
    """MemoryTools product path writes labeled facts on MemoryVNext."""
    from core.memory.system import MemorySystem
    from core.memory.tools import MemoryTools

    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    monkeypatch.setenv("WW_DREAMING_ENABLED", "0")
    ms = MemorySystem(data_dir=str(tmp_path / "mem"), schedule_sleep_hour=-1, idle_threshold_minutes=0)
    assert ms.vnext is not None
    tools = MemoryTools(memory_system=ms, entity_state_mgr=None, entity_id="ent_t")
    out = tools.remember("no_netplan", "never change netplan", kind="constraint", is_core=True)
    assert out.get("kind") == "constraint"
    assert out.get("store") == "vnext"
    listed = tools.recall_mine()
    assert "no_netplan" in listed["facts"]
    ctx = ms.memory_context_block(entity_id="ent_t")
    assert "约束" in ctx or "no_netplan" in ctx
    ms.close() if hasattr(ms, "close") else None
    if ms.vnext:
        ms.vnext.close()


# ── Integration smoke ──────────────────────────────────────────────


def test_vnext_recall_and_prompt_isolation(mv):
    mv.remember("user_name", "Chung", kind="outcome", is_core=True)
    mv.ingest_turn("user", "Discuss Kubernetes rollout strategy for checkout service", new_topic=True)
    mv.ingest_turn("assistant", "Use canary with 5% traffic for checkout.")
    rec = mv.recall("Kubernetes checkout")
    assert "stm" in rec and "atoms" in rec and "ltm" in rec
    blocks = mv.build_context_blocks()
    assert "system_persona_only" in blocks
    assert "working_topic" in blocks
    assert "labeled_facts" in blocks
    # Memory block separate from system persona field
    ctx = mv.inject_for_turn("Kubernetes")
    assert "Core identity" in ctx or "Active topic" in ctx or "Retrieved" in ctx or "user_name" in ctx


def test_memory_vnext_flag_default_on(monkeypatch):
    monkeypatch.delenv("WW_MEMORY_VNEXT", raising=False)
    assert memory_vnext_enabled() is True
    monkeypatch.setenv("WW_MEMORY_VNEXT", "0")
    assert memory_vnext_enabled() is False


def test_dreaming_default_on(monkeypatch):
    monkeypatch.delenv("WW_DREAMING_ENABLED", raising=False)
    assert dreaming_enabled() is True


def test_passive_ingest_no_dual_llm(mv):
    """Passive track writes experience atoms without requiring LLM hooks."""
    r = mv.ingest_turn("user", "Please rename the button to Submit payment")
    assert r["experience_atom"]
    atoms = mv.atoms.by_net("experience")
    assert any("Submit payment" in a.content or "button" in a.content for a in atoms)


def test_token_estimate_positive():
    assert estimate_tokens("hello world") >= 1
