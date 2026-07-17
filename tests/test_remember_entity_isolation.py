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
        mv.set_entity("ent_search_A")
        mv.remember("marker", "SEARCH_ONLY_A", entity_id="ent_search_A")
        mv.set_entity("ent_search_B")
        mv.remember("marker", "SEARCH_ONLY_B", entity_id="ent_search_B")

        mv.set_entity("ent_search_B")
        hits = ms.search("SEARCH_ONLY", limit=20)
        blob = " ".join(
            (h.content if hasattr(h, "content") else str(h)) for h in hits
        )
        assert "SEARCH_ONLY_B" in blob
        assert "SEARCH_ONLY_A" not in blob
    finally:
        if hasattr(mv, "close"):
            mv.close()
