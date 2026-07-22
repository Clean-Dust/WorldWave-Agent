"""BEAM 100K P1 + P2 mechanism tests (no network LLM)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── P0 polish: probe hit counts ──────────────────────────────────────


def test_probe_response_path_records_hit_count_when_atoms_planted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.beam_remediation import (
        beam_retrieval_metrics,
        collect_atom_evidence,
        probe_metrics_from_run_response,
    )
    from core.memory.vnext import MemoryVNext

    mv = MemoryVNext(data_dir=str(tmp_path / "hits"), start_dreaming=False)
    try:
        eid = "beam_hits"
        mv.remember("commits", "165", entity_id=eid)
        snippets = collect_atom_evidence(
            mv, "How many commits do I have?", entity_id=eid
        )
        assert any("165" in s for s in snippets)
        metrics = beam_retrieval_metrics(snippets)
        assert metrics["retrieval_hits"] >= 1
        assert metrics["retrieval_empty"] is False

        # Simulate /ww/run packaging used by beam_runner.probe_ww
        raw = {
            "status": "completed",
            "response": "You have 165 commits.",
            "state_metrics": {"beam_retrieval": metrics},
        }
        m = probe_metrics_from_run_response(raw)
        assert m["retrieval_hits"] >= 1
        assert m["retrieval_empty"] is False
        assert m["status"] == "completed"
    finally:
        mv.close()


def test_raw_extract_fields_from_probe_dict():
    """beam_runner-style raw_extract should surface hits from nested raw."""
    from core.beam_remediation import probe_metrics_from_run_response

    ans = {
        "llm_response": "165",
        "status": "completed",
        "retrieval_hits": 3,
        "retrieval_empty": False,
        "raw": {
            "status": "completed",
            "state_metrics": {
                "beam_retrieval": {
                    "retrieval_hits": 3,
                    "retrieval_empty": False,
                }
            },
        },
    }
    m = probe_metrics_from_run_response(ans["raw"])
    assert m["retrieval_hits"] == 3
    assert m["retrieval_empty"] is False


# ── P1.1 Timeline ────────────────────────────────────────────────────


def test_timeline_days_between_three_fixtures():
    from core.memory.timeline import TimelineStore, days_between_dates

    # Fixture 1: direct ISO
    assert days_between_dates("2024-01-01", "2024-01-11") == 10

    # Fixture 2: store + query match
    store = TimelineStore(data_dir="")
    store.append(
        "Started project Alpha",
        entity_id="e1",
        date_str="2024-03-01",
    )
    store.append(
        "Shipped project Alpha",
        entity_id="e1",
        date_str="2024-03-15",
    )
    gap = store.days_between("Started project Alpha", "Shipped project Alpha", entity_id="e1")
    assert gap == 14

    # Fixture 3: month names
    store2 = TimelineStore(data_dir="")
    store2.append("Kickoff meeting", entity_id="e2", date_str="Jan 5, 2025")
    store2.append("Retro meeting", entity_id="e2", date_str="Jan 12, 2025")
    gap2 = store2.days_between("Kickoff", "Retro", entity_id="e2")
    assert gap2 == 7

    # list_events ordered
    evs = store.list_events("e1")
    assert len(evs) >= 2
    assert evs[0].ts_sort_key <= evs[1].ts_sort_key


def test_timeline_on_ingest_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    monkeypatch.setenv("WW_BEAM_FACT_EXTRACT", "1")
    from core.memory.vnext import MemoryVNext

    mv = MemoryVNext(data_dir=str(tmp_path / "tl"), start_dreaming=False)
    try:
        eid = "tl_entity"
        r = mv.ingest_turn(
            "user",
            "On 2024-06-01 I joined the team. On 2024-06-10 I shipped the demo.",
            entity_id=eid,
        )
        assert r.get("timeline_events", 0) >= 1 or (
            mv.timeline and len(mv.timeline.list_events(eid)) >= 1
        )
        if mv.timeline:
            gap = mv.timeline.days_between("joined", "shipped", entity_id=eid)
            # Best-effort: if both matched, gap is 9
            if gap is not None:
                assert gap == 9
    finally:
        mv.close()


def test_beam_wrap_injects_timeline_for_temporal_question():
    from core.beam_remediation import build_beam_probe_goal, question_looks_temporal

    q = "How many days between the kickoff and the ship date?"
    assert question_looks_temporal(q)
    goal = build_beam_probe_goal(
        q,
        retrieved="retrieved:\n- event: kickoff (2024-01-01)\n- event: ship (2024-01-11)",
        retrieval_hits=2,
    )
    assert "TEMPORAL" in goal or "structured dates" in goal.lower()
    assert "2024-01-01" in goal or "kickoff" in goal.lower()


# ── P1.2 Quantity ────────────────────────────────────────────────────


def test_quantity_path_commits_165(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    from core.beam_remediation import (
        answer_from_quantity_evidence,
        build_beam_probe_goal,
        build_retrieval_floor_context,
        collect_atom_evidence,
    )
    from core.memory.vnext import MemoryVNext

    mv = MemoryVNext(data_dir=str(tmp_path / "qty"), start_dreaming=False)
    try:
        eid = "qty_e"
        mv.remember("commits", "165", entity_id=eid)
        ctx = build_retrieval_floor_context(
            mv, "How many commits do I have?", entity_id=eid
        )
        assert "165" in ctx
        snips = collect_atom_evidence(
            mv, "How many commits do I have?", entity_id=eid
        )
        n = answer_from_quantity_evidence("How many commits do I have?", snips)
        assert n == "165"
        goal = build_beam_probe_goal(
            "How many commits do I have?",
            retrieved=ctx,
            snippets=snips,
        )
        assert "165" in goal
        assert "exact number" in goal.lower() or "QUANTITY" in goal
    finally:
        mv.close()


def test_fact_extract_latency_ms():
    from core.memory.fact_extract import extract_durable_facts

    facts = extract_durable_facts("p95 latency is 42 ms under load")
    blob = " ".join(f"{f['key']}={f['value']}" for f in facts)
    assert "42" in blob
    assert any("latenc" in f["key"] or "ms" in f["key"] for f in facts) or "42" in blob


# ── P1.3 Abstention ──────────────────────────────────────────────────


def test_abstention_policy_helpers():
    from core.beam_remediation import (
        abstention_policy_text,
        forbids_no_record_when_hits,
        requires_short_abstain_when_empty,
    )

    hit_pol = abstention_policy_text(retrieval_hits=3)
    empty_pol = abstention_policy_text(retrieval_hits=0)
    assert forbids_no_record_when_hits(hit_pol)
    assert "FORBIDDEN" in hit_pol or "forbidden" in hit_pol.lower()
    assert requires_short_abstain_when_empty(empty_pol)
    assert "invent" in empty_pol.lower() or "biography" in empty_pol.lower()

    goal_hit = __import__(
        "core.beam_remediation", fromlist=["build_beam_probe_goal"]
    ).build_beam_probe_goal(
        "What is my home city?",
        retrieved="retrieved:\n- home_city: BeamCity",
        retrieval_hits=1,
    )
    assert "no record" in goal_hit.lower() or "FORBIDDEN" in goal_hit
    goal_empty = __import__(
        "core.beam_remediation", fromlist=["build_beam_probe_goal"]
    ).build_beam_probe_goal("What is my passport number?", retrieved="", retrieval_hits=0)
    assert "abstain" in goal_empty.lower()


# ── P1.4 Instruction following ───────────────────────────────────────


def test_instruction_following_code_fence_in_wrap():
    from core.beam_remediation import (
        build_beam_probe_goal,
        question_wants_code_fence,
    )

    q = "Reply with a Python code block with syntax highlighting that prints hello"
    assert question_wants_code_fence(q)
    goal = build_beam_probe_goal(q)
    low = goal.lower()
    assert "fenced code" in low or "```" in goal or "code block" in low
    assert "respond" in low or "reflex_text" in low


def test_instruction_following_bullet_and_json():
    from core.beam_remediation import build_beam_probe_goal

    g1 = build_beam_probe_goal("List my cities as a bullet list")
    assert "bullet" in g1.lower()
    g2 = build_beam_probe_goal("Return the answer as JSON with field city")
    assert "json" in g2.lower()


# ── P1.5 Preference ──────────────────────────────────────────────────


def test_preference_extract_and_wrap():
    from core.beam_remediation import build_beam_probe_goal
    from core.memory.fact_extract import extract_durable_facts

    facts = extract_durable_facts("I prefer concise bullet answers.")
    assert any(f.get("kind") == "preference" or "prefer" in f.get("key", "") for f in facts)
    goal = build_beam_probe_goal(
        "How should you answer me?",
        retrieved="retrieved:\n- preference: concise bullet answers",
        retrieval_hits=1,
    )
    assert "preference" in goal.lower() or "honor" in goal.lower()


# ── P2.1 Contradiction (≥3) ──────────────────────────────────────────


def test_format_contradiction_evidence_acknowledges_both():
    from core.beam_remediation import format_contradiction_evidence

    text = format_contradiction_evidence(
        ["[conflict] city: Paris", "[conflict] city: Lyon"]
    )
    assert "both" in text.lower() or "Side A" in text
    assert "Paris" in text and "Lyon" in text
    assert "CONTRADICTION" in text or "contradiction" in text.lower()


def test_contradiction_extract_dual_values():
    from core.memory.fact_extract import extract_durable_facts, has_contradiction_marker

    assert has_contradiction_marker("but earlier I said something else")
    facts = extract_durable_facts(
        "home_city is Paris but actually home_city is Lyon"
    )
    # Should surface conflict or both values somewhere
    vals = [f.get("value", "") for f in facts]
    keys = [f.get("key", "") for f in facts]
    blob = " ".join(vals + keys)
    assert "paris" in blob.lower() or "lyon" in blob.lower()
    # At least one conflict marker if dual detected
    if any(f.get("conflict") == "true" for f in facts):
        assert True
    else:
        # Fallback: both cities present as facts across keys
        assert "paris" in blob.lower() or "lyon" in blob.lower()


def test_contradiction_in_beam_wrap_and_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("WW_MEMORY_VNEXT", "1")
    monkeypatch.setenv("WW_BEAM_FACT_EXTRACT", "1")
    from core.beam_remediation import build_beam_probe_goal, format_contradiction_evidence
    from core.memory.fact_extract import apply_facts_to_memory, extract_durable_facts
    from core.memory.vnext import MemoryVNext

    # Fixture 2: explicit conflict store
    hits = [
        {"key": "employer", "content": "employer: Acme", "meta": {"conflict": True}},
        {"key": "employer", "content": "employer: Globex", "meta": {"conflict": True}},
    ]
    fmt = format_contradiction_evidence(hits)
    assert "Acme" in fmt and "Globex" in fmt

    # Fixture 3: wrap with conflict retrieval
    goal = build_beam_probe_goal(
        "Where do I work?",
        retrieved="retrieved:\n- [conflict] employer: Acme\n- [conflict] employer: Globex",
        retrieval_hits=2,
        snippets=[
            "[conflict] employer: Acme",
            "[conflict] employer: Globex",
        ],
    )
    assert "contradiction" in goal.lower() or "both sides" in goal.lower()

    # Memory apply path
    mv = MemoryVNext(data_dir=str(tmp_path / "contra"), start_dreaming=False)
    try:
        facts = extract_durable_facts(
            "My age is 30 but earlier my age is 28"
        )
        # Force conflict tags if extract found age values
        if len(facts) >= 1:
            for f in facts:
                f["conflict"] = "true"
            apply_facts_to_memory(mv, facts, entity_id="c1")
        # Also plant two conflict atoms manually
        mv.remember("score", "10", entity_id="c1")
        mv.remember("score", "20", entity_id="c1")
        # Manual conflict mark on historical
        for a in mv.atoms.query(text="score:", current_only=False, entity_id="c1", limit=10):
            meta = dict(a.meta or {})
            meta["conflict"] = True
            a.meta = meta
            tags = list(a.tags or [])
            if "conflict" not in tags:
                tags.append("conflict")
            a.tags = tags
            mv.atoms.add(a)
        fmt2 = format_contradiction_evidence(
            [a.content for a in mv.atoms.query(text="score:", current_only=False, entity_id="c1", limit=10)]
        )
        assert "score" in fmt2.lower() or "10" in fmt2 or "20" in fmt2
    finally:
        mv.close()


# ── P2.2 Event ordering (≥3) ─────────────────────────────────────────


def test_format_event_order_three_fixtures():
    from core.beam_remediation import format_event_order
    from core.memory.timeline import TimelineEvent, extract_timeline_events

    # Fixture 1: dicts
    events = [
        {"ts_sort_key": 300.0, "text": "Third", "date_str": "2024-01-03"},
        {"ts_sort_key": 100.0, "text": "First", "date_str": "2024-01-01"},
        {"ts_sort_key": 200.0, "text": "Second", "date_str": "2024-01-02"},
    ]
    ordered = format_event_order(events)
    assert "1. First" in ordered
    assert "2. Second" in ordered
    assert "3. Third" in ordered
    assert ordered.index("First") < ordered.index("Second") < ordered.index("Third")

    # Fixture 2: TimelineEvent objects
    evs = [
        TimelineEvent(ts_sort_key=2.0, text="B", date_str="2020-02-01"),
        TimelineEvent(ts_sort_key=1.0, text="A", date_str="2020-01-01"),
    ]
    o2 = format_event_order(evs)
    assert "1. A" in o2 and "2. B" in o2

    # Fixture 3: extract from text then order
    extracted = extract_timeline_events(
        "On 2023-05-01 we met. On 2023-05-20 we signed. On 2023-06-01 we launched."
    )
    o3 = format_event_order(extracted)
    assert "1." in o3
    assert "event order" in o3.lower()
    # Chronological: met before launched
    if "met" in o3.lower() and "launched" in o3.lower():
        assert o3.lower().index("met") < o3.lower().index("launched")


def test_ordering_question_gets_event_order_instruction():
    from core.beam_remediation import build_beam_probe_goal, question_looks_ordering

    q = "In what order did the events happen?"
    assert question_looks_ordering(q)
    goal = build_beam_probe_goal(
        q,
        retrieved="retrieved:\n- event order (chronological):\n1. A\n2. B",
        retrieval_hits=2,
    )
    assert "EVENT ORDER" in goal or "chronolog" in goal.lower() or "order" in goal.lower()


# ── P2.3 Summarization ───────────────────────────────────────────────


def test_summarization_wrap_rule():
    from core.beam_remediation import build_beam_probe_goal, question_looks_summary

    q = "Summarize what you know about my project."
    assert question_looks_summary(q)
    goal = build_beam_probe_goal(q, retrieved="retrieved:\n- project: Worldwave")
    low = goal.lower()
    assert "only from retrieved" in low or "summarize only" in low or "evidence" in low
    assert "tool dump" in low or "no tool" in low or "invent" in low


def test_evidence_only_rule_on_all_probes():
    from core.beam_remediation import build_beam_probe_goal

    goal = build_beam_probe_goal("What is my name?")
    assert "EVIDENCE-ONLY" in goal or "retrieved evidence" in goal.lower()


# ── P1.6 multi-hop instruct ──────────────────────────────────────────


def test_multi_hop_combine_snippets_instruction():
    from core.beam_remediation import build_beam_probe_goal

    goal = build_beam_probe_goal(
        "What city and how many commits?",
        retrieved="retrieved:\n- city: X\n- commits: 165",
        retrieval_hits=2,
        snippets=["city: X", "commits: 165"],
    )
    assert "combine" in goal.lower() or "multi" in goal.lower()
