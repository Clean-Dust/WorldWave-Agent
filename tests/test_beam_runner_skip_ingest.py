"""Tests: BEAM resume skips WW ingest when all expected probes are already done."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from beam.data import BeamChat, ProbeItem  # noqa: E402


def _minimal_chat(tmp_path: Path, *, n_probes: int = 2) -> BeamChat:
    probes = [
        ProbeItem(
            ability="information_extraction",
            index=i,
            question=f"Q{i}?",
            ideal=f"A{i}",
            rubric=[f"A{i}"],
        )
        for i in range(n_probes)
    ]
    return BeamChat(
        scale="100K",
        chat_id="skip1",
        path=tmp_path,
        turns=[{"role": "user", "content": "My city is SkipCity."}],
        probes=probes,
    )


def _call_kwargs(tmp_path: Path, chat: BeamChat, done: set, client) -> dict:
    return dict(
        system="ww",
        chat=chat,
        entity_id="beam_100K_skip1_test",
        client=client,
        dry_run=False,
        max_abilities=0,
        abilities=None,
        done=done,
        answers_path=tmp_path / "answers_ww.jsonl",
        seed=1,
        run_tag="skip_test",
        ingest_mode="turn",
        batch_n=5,
        max_turns=0,
        b1_max_chars=1000,
        b2_top_k=3,
        judge_model="test-judge",
        use_llm_judge=False,
        answer_model="",
    )


def test_skip_ingest_when_all_probes_done(tmp_path: Path, monkeypatch, capsys):
    """Case A: done contains all expected keys → no client.run (no ingest, no probe)."""
    import beam_runner as br

    chat = _minimal_chat(tmp_path, n_probes=2)
    expected = br.expected_probe_keys("ww", chat)
    assert len(expected) == 2
    done = set(expected)

    client = MagicMock()
    client.run = MagicMock(return_value={"status": "ok", "response": "x"})

    # Guard: if probe path were reached, fail hard
    monkeypatch.setattr(
        br,
        "probe_ww",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe_ww must not run")),
    )
    monkeypatch.setattr(
        br,
        "ingest_ww",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ingest_ww must not run")),
    )

    rows = br.run_system_on_chat(**_call_kwargs(tmp_path, chat, done, client))
    assert rows == []
    assert client.run.call_count == 0
    out = capsys.readouterr().out
    assert "skip_ingest=1" in out
    assert "probes_complete" in out


def test_ingest_when_one_probe_missing(tmp_path: Path, monkeypatch, capsys):
    """Case B: one expected key missing → ingest once; only missing probe attempted."""
    import beam_runner as br

    chat = _minimal_chat(tmp_path, n_probes=2)
    expected = sorted(br.expected_probe_keys("ww", chat))
    assert len(expected) == 2
    # Leave the second key incomplete
    done = {expected[0]}

    client = MagicMock()
    client.run = MagicMock(return_value={"status": "ok", "response": "partial"})

    ingest_calls: list = []

    def _fake_ingest(c, ch, eid, **kw):
        ingest_calls.append({"entity_id": eid, "dry_run": kw.get("dry_run")})
        # Simulate one packed goal call like real ingest
        c.run("ingest goal", entity_id=eid)
        return {"entity_id": eid, "goals_sent": 1, "dry_run": False}

    monkeypatch.setattr(br, "ingest_ww", _fake_ingest)
    monkeypatch.setattr(
        br,
        "probe_ww",
        lambda *a, **k: {
            "llm_response": "SkipCity",
            "raw": {"status": "ok"},
            "status": "ok",
        },
    )

    rows = br.run_system_on_chat(**_call_kwargs(tmp_path, chat, done, client))
    assert len(ingest_calls) == 1
    assert client.run.call_count >= 1  # from fake ingest
    assert len(rows) == 1
    assert rows[0]["key"] == expected[1]
    assert expected[1] in done
    out = capsys.readouterr().out
    assert "skip_ingest=0" in out


def test_dry_run_skips_ingest_when_probes_complete(tmp_path: Path, capsys):
    """Dry-run path also skips ingest when probes already complete."""
    import beam_runner as br

    chat = _minimal_chat(tmp_path, n_probes=1)
    expected = br.expected_probe_keys("ww", chat)
    done = set(expected)

    client = MagicMock()
    client.run = MagicMock()

    kwargs = _call_kwargs(tmp_path, chat, done, client)
    kwargs["dry_run"] = True
    rows = br.run_system_on_chat(**kwargs)
    assert rows == []
    assert client.run.call_count == 0
    out = capsys.readouterr().out
    assert "skip_ingest=1" in out


def test_empty_expected_does_not_skip_ingest(tmp_path: Path, monkeypatch):
    """No probes / empty expected → probes_complete is False; live WW still ingests."""
    import beam_runner as br

    chat = BeamChat(
        scale="100K",
        chat_id="empty",
        path=tmp_path,
        turns=[{"role": "user", "content": "hi"}],
        probes=[],
    )
    expected = br.expected_probe_keys("ww", chat)
    assert expected == set()

    ingest_calls: list = []

    def _fake_ingest(*a, **k):
        ingest_calls.append(1)
        return {"dry_run": False}

    monkeypatch.setattr(br, "ingest_ww", _fake_ingest)
    client = MagicMock()

    kwargs = _call_kwargs(tmp_path, chat, set(), client)
    kwargs["chat"] = chat
    rows = br.run_system_on_chat(**kwargs)
    assert rows == []
    assert len(ingest_calls) == 1
