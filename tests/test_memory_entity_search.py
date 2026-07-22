"""Gate 0 narrative atom_hit — entity-scoped search finds remember atoms.

Regression: MemorySystem.search(entity_id=A) must hit atoms written via
remember for A even after the process rebinds to entity B (no bind_entity).
Entity B must never surface A's marker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def ms(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    monkeypatch.setenv("WW_DREAMING_ENABLED", "0")
    from core.memory.system import MemorySystem

    system = MemorySystem(
        data_dir=str(tmp_path / "entity_search"),
        schedule_sleep_hour=-1,
        idle_threshold_minutes=0,
    )
    assert system.vnext is not None
    yield system
    if system.vnext is not None:
        try:
            system.vnext.close()
        except Exception:
            pass


def _blob(atoms) -> str:
    return json.dumps(
        [a.to_dict() if hasattr(a, "to_dict") else a for a in atoms],
        default=str,
    )


def test_remember_search_hits_with_entity_id_after_rebind(ms, monkeypatch):
    """Plant remember(value) under entity A → search(query=value, entity_id=A) hits.

    Critical: after set_entity(B) without bind_entity, explicit entity_id=A
    must still find the marker (live narrative prove path).
    """
    from core.memory.entity_scope import bind_entity
    from core.memory.tools import MemoryTools

    marker = "NARR-ENTITY-SEARCH-A99"
    eid_a = "entity_search_A"
    eid_b = "entity_search_B"

    with bind_entity(eid_a):
        ms.vnext.set_entity(eid_a)
        tools = MemoryTools(memory_system=ms, entity_id=eid_a)
        out = tools.remember(
            "favorite_color_code", marker, kind="outcome", is_core=False
        )
        assert out.get("success") is True or out.get("status") == "stored"
        assert out.get("entity_id") == eid_a

    # Process rebinds to B (simulates later /ww/run for another user)
    ms.vnext.set_entity(eid_b)
    assert ms.vnext._default_entity_id == eid_b

    # Prove-style search: entity_id only, no active bind_entity
    hits = ms.search(marker, limit=5, entity_id=eid_a)
    blob = _blob(hits)
    assert marker in blob, f"atom_hit=False after rebind: {blob[:500]}"
    assert any(marker in (a.content or "") for a in hits)

    # Also via labeled fact path / key fragment
    by_key = ms.search("favorite_color_code", limit=5, entity_id=eid_a)
    assert any(marker in (a.content or "") for a in by_key)


def test_search_entity_b_does_not_see_entity_a_marker(ms):
    """search(entity_id=B) must not surface entity A's remember marker."""
    from core.memory.entity_scope import bind_entity
    from core.memory.tools import MemoryTools

    only_a = "ONLY_A_MARKER_ENTITY_SEARCH"
    only_b = "ONLY_B_MARKER_ENTITY_SEARCH"
    eid_a = "iso_ent_A"
    eid_b = "iso_ent_B"

    with bind_entity(eid_a):
        ms.vnext.set_entity(eid_a)
        MemoryTools(memory_system=ms, entity_id=eid_a).remember(
            "secret", only_a, kind="outcome"
        )
    with bind_entity(eid_b):
        ms.vnext.set_entity(eid_b)
        MemoryTools(memory_system=ms, entity_id=eid_b).remember(
            "secret", only_b, kind="outcome"
        )

    # No bind — explicit entity_id only
    ms.vnext.set_entity("unrelated")
    hits_b = ms.search("ONLY_", limit=20, entity_id=eid_b)
    blob_b = _blob(hits_b)
    assert only_b in blob_b
    assert only_a not in blob_b

    hits_a = ms.search("ONLY_", limit=20, entity_id=eid_a)
    blob_a = _blob(hits_a)
    assert only_a in blob_a
    assert only_b not in blob_a


def test_recall_respects_entity_id_param(ms):
    """recall(..., entity_id=A) merges v-next hits for A after rebind to B."""
    from core.memory.entity_scope import bind_entity
    from core.memory.tools import MemoryTools

    marker = "RECALL-ENTITY-MARKER-77"
    eid = "recall_ent_A"
    with bind_entity(eid):
        ms.vnext.set_entity(eid)
        MemoryTools(memory_system=ms, entity_id=eid).remember(
            "code", marker, kind="outcome"
        )
    ms.vnext.set_entity("other")
    payload = ms.recall(marker, top_k=5, entity_id=eid)
    assert payload.get("vnext_hits", 0) >= 1
    assert marker in json.dumps(payload)
    assert payload.get("entity_id") == eid


def test_ingest_turn_stamps_entity_for_search(ms):
    """ingest_turn with entity_id stamps experience atoms findable by search."""
    eid = "ingest_ent_Z"
    marker = "INGEST-MARKER-Z42"
    ms.ingest_turn("user", f"Please note code {marker}", entity_id=eid)
    # After rebind, search still finds via entity_id
    ms.vnext.set_entity("default")
    hits = ms.search(marker, limit=5, entity_id=eid)
    assert any(marker in (a.content or "") for a in hits)


def test_reflex_entity_context_dump_not_preferred_over_remember(ms):
    """Polluted [reflex] Entity Context dumps must not be the only/search path."""
    from core.memory.entity_scope import bind_entity
    from core.memory.tools import MemoryTools

    marker = "CLEAN-REMEMBER-NOT-REFLEX"
    eid = "reflex_ent_1"
    with bind_entity(eid):
        ms.vnext.set_entity(eid)
        MemoryTools(memory_system=ms, entity_id=eid).remember(
            "fav", marker, kind="outcome"
        )
    # Pollute hippocampus with Entity Context reflex dump containing marker
    ms.store_text(
        f"[reflex] user=[Entity Context]\nKnown: noise\n\n[Current Request]\n"
        f"remember {marker} | reply=ok",
        source="reflex",
        entities=["reflex"],
    )
    ms.vnext.set_entity("other")
    hits = ms.search(marker, limit=5, entity_id=eid)
    blob = _blob(hits)
    assert marker in blob
    # Clean remember content (key: value) present; polluted dump filtered
    assert any(
        (a.content or "").startswith("fav:") or marker in (a.content or "")
        for a in hits
        if not (a.content or "").startswith("[reflex]")
    )
    assert not any(
        (a.content or "").startswith("[reflex]")
        and "[Entity Context]" in (a.content or "")
        for a in hits
    )
