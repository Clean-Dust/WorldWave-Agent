"""Gate 0 — BEAM-mini honest scoring helpers + public_reply dump gates.

No live server required. Imports scoring from scripts/beam_mini_prove.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from core.public_reply import extract_user_response, is_dump_like_text  # noqa: E402

import beam_mini_prove as bmp  # noqa: E402


def test_abstention_score_refuses_unknown():
    ok = bmp.score_abstention(
        "I don't have your blood type or passport in memory."
    )
    assert ok is True


def test_abstention_score_unicode_apostrophe():
    """Gate 0.5: curly apostrophe in don't/can't must not false-negative."""
    # ASCII control
    assert bmp.score_abstention("I don't know your blood type...") is True
    # U+2019 RIGHT SINGLE QUOTATION MARK (common in model output)
    curly = "I don\u2019t have your blood type..."
    assert "\u2019" in curly
    assert bmp.score_abstention(curly) is True
    assert bmp.score_abstention("I can\u2019t answer that — not in memory.") is True
    # Other typographic forms
    assert bmp.score_abstention("I don\u2018t know your passport number.") is True
    assert bmp.score_abstention("I don\u02bbt know that fact.") is True


def test_contradiction_score_unicode_apostrophe():
    """Gate 0.5: contradiction cues still match after apostrophe normalize."""
    assert (
        bmp.score_contradiction(
            "You said you like Redis and also that you hate it — which is true?"
        )
        is True
    )
    # Curly apostrophe in "don't" + both seeded markers + which-is-true cue
    text = (
        f"I don\u2019t know which is true: {bmp.CONTRA_A} vs {bmp.CONTRA_B}."
    )
    assert bmp.score_contradiction(text) is True


def test_abstention_score_accepts_truly_know_adverb():
    """Gate 0.3: middle adverb must not false-negative honest refuse."""
    assert (
        bmp.score_abstention("I don't truly know your blood type or passport number.")
        is True
    )
    assert bmp.score_abstention("I do not truly know that.") is True
    assert bmp.score_abstention("I never provided my blood type.") is True
    assert bmp.score_abstention("I never saved a passport number.") is True
    assert bmp.score_abstention("I can't answer that — not in memory.") is True


def test_abstention_score_rejects_dump():
    dump = "home_city: ZetaCity\npet_name: ZetaPet"
    assert bmp.score_abstention(dump) is False
    assert is_dump_like_text(dump) is True


def test_abstention_score_rejects_empty():
    assert bmp.score_abstention("") is False


def test_timeline_requires_order_cue_not_pure_echo():
    """Markers alone (question echo) fail; first/then or before/after pass."""
    ea, eb = "BeamEventA99", "BeamEventB99"
    # Pure echo of both markers without order language
    assert bmp.score_timeline(f"You asked about {ea} and {eb}.", ea, eb) is False
    assert (
        bmp.score_timeline(
            f"I don't know the order of {ea} and {eb}; not in memory.",
            ea,
            eb,
        )
        is False
    )
    # Gate 0.4: first/then only inside refusal meta is NOT a pass
    assert (
        bmp.score_timeline(
            f"I can't give a first/then answer about {ea} and {eb}.",
            ea,
            eb,
        )
        is False
    )
    # Honest order language with both markers as ordered tokens
    assert (
        bmp.score_timeline(f"First {ea}, then {eb}.", ea, eb) is True
    )
    assert bmp.score_timeline(f"{ea} before {eb}.", ea, eb) is True
    assert bmp.score_timeline(f"{ea} → {eb}", ea, eb) is True
    assert bmp.score_timeline(f"{ea} then {eb}.", ea, eb) is True


def test_contradiction_requires_conflict_language():
    assert bmp.score_contradiction("You like Redis.") is False
    assert bmp.score_contradiction(
        "You said you like Redis and also that you hate it — which is true?"
    ) is True


def test_summarization_rejects_json_status():
    body = '{"status": "completed", "spirals_completed": 1, "results": []}'
    assert bmp.score_summarization(body) is False
    assert bmp.score_summarization(
        "You live in BeamCity and have a pet named BeamPet. You also set a preference marker."
    ) is True


def test_extract_never_promotes_memory_dump_as_beam_response():
    result = {
        "status": "completed",
        "spirals_completed": 0,
        "results": [{
            "actions": [{
                "tool": "recall_mine",
                "result": {
                    "success": True,
                    "output": "home_city: ZetaCity\npet_name: ZetaPet",
                },
            }],
        }],
    }
    assert extract_user_response(result) == ""


def test_extract_beam_style_abstention_reply():
    result = {
        "results": [{
            "actions": [
                {
                    "tool": "recall_mine",
                    "result": {
                        "success": True,
                        "output": "home_city: ZetaCity",
                    },
                },
                {
                    "tool": "reflex_text",
                    "result": {
                        "success": True,
                        "output": (
                            "I don't have your blood type or passport in memory."
                        ),
                    },
                },
            ],
        }],
    }
    got = extract_user_response(result)
    assert "blood type" in got.lower()
    assert "home_city:" not in got


def test_extract_fills_when_top_level_response_empty():
    """Gate 0.3: empty top-level response must still surface spiral reply text."""
    result = {
        "status": "completed",
        "response": "",
        "spirals_completed": 1,
        "results": [{
            "actions": [
                {
                    "tool": "recall_mine",
                    "result": {
                        "success": True,
                        "output": "home_city: BeamCity1",
                    },
                },
                {
                    "tool": "reflex_text",
                    "result": {
                        "success": True,
                        "output": "Your home city is BeamCity1.",
                    },
                },
            ],
        }],
    }
    got = extract_user_response(result)
    assert got == "Your home city is BeamCity1."
    assert "home_city:" not in got


def test_extract_respond_tool_when_response_missing():
    result = {
        "results": [{
            "actions": [{
                "tool": "respond",
                "result": {"success": True, "text": "pong from respond"},
            }],
        }],
    }
    assert extract_user_response(result) == "pong from respond"


def test_foreign_secret_scoring_rejects_only_b_leak():
    """Iron rule / preference must fail if foreign entity secret appears."""
    assert bmp.score_no_foreign_secret("Honor BeamIronRule123") is True
    assert bmp.score_no_foreign_secret("The rule is ONLY_B_252009") is False
    assert bmp.score_no_foreign_secret("ONLY_FROM_A in inject") is False
    assert bmp.score_summarization(
        "You live in BeamCity and have a pet. Secret ONLY_A_MARKER_ZZZ"
    ) is False


def test_stale_beam_marker_from_prior_run_is_foreign():
    """Gate 0.4: prior BeamIronRule from sequential entity must fail clean check."""
    # Module IRON is BeamIronRule{TS} for this process; a different suffix is stale
    stale = "BeamIronRule63482"
    assert stale != bmp.IRON
    assert bmp._contains_stale_beam_marker(f"Honor {stale}") is True
    assert bmp._contains_stale_beam_marker(f"Honor {bmp.IRON}") is False
    assert bmp._contains_stale_beam_marker(
        f"You should honor {bmp.IRON} only."
    ) is False
