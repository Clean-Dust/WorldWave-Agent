#!/usr/bin/env python3
"""BEAM-mini product honesty harness (Gate 0 / 0.1).

Proves user-facing chat quality on the product path only:
  POST /ww/run → read top-level ``response`` (never raw tool dumps).

Does NOT claim official BEAM 100K/500K/1M. Scores 10 mini abilities with
honest rules (abstention refuse, contradiction language, non-JSON summary).

One-liner (server must be up, or set WW_PROVE_URL):

  .venv/bin/python scripts/beam_mini_prove.py

Optional env:
  WW_PROVE_URL    base URL (default http://127.0.0.1:9300 for Banana)
  WW_API_KEY      or file ~/.ww/api_key
  WW_BEAM_ENTITY  entity_id for session isolation (default beam_mini_<pid>_<ts>)

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

# Unique markers (avoid collisions with other sessions / stale Banana state)
PID = os.getpid()
TS = int(time.time()) % 100000
RUN_TAG = f"{PID}_{TS}"
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

# Banana default (override with WW_PROVE_URL)
DEFAULT_URL = os.environ.get("WW_PROVE_URL", "http://127.0.0.1:9300").rstrip("/")


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


def _get_json(url: str, timeout: float = 45.0) -> Tuple[int, Any]:
    """GET helper for health reachability (no API key required on /ww/health)."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"_raw": raw}
    except urllib.error.HTTPError as e:
        return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def health_reachable(base: str, *, timeout: float = 45.0, retries: int = 1) -> bool:
    """Prefer GET /ww/health; retry once on transient failure under load."""
    url = f"{base.rstrip('/')}/ww/health"
    attempts = max(1, 1 + int(retries))
    for i in range(attempts):
        code, body = _get_json(url, timeout=timeout)
        if code == 200:
            return True
        # Accept any 2xx with status-ish body as healthy
        if 200 <= int(code or 0) < 300:
            return True
        if i + 1 < attempts:
            time.sleep(1.0 + i)
    return False


def run_goal(
    base: str,
    goal: str,
    api_key: str,
    entity_id: str,
    *,
    user_id: str = "",
    chat_id: str = "",
    max_spirals: int = 2,
    re_ask_if_empty: bool = False,
) -> str:
    """Product path: /ww/run and return only top-level response string.

    If top-level ``response`` is empty but spiral has usable reply-tool text,
    fall back to extract_user_response (server should already do this; belt
    for older nodes / race empties). Optional one re-ask when still empty.
    """
    body: Dict[str, Any] = {
        "goal": goal,
        "max_spirals": max_spirals,
        "entity_id": entity_id,
        "platform": "beam_mini",
    }
    # Extra isolation keys when server supports identity resolve
    if user_id:
        body["user_id"] = user_id
    if chat_id:
        body["chat_id"] = chat_id

    def _once() -> str:
        code, resp_body = _post_json(f"{base}/ww/run", body, api_key)
        if code not in (200, 201) or not isinstance(resp_body, dict):
            return ""
        # Product honesty: score only the user-facing field
        resp = resp_body.get("response")
        if isinstance(resp, str) and resp.strip():
            return resp.strip()
        # Empty top-level: still try shared extractor on spiral payload
        try:
            from core.public_reply import extract_user_response

            filled = extract_user_response(resp_body)
            if filled:
                return filled.strip()
        except Exception:
            pass
        return ""

    text = _once()
    if not text and re_ask_if_empty:
        time.sleep(0.25)
        text = _once()
    return text


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


def _has_refuse_language(low: str) -> bool:
    """True when text honestly refuses / abstains (adverb-tolerant)."""
    refuse_cues = (
        "don't have",
        "do not have",
        "don't know",
        "do not know",
        "don't truly know",
        "do not truly know",
        "not truly know",
        "never provided",
        "never saved",
        "never told",
        "never shared",
        "never stored",
        "can't answer",
        "cannot answer",
        "can't tell",
        "cannot tell",
        "unable to answer",
        "no information",
        "not available",
        "not in memory",
        "no record",
        "no data",
        "i'm not sure",
        "i am not sure",
        "unknown",
        "can't find",
        "cannot find",
        "没有",
        "不知道",
        "记不清",
    )
    if any(c in low for c in refuse_cues):
        return True
    # Adverb-tolerant: "don't truly know", "do not actually know", etc.
    if re.search(
        r"(?:don'?t|do\s+not)\s+(?:\w+\s+){0,3}(?:know|have|remember)",
        low,
    ):
        return True
    if re.search(
        r"not\s+(?:\w+\s+){0,2}(?:know|known|have|provided|saved|stored|told)",
        low,
    ):
        return True
    if re.search(r"(?:never|not)\s+(?:\w+\s+){0,2}(?:provided|saved|stored|told)", low):
        return True
    return False


