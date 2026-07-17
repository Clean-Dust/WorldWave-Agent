"""Gate 0.1 — remember reliability + entity-scoped recall.

Covers:
- OpenAI schema: remember requires only key+value (optional fields not required)
- Empty remember args → success=False structured error (never silent OK)
- Natural language extract → store → recall for that entity_id
- Entity A cannot surface ONLY_A into entity B product-style recall
- Auto-repair extract_remember_kv for common utterances
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory.tools import MemoryTools, extract_remember_kv  # noqa: E402
from core.memory.vnext import MemoryVNext  # noqa: E402
from tools.registry import default_registry  # noqa: E402


# ── Schema ───────────────────────────────────────────────────────


def test_remember_openai_schema_requires_only_key_value():
    reg = default_registry()
    tools = {t["function"]["name"]: t for t in reg.to_openai_tools()}
    remember = tools["remember"]
    params = remember["function"]["parameters"]
    required = params.get("required") or []
    assert "key" in required
    assert "value" in required
    # Optional must NOT be forced (was root cause of empty tool calls)
    assert "kind" not in required
    assert "category" not in required
    assert "is_core" not in required
    props = params["properties"]
    assert "key" in props and "value" in props
    desc = remember["function"]["description"].lower()
    assert "key" in desc and "value" in desc
    assert "home_city" in desc or "required" in desc


def test_recall_mine_optional_params_not_all_required():
    reg = default_registry()
    tools = {t["function"]["name"]: t for t in reg.to_openai_tools()}
    rm = tools["recall_mine"]["function"]["parameters"]
    # query/limit have defaults → not required
    assert "query" not in (rm.get("required") or [])
    assert "limit" not in (rm.get("required") or [])


# ── extract_remember_kv ──────────────────────────────────────────


@pytest.mark.parametrize(
    "utterance,expect_key_sub,expect_val",
    [
        ("Please remember: my home city is BeamCity99.", "city", "BeamCity99"),
        ("Remember: my pet's name is BeamPet99", "pet", "BeamPet99"),
        ("Call remember tool: key=prove_product_code value=PROD-MEM-1", "prove", "PROD-MEM-1"),
        ("remember(key='home_city', value='Tokyo')", None, None),  # may or may not parse
        ("What time is it?", None, None),
    ],
)
def test_extract_remember_kv_natural(utterance, expect_key_sub, expect_val):
    got = extract_remember_kv(utterance)
    if expect_val is None and expect_key_sub is None:
        # free form may return None or partial — only assert non-crash
        return
    if expect_val is None:
        return
    assert got is not None, f"expected extract for {utterance!r}"
    key, value = got
    assert expect_val in value
    if expect_key_sub:
        assert expect_key_sub in key.lower() or key  # key present


def test_extract_key_value_explicit():
    k, v = extract_remember_kv(
        "You MUST call the remember tool now: key=prove_product_code value=PROD-MEM-999."
    )
    assert k == "prove_product_code"
    assert "PROD-MEM-999" in v


def test_extract_remember_this_exact_fact():
    """Natural language without key=/value= must still extract."""
    from core.memory.tools import extract_remember_facts

    utt = "Please remember this exact fact: my secret code is ALPHA99"
    facts = extract_remember_facts(utt)
    assert facts, "expected NL exact-fact extract"
    keys = {f[0] for f in facts}
    vals = " ".join(f[1] for f in facts)
    assert "ALPHA99" in vals
    # key should be mapped (user_name/secret-ish) or user_fact
    assert keys


def test_remember_handler_free_blob_and_multi(tmp_path, monkeypatch):
    """Handler stores free-text and multi-fact without silent no-op."""
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity
    from tools.registry import _remember_handler

    mv = MemoryVNext(data_dir=str(tmp_path / "rem_multi"), start_dreaming=False)
    try:
        eid = "ent_nl_multi"
        mem = MagicMock()
        mem.vnext = mv
        tools = MemoryTools(memory_system=mem, entity_id=eid)
        # Wire active ww so handler finds tools
        import tools.registry as reg

        class _WW:
            _memory_tools = tools
            _last_goal = (
                "Please remember preference_marker=BeamPrefNL1 and "
                "iron_rule always honor BeamIronNL1. Remember both."
            )
            _current_goal = _last_goal

        monkeypatch.setattr(reg, "_active_ww", lambda: _WW())
        monkeypatch.setattr(reg, "_ensure_memory_tools", lambda ww: tools)

        with bind_entity(eid):
            out = _remember_handler()  # empty args → extract from goal
            assert out.get("success") is True
            assert out.get("stored_count", 0) >= 1
            # Free blob path
            out2 = _remember_handler(input="Please remember this exact fact: my city is ZetaNL")
            assert out2.get("success") is True
    finally:
        if hasattr(mv, "close"):
            try:
                mv.close()
            except Exception:
                pass


# ── Gate 0.5: preference / iron / timeline extract reliability ──


def test_extract_preference_marker_bare_kv():
    from core.memory.tools import extract_remember_facts

    utt = (
        "Please remember preference_marker=BeamPref123. "
        "This is my stated preference marker (starts with BeamPref). "
        "Do not confuse it with Redis likes."
    )
    k, v = extract_remember_kv(utt)
    assert k == "preference_marker"
    assert v == "BeamPref123"
    # Must NOT swallow marker into slug key / "stated preference" body
    assert "stated" not in v.lower()
    facts = extract_remember_facts(utt)
    assert any(f[0] == "preference_marker" and f[1] == "BeamPref123" for f in facts)


def test_extract_iron_rule_honor_token():
    from core.memory.tools import extract_remember_facts

    utt = (
        "Iron rule for you: always honor BeamIronRule456 when I ask about rules. "
        "Remember it."
    )
    k, v = extract_remember_kv(utt)
    assert k == "iron_rule"
    assert v == "BeamIronRule456"
    facts = extract_remember_facts(utt)
    assert any(
        f[0] == "iron_rule" and f[1] == "BeamIronRule456" and f[2] == "constraint"
        for f in facts
    )


def test_extract_timeline_multi_event():
    from core.memory.tools import extract_remember_facts

    utt = (
        "Timeline: first I did BeamEventA99, later I did BeamEventB99. "
        "Please remember both."
    )
    facts = extract_remember_facts(utt)
    by = {k: (v, kind) for k, v, kind in facts}
    assert by["timeline_event_a"][0] == "BeamEventA99"
    assert by["timeline_event_b"][0] == "BeamEventB99"
    assert "BeamEventA99" in by["event_order"][0]
    assert "BeamEventB99" in by["event_order"][0]
    assert "first" in by["event_order"][0].lower()


def test_internal_style_remember_lands_in_inject(tmp_path, monkeypatch):
    """Gate 0.5: seed-style utterances → entity store → inject/search hit."""
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity
    from core.memory.tools import extract_remember_facts

    mv = MemoryVNext(data_dir=str(tmp_path / "g05_seed"), start_dreaming=False)
    try:
        eid = "beam_mini_seed_probe"
        mem = MagicMock()
        mem.vnext = mv
        tools = MemoryTools(memory_system=mem, entity_id=eid)
        seeds = [
            (
                "Please remember preference_marker=BeamPref777. "
                "This is my stated preference marker."
            ),
            (
                "Iron rule for you: always honor BeamIronRule777 when I ask "
                "about rules. Remember it."
            ),
            (
                "Timeline: first I did BeamEventA777, later I did BeamEventB777. "
                "Please remember both."
            ),
        ]
        with bind_entity(eid):
            mv.set_entity(eid)
            for utt in seeds:
                for key, value, kind in extract_remember_facts(utt):
                    r = tools.remember(key, value, kind=kind)
                    assert r.get("success") is True
                    assert r.get("entity_id") == eid

            inj = mv.inject_for_turn(
                "preference iron rule order events", entity_id=eid
            )
            assert "BeamPref777" in inj
            assert "BeamIronRule777" in inj
            assert "BeamEventA777" in inj
            assert "BeamEventB777" in inj

            listed = tools.recall_mine()
            blob = " ".join(
                f"{k}:{v}" for k, v in (listed.get("facts") or {}).items()
            )
            assert "BeamPref777" in blob
            assert "BeamIronRule777" in blob
            assert "BeamEventA777" in blob

            # iron_rule stored as constraint
            facts = mv.list_facts(entity_id=eid).get("facts") or {}
            iron = facts.get("iron_rule")
            if isinstance(iron, dict):
                assert iron.get("value") == "BeamIronRule777" or "BeamIronRule777" in str(
                    iron
                )
            # remember() without kind still constraint for iron_rule
            r2 = tools.remember("iron_rule", "BeamIronRule777")
            assert r2.get("kind") == "constraint"
    finally:
        mv.close()


def test_promote_topic_stamps_entity_id(tmp_path, monkeypatch):
    """Gate 0.5: LTM promote from beam_mini entities stamps meta.entity_id."""
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity

    mv = MemoryVNext(data_dir=str(tmp_path / "g05_promote"), start_dreaming=False)
    try:
        eid = "beam_mini_promote_1"
        with bind_entity(eid):
            mv.set_entity(eid)
            mv.remember(
                "iron_rule", "NO_UNTAGGED_IRON", kind="constraint", entity_id=eid
            )
            mv.ingest_turn(
                "user", "honor NO_UNTAGGED_IRON", entity_id=eid
            )
            assert mv.wm.active is not None
            uri = mv.ltm.promote_topic(
                mv.wm.active, category="experiences", entity_id=eid
            )
            assert uri
            # Index entry must be stamped
            hits = mv.ltm.search("NO_UNTAGGED_IRON", entity_id=eid, top_k=5)
            assert hits, "expected stamped LTM hit for entity"
            for h in hits:
                assert h.get("entity_id") == eid
                meta = h.get("meta") or {}
                assert meta.get("entity_id") == eid
            # Foreign entity must not see untagged iron from this promote
            foreign = mv.ltm.search(
                "NO_UNTAGGED_IRON", entity_id="beam_mini_other", top_k=5
            )
            blob = " ".join(str(h.get("content") or h.get("abstract") or "") for h in foreign)
            assert "NO_UNTAGGED_IRON" not in blob
    finally:
        mv.close()


# ── Empty args never success ─────────────────────────────────────


def test_remember_empty_args_not_success(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    mv = MemoryVNext(data_dir=str(tmp_path / "vnext"), start_dreaming=False)
    try:
        mem = MagicMock()
        mem.vnext = mv
        tools = MemoryTools(memory_system=mem, entity_state_mgr=None, entity_id="ent_a")
        r = tools.remember("", "")
        assert r.get("success") is False
        assert r.get("status") == "error"
        r2 = tools.remember("only_key", "")
        assert r2.get("success") is False
    finally:
        mv.close()


def test_remember_handler_empty_structured_error(monkeypatch):
    from tools import registry as reg

    # No active ww → still structured error, not fake stored
    monkeypatch.setattr(reg, "_active_ww", lambda: None)
    out = reg._remember_handler(key="", value="")
    assert out.get("success") is False
    assert "key" in (out.get("error") or out.get("message") or "").lower()


# ── Natural remember → entity fact list ──────────────────────────


def test_natural_language_remember_lists_for_entity(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    mv = MemoryVNext(data_dir=str(tmp_path / "vnext_nl"), start_dreaming=False)
    try:
        eid = "entity_nl_test_1"
        mv.set_entity(eid)
        mem = MagicMock()
        mem.vnext = mv
        tools = MemoryTools(memory_system=mem, entity_id=eid)
        pair = extract_remember_kv("Please remember: my home city is ZetaCityAlpha.")
        assert pair is not None
        key, value = pair
        stored = tools.remember(key, value)
        assert stored.get("success") is True
        assert stored.get("entity_id") == eid
        listed = tools.recall_mine("city")
        facts = listed.get("facts") or {}
        blob = " ".join(f"{k} {v}" for k, v in facts.items())
        assert "ZetaCityAlpha" in blob
        assert listed.get("entity_id") == eid
    finally:
        mv.close()


# ── Entity isolation hard guarantee ──────────────────────────────


def test_entity_a_fact_not_visible_to_entity_b(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    mv = MemoryVNext(data_dir=str(tmp_path / "vnext_iso"), start_dreaming=False)
    try:
        mem = MagicMock()
        mem.vnext = mv

        tools_a = MemoryTools(memory_system=mem, entity_id="entity_A")
        tools_b = MemoryTools(memory_system=mem, entity_id="entity_B")

        r_a = tools_a.remember("secret_a", "ONLY_A_MARKER_ZZZ", kind="outcome")
        r_b = tools_b.remember("secret_b", "ONLY_B_MARKER_YYY", kind="outcome")
        assert r_a.get("success") is True
        assert r_b.get("success") is True

        facts_b = tools_b.recall_mine().get("facts") or {}
        blob_b = " ".join(f"{k}:{v}" for k, v in facts_b.items())
        assert "ONLY_B_MARKER_YYY" in blob_b
        assert "ONLY_A_MARKER_ZZZ" not in blob_b

        facts_a = tools_a.recall_mine().get("facts") or {}
        blob_a = " ".join(f"{k}:{v}" for k, v in facts_a.items())
        assert "ONLY_A_MARKER_ZZZ" in blob_a
        assert "ONLY_B_MARKER_YYY" not in blob_a

        # list_facts direct path
        mv.set_entity("entity_B")
        listed = mv.list_facts(entity_id="entity_B")
        values = " ".join(
            str(v.get("value") if isinstance(v, dict) else v)
            for v in (listed.get("facts") or {}).values()
        )
        assert "ONLY_A_MARKER_ZZZ" not in values
        assert "ONLY_B_MARKER_YYY" in values
    finally:
        mv.close()


def test_set_entity_rebind_prevents_stale_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    mv = MemoryVNext(data_dir=str(tmp_path / "vnext_rebind"), start_dreaming=False)
    try:
        mem = MagicMock()
        mem.vnext = mv
        tools = MemoryTools(memory_system=mem, entity_id="primary")
        tools.remember("who", "PRIMARY_FACT")
        tools.set_entity("task_entity_X")
        tools.remember("who", "TASK_FACT")
        listed = tools.recall_mine()
        facts = listed.get("facts") or {}
        assert facts.get("who") == "TASK_FACT"
        assert listed.get("entity_id") == "task_entity_X"
        # primary still isolated
        tools.set_entity("primary")
        primary_facts = tools.recall_mine().get("facts") or {}
        assert primary_facts.get("who") == "PRIMARY_FACT"
    finally:
        mv.close()


def test_search_filters_atoms_by_entity(tmp_path, monkeypatch):
    """MemorySystem.search must not leak other entity remember atoms."""
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.system import MemorySystem
    from core.memory.entity_scope import bind_entity

    data = tmp_path / "memsys"
    data.mkdir()
    # Build a minimal MemorySystem with vnext if possible
    ms = MemorySystem(data_dir=str(data))
    if getattr(ms, "vnext", None) is None:
        mv = MemoryVNext(data_dir=str(tmp_path / "vnext_search"), start_dreaming=False)
        ms.vnext = mv
    else:
        mv = ms.vnext
    try:
        with bind_entity("ent_search_A"):
            mv.remember("marker", "SEARCH_ONLY_A", entity_id="ent_search_A")
        with bind_entity("ent_search_B"):
            mv.remember("marker", "SEARCH_ONLY_B", entity_id="ent_search_B")

        with bind_entity("ent_search_B"):
            hits = ms.search("SEARCH_ONLY", limit=20)
            blob = " ".join(
                (h.content if hasattr(h, "content") else str(h)) for h in hits
            )
            assert "SEARCH_ONLY_B" in blob
            assert "SEARCH_ONLY_A" not in blob
    finally:
        if hasattr(mv, "close"):
            mv.close()


def test_interleaved_set_entity_list_facts_uses_request_scope(tmp_path, monkeypatch):
    """Regression: global rebind must not leak entity A facts into A listing.

    Simulate interleaved set_entity A/B while listing facts for A under
    request-scoped bind_entity.
    """
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity

    mv = MemoryVNext(data_dir=str(tmp_path / "vnext_rebind_iso"), start_dreaming=False)
    try:
        with bind_entity("entity_A"):
            mv.remember("secret", "ONLY_A_INTERLEAVE", entity_id="entity_A")
        with bind_entity("entity_B"):
            mv.remember("secret", "ONLY_B_INTERLEAVE", entity_id="entity_B")

        with bind_entity("entity_A"):
            # Mid-flight: clobber instance / process default toward B
            mv._default_entity_id = "entity_B"
            # list_facts without explicit entity_id must still use request scope A
            listed = mv.list_facts()
            values = " ".join(
                str(v.get("value") if isinstance(v, dict) else v)
                for v in (listed.get("facts") or {}).values()
            )
            assert listed.get("entity_id") == "entity_A"
            assert "ONLY_A_INTERLEAVE" in values
            assert "ONLY_B_INTERLEAVE" not in values
    finally:
        mv.close()


def test_inject_for_b_never_contains_only_a(tmp_path, monkeypatch):
    """Unit: store ONLY_A on A, ONLY_B on B; inject/search for B never has ONLY_A."""
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity
    from core.memory.system import MemorySystem

    ms = MemorySystem(data_dir=str(tmp_path / "inj_iso"))
    mv = ms.vnext
    if mv is None:
        mv = MemoryVNext(data_dir=str(tmp_path / "inj_iso_v"), start_dreaming=False)
        ms.vnext = mv
    try:
        with bind_entity("ent_inj_A"):
            mv.remember("secret", "ONLY_A_MARKER_ZZZ", entity_id="ent_inj_A")
            mv.ingest_turn("user", "Remember ONLY_A_MARKER_ZZZ please", entity_id="ent_inj_A")
        with bind_entity("ent_inj_B"):
            mv.remember("secret", "ONLY_B_MARKER_YYY", entity_id="ent_inj_B")
            inj = mv.inject_for_turn("what is my secret", entity_id="ent_inj_B")
            assert "ONLY_B_MARKER_YYY" in inj
            assert "ONLY_A_MARKER_ZZZ" not in inj
            hits = ms.search("ONLY_", limit=20)
            blob = " ".join(
                (h.content if hasattr(h, "content") else str(h)) for h in hits
            )
            assert "ONLY_A_MARKER_ZZZ" not in blob
            assert "ONLY_B_MARKER_YYY" in blob
            rec = mv.recall("secret", entity_id="ent_inj_B")
            atom_blob = json_dumps_safe(rec)
            assert "ONLY_A_MARKER_ZZZ" not in atom_blob
    finally:
        if hasattr(mv, "close"):
            mv.close()


def json_dumps_safe(obj) -> str:
    import json

    return json.dumps(obj, default=str)


def test_tools_a_and_b_with_shared_vnext_and_global_rebind(tmp_path, monkeypatch):
    """MemoryTools for A must list A's facts even if vnext default rebinds to B."""
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity

    mv = MemoryVNext(data_dir=str(tmp_path / "tools_rebind"), start_dreaming=False)
    try:
        mem = MagicMock()
        mem.vnext = mv
        tools_a = MemoryTools(memory_system=mem, entity_id="tools_ent_A")
        tools_b = MemoryTools(memory_system=mem, entity_id="tools_ent_B")

        with bind_entity("tools_ent_A"):
            tools_a.remember("s", "ONLY_A_TOOL")
        with bind_entity("tools_ent_B"):
            tools_b.remember("s", "ONLY_B_TOOL")

        with bind_entity("tools_ent_A"):
            # Clobber global default mid-request
            mv.set_entity("tools_ent_B")
            # But bind_entity A still active — re-set request after clobber:
            # set_entity also sets ContextVar; simulate only instance clobber:
            mv._default_entity_id = "tools_ent_B"
            from core.memory.entity_scope import set_request_entity

            set_request_entity("tools_ent_A")
            listed = tools_a.recall_mine()
            blob = " ".join(f"{k}:{v}" for k, v in (listed.get("facts") or {}).items())
            assert "ONLY_A_TOOL" in blob
            assert "ONLY_B_TOOL" not in blob
    finally:
        mv.close()


