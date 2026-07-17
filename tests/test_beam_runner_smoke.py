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


def test_goal_char_budget_constant():
    import beam_runner as br

    assert br.GOAL_CHAR_BUDGET <= 1800
    assert br.GOAL_CHAR_BUDGET < br.API_GOAL_MAX_CHARS


def test_pack_batch_n_flushes_on_char_budget():
    """batch_n mode packs by character budget, not only turn count."""
    import beam_runner as br

    budget = 400
    # Each turn ~120 chars of content; batch_n=10 would overfill budget.
    turns = [
        {"role": "user", "content": f"fact-{i:02d} " + ("x" * 100)}
        for i in range(8)
    ]
    goals = br.pack_ingest_goals(
        turns, ingest_mode="batch_n", batch_n=10, budget=budget
    )
    assert len(goals) >= 2  # must flush early due to chars
    assert all(len(g) <= budget for g in goals)
    # All facts preserved (no silent truncate)
    blob = "\n".join(goals)
    for i in range(8):
        assert f"fact-{i:02d}" in blob


def test_pack_batch_n_respects_turn_count():
    import beam_runner as br

    turns = [
        {"role": "user", "content": f"short {i}"}
        for i in range(7)
    ]
    goals = br.pack_ingest_goals(
        turns, ingest_mode="batch_n", batch_n=3, budget=1800
    )
    # 7 turns / batch_n=3 => 3 goals (3+3+1)
    assert len(goals) == 3
    assert all(len(g) <= 1800 for g in goals)


def test_pack_batch_n_hard_splits_oversized_turn():
    import beam_runner as br

    budget = 300
    marker_a = "ALPHA_UNIQUE_TOKEN"
    marker_b = "BETA_UNIQUE_TOKEN"
    long = marker_a + ("Z" * 900) + marker_b
    turns = [
        {"role": "user", "content": "tiny"},
        {"role": "user", "content": long},
        {"role": "assistant", "content": "ok"},
    ]
    goals = br.pack_ingest_goals(
        turns, ingest_mode="batch_n", batch_n=5, budget=budget
    )
    assert all(len(g) <= budget for g in goals)
    split_goals = [g for g in goals if "[part " in g]
    assert len(split_goals) >= 2
    assert any("[part 1/" in g for g in split_goals)
    blob = "\n".join(goals)
    assert marker_a in blob
    assert marker_b in blob
    # Reconstruct body parts: every char of long line must appear
    assert "Z" * 900 in blob.replace("\n", "") or blob.count("Z") >= 900


def test_pack_turn_mode_hard_splits_and_no_silent_truncate():
    import beam_runner as br

    budget = 250
    secret = "PASSPORT_NO_XY-999-SECRET"
    body = ("prefix " * 20) + secret + (" suffix" * 30)
    turns = [
        {"role": "user", "content": body},
        {"role": "assistant", "content": body},
    ]
    goals = br.pack_ingest_goals(turns, ingest_mode="turn", budget=budget)
    assert len(goals) >= 2
    assert all(len(g) <= budget for g in goals)
    blob = "\n".join(goals)
    assert secret in blob
    assert any("[part " in g for g in goals)
    # Assistant path keeps note header and still splits (no content[:2000])
    assert any(br.ASSISTANT_NOTE_HEADER[:40] in g for g in goals)


def test_hard_split_goal_preserves_full_body():
    import beam_runner as br

    header = "HDR:\n"
    body = "".join(f"[{i:04d}]" for i in range(200))  # 1200 chars
    budget = 200
    parts = br.hard_split_goal(header, body, budget)
    assert len(parts) > 1
    assert all(len(p) <= budget for p in parts)
    # Strip headers and part labels; concatenated chunks == original body
    rebuilt = []
    for p in parts:
        assert p.startswith(header)
        rest = p[len(header) :]
        assert rest.startswith("[part ")
        nl = rest.index("\n")
        rebuilt.append(rest[nl + 1 :])
    assert "".join(rebuilt) == body


def test_ingest_ww_uses_packed_goals(tmp_path: Path):
    """ingest_ww sends only budget-safe goals via client.run (no network)."""
    import beam_runner as br
    from beam.data import BeamChat

    sent: list = []

    class FakeClient:
        def run(self, goal, **kwargs):
            sent.append(goal)
            return {"status": "ok"}

    long = "FACT_KEEP_ME " + ("w" * 2500)
    chat = BeamChat(
        scale="100K",
        chat_id="pack",
        path=tmp_path,
        turns=[{"role": "user", "content": long}],
        probes=[],
    )
    out = br.ingest_ww(
        FakeClient(),  # type: ignore[arg-type]
        chat,
        "beam_test_entity",
        ingest_mode="batch_n",
        batch_n=5,
    )
    assert out["goals_sent"] == len(sent)
    assert out["turns_ingested"] == 1
    assert sent
    assert all(len(g) <= br.GOAL_CHAR_BUDGET for g in sent)
    assert all(len(g) <= br.API_GOAL_MAX_CHARS for g in sent)
    assert "FACT_KEEP_ME" in "\n".join(sent)


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