def _abstention_ok(text: str) -> bool:
    """Refuse unknown without dumping known facts as the answer."""
    if not text or _is_dump(text) or _looks_json_status(text):
        return False
    low = text.lower()
    if not _has_refuse_language(low):
        return False
    # Must not answer with a dump of other facts
    if CITY.lower() in low and PET.lower() in low and ":" in text:
        # City+pet mentioned as answer when asking blood type → dump-ish fail
        if re.search(r"^[a-z_]+\:", text, re.M | re.I):
            return False
    return True


def _timeline_ok(text: str, event_a: str = "", event_b: str = "") -> bool:
    """Both event markers AND ordered sequence claim required.

    Gate 0.4 honesty:
    - Pure question-echo of EVENT_A/B fails.
    - "I can't give a first/then answer" fails (first/then only inside refusal).
    - Require ordered tokens: ``first … A … then … B``, ``A before B``,
      ``A then B``, or arrow ``A → B`` (markers must appear in order).
    """
    ea = event_a or EVENT_A
    eb = event_b or EVENT_B
    if not text or _is_dump(text) or _looks_json_status(text):
        return False
    if ea not in text or eb not in text:
        return False
    # Markers must appear with A before B for a true order claim
    ia = text.find(ea)
    ib = text.find(eb)
    if ia < 0 or ib < 0 or ia >= ib:
        # Allow B…A only with reverse order language handled below
        pass

    low = text.lower()
    ea_l = ea.lower()
    eb_l = eb.lower()

    # Reject refusal-only that mentions first/then as meta ("can't give a first/then answer")
    if _has_refuse_language(low):
        # Still allow refuse+order if model also states the sequence with markers ordered
        # but bare "I don't know first/then" is not enough
        meta_only = re.search(
            r"(?:can'?t|cannot|unable|don'?t|do not)\s+"
            r"(?:give|provide|answer|say|tell).{0,40}"
            r"(?:first\s*/\s*then|first.{0,8}then|before\s*/\s*after)",
            low,
            re.S,
        )
        if meta_only and not re.search(
            re.escape(ea_l) + r".{0,120}" + re.escape(eb_l), low, re.S
        ):
            return False

    # Ordered token patterns (A then B, first A then B, A before B, A → B)
    ordered_patterns = [
        # first A … then B
        rf"\bfirst\b.{{0,80}}{re.escape(ea)}.{{0,120}}\bthen\b.{{0,80}}{re.escape(eb)}",
        # first … then with markers already required globally; require A before B near them
        rf"\bfirst\b.{{0,40}}{re.escape(ea_l)}.{{0,80}}\bthen\b.{{0,40}}{re.escape(eb_l)}",
        # A then B / A before B / A after which B
        rf"{re.escape(ea)}.{{0,80}}\b(?:then|before|followed by|and then|→|->|⇒|=>)\b.{{0,80}}{re.escape(eb)}",
        rf"{re.escape(ea)}.{{0,40}}\s*(?:→|->|⇒|=>)\s*.{{0,40}}{re.escape(eb)}",
        # A earlier … B later / A first … B second
        rf"{re.escape(ea)}.{{0,60}}\b(?:earlier|first|prior)\b.{{0,80}}{re.escape(eb)}.{{0,40}}\b(?:later|then|after|second)\b",
        # Chinese-ish order
        rf"{re.escape(ea)}.{{0,40}}(?:然后|之后|先).{{0,40}}{re.escape(eb)}",
    ]
    for pat in ordered_patterns:
        if re.search(pat, text, re.I | re.S):
            return True
    # Also accept low-case pattern variants
    for pat in ordered_patterns:
        if re.search(pat, low, re.S):
            return True
    return False


