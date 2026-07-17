"""LLM-as-judge scaffold for official BEAM ability categories.

Does **not** hand-edit scores. Pluggable via ``WW_BEAM_JUDGE_MODEL`` /
``judge_model`` arg. When no LLM is available, returns a structured
``pending`` judgment (never fake official numbers).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

from .data import ABILITY_KEYS

# Frozen defaults for reproducible reports (document in meta.json)
DEFAULT_JUDGE_TEMP = 0.0
DEFAULT_JUDGE_MODEL = os.environ.get("WW_BEAM_JUDGE_MODEL") or os.environ.get(
    "WW_JUDGE_MODEL", "deepseek/deepseek-v4-flash"
)


def rubric_prompt(
    ability: str,
    question: str,
    ideal: str,
    rubric: List[str],
    response: str,
) -> str:
    rub = "\n".join(f"- {r}" for r in (rubric or []) if r) or "- (no rubric items)"
    return (
        "You are a strict evaluator for the BEAM long-term memory benchmark.\n"
        f"Ability category: {ability}\n"
        "Score the candidate answer against the ideal/rubric. "
        "Do not invent credit. Output JSON only:\n"
        '{"score": 0.0-1.0, "pass": true|false, "rationale": "..."}\n\n'
        f"Question:\n{question}\n\n"
        f"Ideal / reference:\n{ideal or '(none)'}\n\n"
        f"Rubric:\n{rub}\n\n"
        f"Candidate answer:\n{response or '(empty)'}\n"
    )


def heuristic_score(
    ability: str,
    question: str,
    ideal: str,
    rubric: List[str],
    response: str,
) -> Dict[str, Any]:
    """Lightweight offline check for dry-run / smoke — not official.

    Marks pass only on strong lexical overlap with ideal/rubric tokens.
    Empty response → fail.
    """
    resp = (response or "").strip()
    if not resp:
        return {
            "score": 0.0,
            "pass": False,
            "rationale": "empty response",
            "method": "heuristic",
            "official": False,
        }
    needles: List[str] = []
    if ideal:
        needles.extend(re.findall(r"[A-Za-z0-9_]{4,}", ideal.lower()))
    for r in rubric or []:
        needles.extend(re.findall(r"[A-Za-z0-9_]{4,}", str(r).lower()))
    needles = list(dict.fromkeys(needles))[:40]
    low = resp.lower()
    if not needles:
        # No ideal: non-empty only — not a pass claim
        return {
            "score": 0.0,
            "pass": False,
            "rationale": "no ideal/rubric for heuristic; non-empty recorded only",
            "method": "heuristic",
            "official": False,
        }
    hits = sum(1 for n in needles if n in low)
    ratio = hits / max(len(needles), 1)
    # Conservative: require high overlap
    ok = ratio >= 0.35 and len(resp) >= 20
    return {
        "score": round(min(1.0, ratio), 4),
        "pass": bool(ok),
        "rationale": f"heuristic token overlap {hits}/{len(needles)}",
        "method": "heuristic",
        "official": False,
    }


def judge_one(
    ability: str,
    question: str,
    ideal: str,
    rubric: List[str],
    response: str,
    *,
    llm_chat: Optional[Callable[[str], str]] = None,
    model: str = "",
    temperature: float = DEFAULT_JUDGE_TEMP,
) -> Dict[str, Any]:
    """Judge a single answer. Never hand-edits; LLM path parses JSON only."""
    ability = ability if ability in ABILITY_KEYS else ability
    if llm_chat is None:
        out = heuristic_score(ability, question, ideal, rubric, response)
        out["model"] = model or "heuristic"
        out["temperature"] = temperature
        return out

    prompt = rubric_prompt(ability, question, ideal, rubric, response)
    try:
        raw = llm_chat(prompt) or ""
    except Exception as e:
        return {
            "score": 0.0,
            "pass": False,
            "rationale": f"judge LLM error: {e}",
            "method": "llm",
            "model": model or DEFAULT_JUDGE_MODEL,
            "temperature": temperature,
            "official": False,
        }
    parsed = _parse_judge_json(raw)
    parsed["method"] = "llm"
    parsed["model"] = model or DEFAULT_JUDGE_MODEL
    parsed["temperature"] = temperature
    parsed["official"] = False  # runner never claims official without human seal
    parsed["raw"] = raw[:2000]
    return parsed


def _parse_judge_json(raw: str) -> Dict[str, Any]:
    s = (raw or "").strip()
    # Extract first {...}
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {
            "score": 0.0,
            "pass": False,
            "rationale": "judge returned non-JSON",
        }
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {
            "score": 0.0,
            "pass": False,
            "rationale": "judge JSON parse failed",
        }
    score = data.get("score", 0.0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    passed = data.get("pass")
    if passed is None:
        passed = score >= 0.5
    return {
        "score": score,
        "pass": bool(passed),
        "rationale": str(data.get("rationale") or "")[:500],
    }