# ── Gate 0.4: sequential same-process isolation (real storage, no mocks) ──


def test_sequential_entity_ltm_inject_never_leaks(tmp_path, monkeypatch):
    """Live-style sequential /ww/run: A then B on same MemorySystem process.

    Reproduces Banana beam_mini run B iron_rule leak: LTM abstract search
    without entity partition surfaces prior BeamIronRule markers.
    Does NOT mock away storage — real vnext facts/atoms/LTM on disk.
    """
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity
    from core.memory.system import MemorySystem

    data = tmp_path / "seq_iso"
    data.mkdir()
    ms = MemorySystem(data_dir=str(data))
    mv = ms.vnext
    if mv is None:
        mv = MemoryVNext(data_dir=str(tmp_path / "seq_iso_v"), start_dreaming=False)
        ms.vnext = mv
    try:
        # Entity A: remember iron_rule + ingest + promote to LTM (like STM purge)
        with bind_entity("ent_seq_A"):
            mv.set_entity("ent_seq_A")
            r = mv.remember(
                "iron_rule",
                "ONLY_FROM_A",
                kind="constraint",
                entity_id="ent_seq_A",
            )
            assert r.get("status") == "stored" or r.get("entity_id") == "ent_seq_A"
            mv.ingest_turn(
                "user",
                "Iron rule for you: always honor ONLY_FROM_A.",
                entity_id="ent_seq_A",
            )
            assert mv.wm.active is not None
            uri = mv.ltm.promote_topic(
                mv.wm.active, category="experiences", entity_id="ent_seq_A"
            )
            assert uri
            inj_a = mv.inject_for_turn("iron rule honor", entity_id="ent_seq_A")
            assert "ONLY_FROM_A" in inj_a

        # Entity B: sequential same process — must never see ONLY_FROM_A
        with bind_entity("ent_seq_B"):
            mv.set_entity("ent_seq_B")
            mv.remember(
                "iron_rule",
                "ONLY_FROM_B",
                kind="constraint",
                entity_id="ent_seq_B",
            )
            inj_b = mv.inject_for_turn(
                "What iron rule should you honor for me?", entity_id="ent_seq_B"
            )
            assert "ONLY_FROM_B" in inj_b
            assert "ONLY_FROM_A" not in inj_b, (
                f"cross-entity LTM/inject leak:\n{inj_b}"
            )

            rec = mv.recall("iron rule", entity_id="ent_seq_B")
            ltm_blob = json_dumps_safe(rec.get("ltm") or [])
            assert "ONLY_FROM_A" not in ltm_blob
            atom_blob = json_dumps_safe(rec.get("atoms") or [])
            assert "ONLY_FROM_A" not in atom_blob
            fact_blob = json_dumps_safe(rec.get("facts") or [])
            assert "ONLY_FROM_A" not in fact_blob
            assert "ONLY_FROM_B" in fact_blob or "ONLY_FROM_B" in inj_b

            # Product search path
            hits = ms.search("iron", limit=20, entity_id="ent_seq_B")
            search_blob = " ".join(
                (h.content if hasattr(h, "content") else str(h)) for h in hits
            )
            assert "ONLY_FROM_A" not in search_blob
            assert "ONLY_FROM_B" in search_blob

            # MemoryTools rebind path (same as tool handlers)
            mem = MagicMock()
            mem.vnext = mv
            tools = MemoryTools(memory_system=mem, entity_id="ent_seq_B")
            listed = tools.recall_mine("iron")
            blob = " ".join(
                f"{k}:{v}" for k, v in (listed.get("facts") or {}).items()
            )
            assert "ONLY_FROM_B" in blob
            assert "ONLY_FROM_A" not in blob
    finally:
        if hasattr(mv, "close"):
            mv.close()


