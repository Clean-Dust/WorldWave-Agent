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


def test_abstention_score_rejects_dump():
    dump = "home_city: ZetaCity\npet_name: ZetaPet"
    assert bmp.score_abstention(dump) is False
    assert is_dump_like_text(dump) is True


def test_abstention_score_rejects_empty():
    assert bmp.score_abstention("") is False


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


def test_foreign_secret_scoring_rejects_only_b_leak():
    """Iron rule / preference must fail if foreign entity secret appears."""
    assert bmp.score_no_foreign_secret("Honor BeamIronRule123") is True
    assert bmp.score_no_foreign_secret("The rule is ONLY_B_252009") is False
    assert bmp.score_summarization(
        "You live in BeamCity and have a pet. Secret ONLY_A_MARKER_ZZZ"
    ) is False