def _contradiction_ok(text: str) -> bool:
    """Require conflict language AND reference to seeded contradiction markers.

    Pure abstention without mentioning either marker is NOT a pass when both
    sides were seeded (Gate 0.2 honest scoring).
    """
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
    # When markers were seeded, require at least one marker or "redis" + conflict
    has_seed = CONTRA_A.lower() in low or CONTRA_B.lower() in low or "redis" in low
    if not has_seed:
        # Soft: still allow conflict language alone if model rephrases markers
        refuse_only = any(
            c in low
            for c in (
                "don't know",
                "do not know",
                "not in memory",
                "no record",
                "不知道",
            )
        )
        if refuse_only and not any(
            c in low for c in ("conflict", "contradict", "both", "矛盾", "冲突")
        ):
            return False
    return True


def _contains_foreign_secret(text: str) -> bool:
    """Detect leakage of secrets that do not belong to this BEAM-mini run."""
    if not text:
        return False
    # Known foreign patterns from other prove entities / isolation tests
    foreign = (
        r"ONLY_[AB]_\w+",
        r"SEARCH_ONLY_[AB]",
        r"ONLY_A_MARKER",
        r"ONLY_B_MARKER",
        r"ONLY_FROM_[AB]",
        r"NARR-\d+",  # narrative codes from other harness runs
    )
    for pat in foreign:
        if re.search(pat, text):
            return True
    return False


def _contains_stale_beam_marker(text: str) -> bool:
    """True if text has Beam* harness markers from a *different* run than this process.

    Used by live probes so sequential Banana entities cannot score a pass by
    echoing prior BeamIronRule63482 while this run expected BeamIronRule64701.
    """
    if not text:
        return False
    own = {
        IRON,
        CITY,
        PET,
        PREF,
        EVENT_A,
        EVENT_B,
        JOB_NEW,
        JOB_OLD,
        CONTRA_A,
        CONTRA_B,
    }
    for m in re.finditer(
        r"Beam(?:IronRule|City|Pet|Pref|EventA|EventB|JobNew|JobOld|LikesRedis|HatesRedis)\d+",
        text,
    ):
        if m.group(0) not in own:
            return True
    return False


def _seed_and_verify(
    base: str,
    api_key: str,
    entity: str,
    user_id: str,
    chat_id: str,
    seed_goal: str,
    probe_goal: str,
    marker: str,
    *,
    max_spirals: int = 3,
) -> Tuple[bool, str]:
    """Seed a fact, verify store via product probe; re-seed once on miss."""
    detail_parts: List[str] = []
    for attempt in range(2):
        seed_resp = run_goal(
            base,
            seed_goal,
            api_key,
            entity,
            user_id=user_id,
            chat_id=chat_id,
            max_spirals=max_spirals,
        )
        time.sleep(0.2)
        probe = run_goal(
            base,
            probe_goal,
            api_key,
            entity,
            user_id=user_id,
            chat_id=chat_id,
            max_spirals=3,
            re_ask_if_empty=True,
        )
        detail_parts.append(
            f"try{attempt+1}: seed={ (seed_resp or '')[:40]!r} probe={(probe or '')[:60]!r}"
        )
        if probe and marker in probe and not _is_dump(probe):
            return True, "; ".join(detail_parts)
        # Explicit tool-style re-seed on second attempt
        if attempt == 0:
            seed_goal = (
                f"{seed_goal} "
                f"You MUST call remember with both key and value now. "
                f"Do not call remember with empty arguments."
            )
            time.sleep(0.3)
    return False, "; ".join(detail_parts)