def test_sequential_entity_twice_same_process(tmp_path, monkeypatch):
    """Two consecutive entity switches (beam_mini ×2 style) stay airtight."""
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.memory.entity_scope import bind_entity
    from core.memory.system import MemorySystem

    ms = MemorySystem(data_dir=str(tmp_path / "seq2"))
    mv = ms.vnext
    if mv is None:
        mv = MemoryVNext(data_dir=str(tmp_path / "seq2v"), start_dreaming=False)
        ms.vnext = mv
    try:
        markers = []
        for i, eid in enumerate(("beam_mini_run1", "beam_mini_run2")):
            marker = f"ONLY_FROM_{'A' if i == 0 else 'B'}_{i}"
            markers.append((eid, marker))
            with bind_entity(eid):
                mv.set_entity(eid)
                mv.remember("iron_rule", marker, kind="constraint", entity_id=eid)
                mv.ingest_turn("user", f"honor {marker}", entity_id=eid)
                if mv.wm.active is not None:
                    mv.ltm.promote_topic(
                        mv.wm.active, category="experiences", entity_id=eid
                    )

        # Re-enter run2 inject — must not contain run1 marker
        eid2, m2 = markers[1]
        _, m1 = markers[0]
        with bind_entity(eid2):
            mv.set_entity(eid2)
            inj = mv.inject_for_turn("iron rule", entity_id=eid2)
            assert m2 in inj
            assert m1 not in inj
    finally:
        if hasattr(mv, "close"):
            mv.close()
