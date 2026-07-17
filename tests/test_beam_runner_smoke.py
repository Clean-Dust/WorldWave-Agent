"""Smoke tests for official BEAM runner skeleton (no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from beam.baselines import SimpleBM25, b1_context_prompt, b2_rag_prompt, chunk_text  # noqa: E402
from beam.data import (  # noqa: E402
    chat_text_blob,
    list_chat_ids,
    load_chat,
    resolve_data_root,
)
from beam.judge import heuristic_score, judge_one  # noqa: E402


def _sample_root() -> Path:
    """Prefer /tmp/BEAM-data smoke fixture; else build tiny synthetic tree."""
    p = Path("/tmp/BEAM-data")
    if (p / "chats" / "100K" / "1" / "chat.json").is_file():
        return p
    return Path("")  # synthetic path handled in fixture


@pytest.fixture
def beam_root(tmp_path: Path) -> Path:
    real = Path("/tmp/BEAM-data")
    if (real / "chats" / "100K" / "1" / "chat.json").is_file():
        return real
    # Minimal synthetic official layout
    chat_dir = tmp_path / "chats" / "100K" / "1"
    pq = chat_dir / "probing_questions"
    pq.mkdir(parents=True)
    chat = [
        {
            "batch_number": 1,
            "turns": [
                [
                    {"role": "user", "content": "My home city is BeamCitySmoke."},
                    {
                        "role": "assistant",
                        "content": "Got it, home city BeamCitySmoke.",
                    },
                ]
            ],
        }
    ]
    (chat_dir / "chat.json").write_text(json.dumps(chat), encoding="utf-8")
    probes = {
        "abstention": [
            {
                "question": "What is my passport number?",
                "ideal_response": "No passport information in the chat.",
                "rubric": ["no passport", "not available"],
            }
        ],
        "information_extraction": [
            {
                "question": "What is my home city?",
                "answer": "BeamCitySmoke",
                "rubric": ["BeamCitySmoke"],
            }
        ],
    }
    (pq / "probing_questions.json").write_text(
        json.dumps(probes), encoding="utf-8"
    )
    return tmp_path


def test_resolve_data_root_isolation(beam_root: Path, monkeypatch):
    monkeypatch.setenv("WW_BEAM_DATA", str(beam_root))
    root = resolve_data_root()
    assert root == beam_root.resolve()
    # Explicit override wins
    other = beam_root / "other"
    other.mkdir(exist_ok=True)
    assert resolve_data_root(other) == other.resolve()


def test_list_and_load_chat(beam_root: Path):
    ids = list_chat_ids("100K", beam_root)
    assert "1" in ids
    chat = load_chat("100K", "1", beam_root)
    assert chat.chat_id == "1"
    assert chat.scale == "100K"
    assert len(chat.turns) >= 1
    assert any(t["role"] == "user" for t in chat.turns)
    assert len(chat.probes) >= 1
    blob = chat_text_blob(chat)
    assert "USER:" in blob or "user" in blob.lower() or len(blob) > 0


def test_entity_path_isolation():
    import beam_runner as br

    a = br.entity_for("100K", "1", "tagA")
    b = br.entity_for("100K", "1", "tagB")
    c = br.entity_for("100K", "2", "tagA")
    assert a != b
    assert a != c
    assert a.startswith("beam_100K_1_")


def test_baselines_bm25_and_prompts():
    chunks = chunk_text("alpha beta gamma " * 50, chunk_chars=40, overlap=5)
    assert len(chunks) >= 2
    bm = SimpleBM25(["cats sit on mats", "dogs chase cars", "beam city smoke"])
    top = bm.top_k("beam city", k=1)
    assert top[0][0] == 2
    p1 = b1_context_prompt("Where?", "USER: hello", max_chars=100)
    assert "QUESTION" in p1
    p2 = b2_rag_prompt("beam", "USER: beam city smoke\nASSISTANT: ok", top_k=2)
    assert "RETRIEVED" in p2 or "QUESTION" in p2


def test_judge_heuristic_empty_fails():
    j = heuristic_score("abstention", "q", "ideal passport", ["passport"], "")
    assert j["pass"] is False
    assert j["official"] is False


def test_judge_one_no_llm():
    j = judge_one(
        "information_extraction",
        "city?",
        "BeamCitySmoke",
        ["BeamCitySmoke"],
        "Your home city is BeamCitySmoke.",
        llm_chat=None,
    )
    assert "score" in j
    assert j.get("official") is False


def test_beam_runner_dry_run(beam_root: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WW_BEAM_DATA", str(beam_root))
    # Keep results under repo results/ — runner writes there; use dry-run
    import beam_runner as br

    rc = br.main(
        [
            "--scale",
            "100K",
            "--systems",
            "b1,b2",
            "--chat",
            "1",
            "--dry-run",
            "--max-abilities",
            "1",
            "--data-root",
            str(beam_root),
            "--run-tag",
            "smoke_test",
            "--seed",
            "1",
        ]
    )
    assert rc == 0


def test_public_reply_rejects_metrics_dict():
    from core.public_reply import extract_user_response, is_metrics_dump

    metrics = {
        "session_id": "72cbe186ee47",
        "active_interrupts": 1,
        "interrupt_history": [
            {"reason": "rewind: phase 0 repeated 68 times", "phase": "learn"}
        ],
        "total_checkpoints": 5,
        "current_spiral": 194,
    }
    assert is_metrics_dump(metrics) is True
    assert is_metrics_dump(json.dumps(metrics)) is True
    result = {
        "status": "completed",
        "response": "",
        "summary": metrics,
        "results": [],
        "spirals_completed": 0,
    }
    assert extract_user_response(result) == ""