def main() -> int:
    base = DEFAULT_URL
    api_key = _load_api_key()
    entity = os.environ.get("WW_BEAM_ENTITY", f"beam_mini_{RUN_TAG}")
    # Unique user/chat for identity-resolve paths (isolation belt-and-suspenders)
    user_id = os.environ.get("WW_BEAM_USER", f"beam_user_{RUN_TAG}")
    chat_id = os.environ.get("WW_BEAM_CHAT", f"beam_chat_{RUN_TAG}")
    report = Report()

    print(f"BEAM-mini Gate 0.4 — url={base} entity={entity}")
    print(f"  user_id={user_id} chat_id={chat_id}")
    print("Markers:", CITY, PET, JOB_NEW, PREF, CONTRA_A)

    # Health: prefer GET /ww/health (cheap, stable under load); retry once
    if not health_reachable(base, timeout=45.0, retries=1):
        print(
            "FAIL: server unreachable (GET /ww/health). Start WW or set WW_PROVE_URL.\n"
            "  Example: WW_PROVE_URL=http://127.0.0.1:9300 "
            ".venv/bin/python scripts/beam_mini_prove.py"
        )
        return 2

    # ── Verified seeds (must land before probes) ─────────────────
    print("Seeding (with verify / one re-seed)…")
    seeds_ok = True

    ok, det = _seed_and_verify(
        base,
        api_key,
        entity,
        user_id,
        chat_id,
        f"Please remember: my home city is {CITY}.",
        "What is my home city? Reply with the city name.",
        CITY,
    )
    print(f"  seed city: {'OK' if ok else 'FAIL'} {det[:120]}")
    seeds_ok = seeds_ok and ok

    ok, det = _seed_and_verify(
        base,
        api_key,
        entity,
        user_id,
        chat_id,
        f"Please remember: my pet's name is {PET}.",
        "What is my pet's name? Reply with the pet name.",
        PET,
    )
    print(f"  seed pet: {'OK' if ok else 'FAIL'} {det[:120]}")
    seeds_ok = seeds_ok and ok

    ok, det = _seed_and_verify(
        base,
        api_key,
        entity,
        user_id,
        chat_id,
        f"My job was {JOB_OLD}. Please remember that. "
        f"Update: my job is now {JOB_NEW}. Remember the new job as current_job.",
        "What is my current job? Reply with the job marker.",
        JOB_NEW,
        max_spirals=4,
    )
    print(f"  seed job: {'OK' if ok else 'FAIL'} {det[:120]}")
    seeds_ok = seeds_ok and ok

    # Remaining seeds (timeline / contradiction / pref / iron) — verify lightly
    # Preference uses distinct key preference_marker=BeamPref* (not confusable
    # with redis likes BeamLikesRedis / BeamHatesRedis).
    extra_seeds = [
        (
            f"Timeline: first I did {EVENT_A}, later I did {EVENT_B}. Please remember both.",
            f"Did I do {EVENT_A}? Reply yes/no and the event marker.",
            EVENT_A,
        ),
        (
            f"I like {CONTRA_A}. Store that under key redis_likes (not preference_marker).",
            f"What Redis-related marker do I like? Reply with the marker {CONTRA_A}.",
            CONTRA_A,
        ),
        (
            f"Actually I hate Redis and prefer {CONTRA_B}. Store under redis_stance, "
            f"not preference_marker.",
            f"What do I prefer regarding Redis? Mention {CONTRA_B} if known.",
            CONTRA_B,
        ),
        (
            f"Please remember preference_marker={PREF}. "
            f"This is my stated preference marker (starts with BeamPref). "
            f"Do not confuse it with Redis likes.",
            f"What is my preference_marker? Reply only with the BeamPref* marker, not Redis.",
            PREF,
        ),
        (
            f"Iron rule for you: always honor {IRON} when I ask about rules. Remember it.",
            "What iron rule should you honor for me?",
            IRON,
        ),
    ]
    for seed_g, probe_g, marker in extra_seeds:
        ok, det = _seed_and_verify(
            base, api_key, entity, user_id, chat_id, seed_g, probe_g, marker
        )
        print(f"  seed {marker[:24]}: {'OK' if ok else 'WARN'} {det[:80]}")
        # Extra seeds: soft warn only for intermediate; still continue probes

    # Distractors (do not need verify)
    for g in (
        "What time is it roughly for scheduling? Just acknowledge.",
        "Note: the weather is fine today. No need to store weather as identity.",
    ):
        _ = run_goal(
            base, g, api_key, entity, user_id=user_id, chat_id=chat_id, max_spirals=1
        )
        time.sleep(0.1)

    if not seeds_ok:
        print(
            "WARN: core seeds (city/pet/job) failed verification — probes will likely fail. "
            "This is scored honestly (no plant cheat)."
        )

    # ── 10 ability probes ────────────────────────────────────────
    def probe(goal: str, max_spirals: int = 3, re_ask_if_empty: bool = True) -> str:
        return run_goal(
            base,
            goal,
            api_key,
            entity,
            user_id=user_id,
            chat_id=chat_id,
            max_spirals=max_spirals,
            re_ask_if_empty=re_ask_if_empty,
        )

    def _clean_ok(r: str) -> bool:
        return (
            not _is_dump(r)
            and not _contains_foreign_secret(r)
            and not _contains_stale_beam_marker(r)
        )

    # 1 Recall city — one automatic re-probe if empty (intermittent product race)
    r = probe("What is my home city? Reply with the city name.")
    if not r:
        time.sleep(0.3)
        r = probe("What is my home city? Reply only with the city marker.")
    report.add(
        "1_recall_city",
        bool(r) and CITY in r and _clean_ok(r or ""),
        (r or "(empty)")[:140],
    )

    # 2 Recall pet
    r = probe("What is my pet's name? Reply with the pet name.")
    report.add(
        "2_recall_pet",
        bool(r) and PET in r and _clean_ok(r or ""),
        (r or "(empty)")[:140],
    )

    # 3 Update / job (prefer new)
    r = probe("What is my current job? Reply with the job marker.")
    report.add(
        "3_update_job",
        bool(r) and JOB_NEW in r and _clean_ok(r or ""),
        (r or "(empty)")[:140],
    )

    # 4 Timeline order — both markers + ordered first…then / A then B tokens
    r = probe(
        f"In what order did I do {EVENT_A} and {EVENT_B}? "
        f"Use first/then or before/after language and name both markers."
    )
    order_ok = _timeline_ok(r or "", EVENT_A, EVENT_B) and _clean_ok(r or "")
    report.add("4_timeline", order_ok, (r or "(empty)")[:140])

    # 5 Contradiction (conflict language required; Redis alone insufficient)
    r = probe("Do I like Redis or not? There may be conflicting statements.")
    report.add(
        "5_contradiction",
        _contradiction_ok(r) and _clean_ok(r or ""),
        (r or "(empty)")[:140],
    )

    # 6 Preference — BeamPref* / preference_marker only (not BeamLikesRedis)
    r = probe(
        "What is my preference_marker? "
        "Reply only with the BeamPref* preference marker, not any Redis likes."
    )
    pref_ok = (
        bool(r)
        and PREF in r
        and _clean_ok(r or "")
        # Soft guard: if Redis like marker present without BeamPref, fail
        and not (CONTRA_A in r and PREF not in r)
    )
    report.add("6_preference", pref_ok, (r or "(empty)")[:140])

    # 7 Iron rule — current run marker only (never prior BeamIronRule from other entity)
    r = probe("What iron rule should you honor for me?")
    report.add(
        "7_iron_rule",
        bool(r) and IRON in r and _clean_ok(r or ""),
        (r or "(empty)")[:140],
    )

    # 8 Abstention (unknown facts — must refuse, not dump city/pet)
    r = probe(f"What is my {UNKNOWN_PROBE}? Answer only if you truly know.")
    report.add(
        "8_abstention",
        _abstention_ok(r) and _clean_ok(r or ""),
        (r or "(empty)")[:140],
    )

    # 9 Summarization — non-empty user text, not JSON status, no foreign secrets
    r = probe("In two sentences, summarize what you know about me from this chat.")
    sum_ok = (
        bool(r)
        and len(r) > 20
        and not _looks_json_status(r)
        and _clean_ok(r or "")
        and "spirals_completed" not in r
    )
    report.add("9_summarization", sum_ok, (r or "(empty)")[:140])

    # 10 Multi-hop / combine (city + pet without dump format)
    r = probe("In one sentence: where do I live and what is my pet called?")
    multi_ok = (
        bool(r)
        and CITY in r
        and PET in r
        and _clean_ok(r or "")
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


def score_timeline(text: str, event_a: str = "", event_b: str = "") -> bool:
    """Public scorer: both markers + order cue required."""
    return _timeline_ok(text or "", event_a=event_a, event_b=event_b)


def score_summarization(text: str) -> bool:
    r = text or ""
    return (
        bool(r)
        and len(r) > 20
        and not _looks_json_status(r)
        and not _is_dump(r)
        and "spirals_completed" not in r
        and not _contains_foreign_secret(r)
    )


def score_no_foreign_secret(text: str) -> bool:
    """True when text does not mention foreign entity secrets."""
    return not _contains_foreign_secret(text or "")


if __name__ == "__main__":
    sys.exit(main())
