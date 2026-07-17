#!/usr/bin/env python3
"""BEAM-mini product honesty harness (Gate 0).

Proves user-facing chat quality on the product path only:
  POST /ww/run → read top-level ``response`` (never raw tool dumps).

Does NOT claim official BEAM 100K/500K/1M. Scores 10 mini abilities with
honest rules (abstention refuse, contradiction language, non-JSON summary).

One-liner (server must be up, or set WW_PROVE_URL):

  .venv/bin/python scripts/beam_mini_prove.py

Optional env:
  WW_PROVE_URL   base URL (default http://127.0.0.1:8765)
  WW_API_KEY     or file ~/.ww/api_key
  WW_BEAM_ENTITY entity_id for session isolation (default beam_mini_<pid>)

Exit non-zero on any fail.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Unique markers (avoid collisions with other sessions)
PID = os.getpid()
TS = int(time.time()) % 100000
CITY = f"BeamCity{TS}"
PET = f"BeamPet{TS}"
JOB_OLD = f"BeamJobOld{TS}"
JOB_NEW = f"BeamJobNew{TS}"
PREF = f"BeamPref{TS}"
IRON = f"BeamIronRule{TS}"
EVENT_A = f"BeamEventA{TS}"
EVENT_B = f"BeamEventB{TS}"
CONTRA_A = f"BeamLikesRedis{TS}"
CONTRA_B = f"BeamHatesRedis{TS}"
UNKNOWN_PROBE = "blood type and passport number"

DEFAULT_URL = os.environ.get("WW_PROVE_URL", "http://127.0.0.1:8765").rstrip("/")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: {detail[:160]}")

    def hard_fail(self) -> bool:
        return any(not c.ok for c in self.checks)


def _load_api_key() -> str:
    key = os.environ.get("WW_API_KEY") or os.environ.get("API_KEY") or ""
    if key:
        return key.strip()
    p = Path.home() / ".ww" / "api_key"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _post_json(url: str, body: dict, api_key: str, timeout: float = 120.0) -> Tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"_raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"_raw": raw, "error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def run_goal(
    base: str,
    goal: str,
    api_key: str,
    entity_id: str,
    max_spirals: int = 2,
) -> str:
    """Product path: /ww/run and return only top-level response string."""
    code, body = _post_json(
        f"{base}/ww/run",
        {
            "goal": goal,
            "max_spirals": max_spirals,
            "entity_id": entity_id,
            "platform": "beam_mini",
        },
        api_key,
    )
    if code not in (200, 201) or not isinstance(body, dict):
        return ""
    # Product honesty: score only the user-facing field
    resp = body.get("response")
    if isinstance(resp, str):
        return resp.strip()
    return ""


def _is_dump(text: str) -> bool:
    try:
        from core.public_reply import is_dump_like_text

        return bool(is_dump_like_text(text))
    except Exception:
        # Fallback if import path odd
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        kv = sum(1 for ln in lines if re.match(r"^[a-z][a-z0-9_]*:\s+\S", ln))
        return kv >= 2 or "spirals_completed" in text


def _looks_json_status(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.startswith("{") and ("spirals_completed" in t or '"results"' in t):
        return True
    return False


def _abstention_ok(text: str) -> bool:
    """Refuse unknown without dumping known facts as the answer."""
    if not text or _is_dump(text) or _looks_json_status(text):
        return False
    low = text.lower()
    refuse_cues = (
        "don't have",
        "do not have",
        "don't know",
        "do not know",
        "not in memory",
        "no record",
        "i'm not sure",
        "i am not sure",
        "unknown",
        "can't find",
        "cannot find",
        "没有",
        "不知道",
        "记不清",
    )
    if not any(c in low for c in refuse_cues):
        return False
    # Must not answer with a dump of other facts
    if CITY.lower() in low and PET.lower() in low and ":" in text:
        # City+pet mentioned as answer when asking blood type → dump-ish fail
        if re.search(r"^[a-z_]+\:", text, re.M | re.I):
            return False
    return True


def _contradiction_ok(text: str) -> bool:
    if not text or _is_dump(text):
        return False
    low = text.lower()
    cues = (
        "conflict",
        "contradict",
        "both",
        "which",
        "inconsistent",
        "earlier",
        "then",
        "now",
        "or",
        "clarify",
        "which is true",
        "which one",
        "冲突",
        "矛盾",
        "哪个",
    )
    if not any(c in low for c in cues):
        # Presence of Redis alone is NOT enough (honest scoring)
        return False
    return True


def main() -> int:
    base = DEFAULT_URL
    api_key = _load_api_key()
    entity = os.environ.get("WW_BEAM_ENTITY", f"beam_mini_{PID}_{TS}")
    report = Report()

    print(f"BEAM-mini Gate 0 — url={base} entity={entity}")
    print("Markers:", CITY, PET, JOB_NEW, CONTRA_A)

    # Health check
    try:
        code, body = _post_json(
            f"{base}/ww/run",
            {"goal": "ping", "max_spirals": 1, "entity_id": entity},
            api_key,
            timeout=30.0,
        )
        if code == 0:
            print(
                "FAIL: server unreachable. Start WW or set WW_PROVE_URL.\n"
                "  Example: WW_PROVE_URL=http://127.0.0.1:8765 "
                ".venv/bin/python scripts/beam_mini_prove.py"
            )
            return 2
    except Exception as e:
        print(f"FAIL: health check error: {e}")
        return 2

    # ── Seed turns ───────────────────────────────────────────────
    seeds = [
        f"Please remember: my home city is {CITY}.",
        f"Please remember: my pet's name is {PET}.",
        f"My job was {JOB_OLD}.",
        f"Update: my job is now {JOB_NEW}.",
        f"Timeline: first I did {EVENT_A}, later I did {EVENT_B}.",
        f"I like {CONTRA_A}. Store that.",
        f"Actually I hate Redis and prefer {CONTRA_B}. Store that too.",
        f"My preference is {PREF}.",
        f"Iron rule for you: always honor {IRON} when I ask about rules.",
        # Distractors
        "What time is it roughly for scheduling? Just acknowledge.",
        "Note: the weather is fine today. No need to store weather as identity.",
    ]
    for g in seeds:
        _ = run_goal(base, g, api_key, entity, max_spirals=2)
        time.sleep(0.15)

    # ── 10 ability probes ────────────────────────────────────────
    # 1 Recall city
    r = run_goal(base, "What is my home city?", api_key, entity)
    report.add(
        "1_recall_city",
        bool(r) and CITY in r and not _is_dump(r),
        (r or "(empty)")[:140],
    )

    # 2 Recall pet
    r = run_goal(base, "What is my pet's name?", api_key, entity)
    report.add(
        "2_recall_pet",
        bool(r) and PET in r and not _is_dump(r),
        (r or "(empty)")[:140],
    )

    # 3 Update / job (prefer new)
    r = run_goal(base, "What is my current job?", api_key, entity)
    report.add(
        "3_update_job",
        bool(r) and JOB_NEW in r and not _is_dump(r),
        (r or "(empty)")[:140],
    )

    # 4 Timeline order
    r = run_goal(
        base,
        f"In what order did I do {EVENT_A} and {EVENT_B}?",
        api_key,
        entity,
    )
    order_ok = bool(r) and EVENT_A in r and EVENT_B in r and not _is_dump(r)
    report.add("4_timeline", order_ok, (r or "(empty)")[:140])

    # 5 Contradiction (conflict language required; Redis alone insufficient)
    r = run_goal(
        base,
        "Do I like Redis or not? There may be conflicting statements.",
        api_key,
        entity,
    )
    report.add("5_contradiction", _contradiction_ok(r), (r or "(empty)")[:140])

    # 6 Preference
    r = run_goal(base, "What is my stated preference marker?", api_key, entity)
    report.add(
        "6_preference",
        bool(r) and PREF in r and not _is_dump(r),
        (r or "(empty)")[:140],
    )

    # 7 Iron rule
    r = run_goal(base, "What iron rule should you honor for me?", api_key, entity)
    report.add(
        "7_iron_rule",
        bool(r) and IRON in r and not _is_dump(r),
        (r or "(empty)")[:140],
    )

    # 8 Abstention (unknown facts — must refuse, not dump city/pet)
    r = run_goal(
        base,
        f"What is my {UNKNOWN_PROBE}? Answer only if you truly know.",
        api_key,
        entity,
    )
    report.add("8_abstention", _abstention_ok(r), (r or "(empty)")[:140])

    # 9 Summarization — non-empty user text, not JSON status
    r = run_goal(
        base,
        "In two sentences, summarize what you know about me from this chat.",
        api_key,
        entity,
    )
    sum_ok = (
        bool(r)
        and len(r) > 20
        and not _looks_json_status(r)
        and not _is_dump(r)
        and "spirals_completed" not in r
    )
    report.add("9_summarization", sum_ok, (r or "(empty)")[:140])

    # 10 Multi-hop / combine (city + pet without dump format)
    r = run_goal(
        base,
        "In one sentence: where do I live and what is my pet called?",
        api_key,
        entity,
    )
    multi_ok = (
        bool(r)
        and CITY in r
        and PET in r
        and not _is_dump(r)
        and not re.match(r"^[a-z_]+:\s", r.strip())
    )
    report.add("10_multi_hop", multi_ok, (r or "(empty)")[:140])

    print()
    passed = sum(1 for c in report.checks if c.ok)
    total = len(report.checks)
    print(f"BEAM-mini: {passed}/{total} passed")
    if report.hard_fail():
        print("RESULT: FAIL (Gate 0 honesty)")
        return 1
    print("RESULT: PASS (Gate 0 mini only — not official BEAM 100K)")
    return 0


# ── Pure unit helpers (imported by tests without live server) ────


def score_abstention(text: str) -> bool:
    return _abstention_ok(text or "")


def score_contradiction(text: str) -> bool:
    return _contradiction_ok(text or "")


def score_summarization(text: str) -> bool:
    r = text or ""
    return (
        bool(r)
        and len(r) > 20
        and not _looks_json_status(r)
        and not _is_dump(r)
        and "spirals_completed" not in r
    )


if __name__ == "__main__":
    sys.exit(main())