def test_simple_llm_answer_builds_messages(monkeypatch):
    """_simple_llm_answer must construct LLMClient(model=...) and chat(messages=...)."""
    import types

    import beam_runner as br

    captured: dict = {}

    class FakeClient:
        def __init__(self, model="default", **kwargs):
            captured["model"] = model
            captured["kwargs"] = kwargs

        def chat(self, messages, phase="", json_mode=True, temperature=None, **kw):
            captured["messages"] = messages
            captured["phase"] = phase
            captured["json_mode"] = json_mode
            captured["temperature"] = temperature
            return "ok-answer"

    fake_mod = types.ModuleType("core.llm")
    fake_mod.LLMClient = FakeClient
    fake_mod.DEFAULT_MODEL = "test-default-model"
    monkeypatch.setitem(sys.modules, "core.llm", fake_mod)
    monkeypatch.delenv("WW_BEAM_ANSWER_MODEL", raising=False)
    monkeypatch.delenv("WW_MODEL", raising=False)
    br._ENV_LOADED = True  # skip dotenv side effects

    out = br._simple_llm_answer("hello probe", json_mode=False, model="my-answer-model")
    assert out == "ok-answer"
    assert captured["model"] == "my-answer-model"
    assert captured["messages"] == [{"role": "user", "content": "hello probe"}]
    assert captured["phase"] == ""
    assert captured["json_mode"] is False
    assert captured["temperature"] == 0.0

    out_j = br._simple_llm_answer("judge me", json_mode=True, model="judge-model")
    assert out_j == "ok-answer"
    assert captured["json_mode"] is True
    assert captured["model"] == "judge-model"
    assert captured["messages"] == [{"role": "user", "content": "judge me"}]


def test_simple_llm_answer_failure_returns_empty(monkeypatch):
    import types

    import beam_runner as br

    class BoomClient:
        def __init__(self, model="x", **kwargs):
            pass

        def chat(self, *a, **k):
            raise RuntimeError("no network")

    fake_mod = types.ModuleType("core.llm")
    fake_mod.LLMClient = BoomClient
    fake_mod.DEFAULT_MODEL = "x"
    monkeypatch.setitem(sys.modules, "core.llm", fake_mod)
    br._ENV_LOADED = True
    assert br._simple_llm_answer("q") == ""

def test_worst_case_rows_from_real_scores():
    import beam_runner as br

    rows = [
        {
            "system": "b1",
            "chat_id": "1",
            "ability": "abstention",
            "probe_index": 0,
            "question": "q0",
            "llm_response": "bad",
            "judgment": {"score": 0.9, "pass": True, "rationale": "high"},
        },
        {
            "system": "b1",
            "chat_id": "1",
            "ability": "information_extraction",
            "probe_index": 0,
            "question": "q1",
            "llm_response": "worse",
            "judgment": {"score": 0.1, "pass": False, "rationale": "low"},
        },
        {
            "system": "b2",
            "chat_id": "2",
            "ability": "summarization",
            "probe_index": 1,
            "question": "q2",
            "llm_response": "mid",
            "judgment": {"score": 0.4, "pass": False, "rationale": "mid"},
        },
    ]
    worst = br.worst_case_rows(rows, n=2)
    assert len(worst) == 2
    assert worst[0]["judgment"]["score"] == 0.1
    assert worst[1]["judgment"]["score"] == 0.4


def test_write_summary_worst_cases_not_placeholder(tmp_path: Path):
    import beam_runner as br

    rows = [
        {
            "system": "b1",
            "chat_id": "9",
            "ability": "abstention",
            "probe_index": 0,
            "question": "What is my passport?",
            "llm_response": "I invent a number",
            "judgment": {
                "score": 0.0,
                "pass": False,
                "rationale": "hallucinated passport",
            },
        }
    ]
    meta = {
        "git_sha": "deadbeef",
        "seed": 1,
        "judge_model": "test",
        "judge_temp": 0.0,
        "answer_model": "test-ans",
        "protocol_complete": False,
        "official_claim": False,
    }
    br.write_summary(tmp_path, "100K", ["b1"], rows, meta)
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Placeholder" not in text
    assert "Worst cases" in text
    assert "hallucinated passport" in text
    assert "protocol_complete: **false**" in text
    assert "official_claim: **false**" in text
    meta_out = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta_out["official_claim"] is False
