#!/usr/bin/env python3
"""Official BEAM runner skeleton for WorldWave memory evaluation.

Gate order: Gate 0 product honesty must stay green before any official tier.
This runner does **not** claim official 100K/500K/1M scores. Mini ≠ 100K.

Usage::

    .venv/bin/python scripts/beam_runner.py --scale 100K --systems ww,b1,b2 --chat 1
    .venv/bin/python scripts/beam_runner.py --scale 100K --systems ww --resume
    .venv/bin/python scripts/beam_runner.py --scale 100K --list-chats
    .venv/bin/python scripts/beam_runner.py --scale 100K --chat 1 --dry-run --max-abilities 1

Data: local cache ``~/.ww/beam_cache`` or ``WW_BEAM_DATA`` (see docs/beam-eval.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from beam.baselines import b1_context_prompt, b2_rag_prompt  # noqa: E402
from beam.data import (  # noqa: E402
    ABILITY_KEYS,
    SCALES,
    BeamChat,
    chat_text_blob,
    list_chat_ids,
    load_chat,
    resolve_data_root,
)
from beam.judge import DEFAULT_JUDGE_MODEL, DEFAULT_JUDGE_TEMP, judge_one  # noqa: E402
from beam.ww_client import WWRunClient, resolve_api_key  # noqa: E402


def _git_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _config_hash(cfg: dict) -> str:
    blob = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _results_dir(scale: str, cfg: dict) -> Path:
    sha = _git_sha()
    ch = _config_hash(cfg)
    d = ROOT / "results" / "beam" / scale / f"{sha}_{ch}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _load_done_keys(path: Path) -> Set[str]:
    done: Set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        k = row.get("key") or f"{row.get('chat_id')}:{row.get('ability')}:{row.get('probe_index')}"
        done.add(str(k))
    return done


def _load_answer_rows(out_dir: Path, systems: Sequence[str]) -> List[dict]:
    """Load all answer rows from answers_*.jsonl (resume-safe full picture)."""
    by_key: Dict[str, dict] = {}
    order: List[str] = []
    for system in systems:
        path = out_dir / f"answers_{system}.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            k = str(
                row.get("key")
                or f"{row.get('chat_id')}:{row.get('ability')}:{row.get('probe_index')}:{system}"
            )
            # Last write wins for duplicate keys (re-run without --resume)
            if k not in by_key:
                order.append(k)
            by_key[k] = row
    return [by_key[k] for k in order]


def entity_for(scale: str, chat_id: str, run_tag: str) -> str:
    return f"beam_{scale}_{chat_id}_{run_tag}"


# /ww/run TaskRequest.goal max_length=2000; keep margin for safety.
API_GOAL_MAX_CHARS = 2000
GOAL_CHAR_BUDGET = 1800

BATCH_INGEST_HEADER = (
    "Ingest the following conversation turns into memory. "
    "Use remember for durable user facts. Acknowledge briefly.\n\n"
)
ASSISTANT_NOTE_HEADER = (
    "Note this prior assistant message for conversation continuity "
    "(do not invent new user facts):\n"
)


def hard_split_goal(
    header: str,
    body: str,
    budget: int = GOAL_CHAR_BUDGET,
) -> List[str]:
    """Build one or more goals under *budget*. Never silent-truncate body.

    When header+body fits, returns a single goal. Otherwise splits *body* into
    labeled parts: ``{header}[part N/M]\\n{chunk}`` (still product /ww/run).
    """
    header = header or ""
    body = body if body is not None else ""
    if not header and not body:
        return []
    if len(header) + len(body) <= budget:
        return [header + body]
    if len(header) >= budget - 12:
        raise ValueError(
            f"goal header ({len(header)} chars) leaves no room under budget {budget}"
        )

    def _goals_for_chunk_size(chunk_size: int) -> List[str]:
        if chunk_size < 1:
            return []
        chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
        total = len(chunks)
        return [
            f"{header}[part {i}/{total}]\n{chunk}"
            for i, chunk in enumerate(chunks, 1)
        ]

    lo, hi = 1, max(1, len(body))
    best: Optional[List[str]] = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _goals_for_chunk_size(mid)
        if candidate and all(len(g) <= budget for g in candidate):
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        # Pathological: tiny room after header — still emit with minimal chunks.
        room = max(1, budget - len(header) - len(f"[part 1/{max(1, len(body))}]\n"))
        best = _goals_for_chunk_size(room)
        for g in best:
            if len(g) > budget:
                raise ValueError(
                    f"unable to hard-split goal under budget {budget} "
                    f"(header={len(header)}, body={len(body)})"
                )
    return best


def _batch_body_len(lines: Sequence[str]) -> int:
    if not lines:
        return 0
    return sum(len(x) for x in lines) + (len(lines) - 1)


def pack_ingest_goals(
    turns: Sequence[Dict[str, Any]],
    *,
    ingest_mode: str = "turn",
    batch_n: int = 5,
    budget: int = GOAL_CHAR_BUDGET,
) -> List[str]:
    """Pack chat turns into /ww/run goals within *budget* (default 1800).

    * ``batch_n``: flush on turn count **or** when adding the next turn would
      exceed the character budget. Oversized single turns are hard-split.
    * ``turn``: one message per goal (same budget); oversized content hard-split.
    """
    mode = (ingest_mode or "turn").strip().lower()
    if mode == "batch_n":
        return _pack_batch_n_goals(turns, batch_n=batch_n, budget=budget)
    return _pack_turn_goals(turns, budget=budget)


def _pack_turn_goals(
    turns: Sequence[Dict[str, Any]],
    *,
    budget: int,
) -> List[str]:
    goals: List[str] = []
    for t in turns:
        role = str(t.get("role") or "user")
        content = str(t.get("content") or "")
        if role == "assistant":
            goals.extend(hard_split_goal(ASSISTANT_NOTE_HEADER, content, budget))
        else:
            goals.extend(hard_split_goal("", content, budget))
    return goals


def _pack_batch_n_goals(
    turns: Sequence[Dict[str, Any]],
    *,
    batch_n: int,
    budget: int,
) -> List[str]:
    batch_n = max(1, int(batch_n))
    header = BATCH_INGEST_HEADER
    body_budget = budget - len(header)
    if body_budget < 1:
        raise ValueError(
            f"batch ingest header ({len(header)} chars) exceeds budget {budget}"
        )

    goals: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        goals.extend(hard_split_goal(header, "\n".join(buf), budget))
        buf = []

    for t in turns:
        line = f"{t.get('role', 'user')}: {t.get('content') or ''}"
        # Single turn larger than body budget: flush then hard-split this line.
        if len(line) > body_budget:
            flush()
            goals.extend(hard_split_goal(header, line, budget))
            continue
        trial = buf + [line]
        if buf and (len(trial) > batch_n or _batch_body_len(trial) > body_budget):
            flush()
        buf.append(line)
    flush()
    return goals


def ingest_ww(
    client: WWRunClient,
    chat: BeamChat,
    entity_id: str,
    *,
    ingest_mode: str = "turn",
    batch_n: int = 5,
    max_turns: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Feed chat into real WW memory path turn-by-turn (not one giant prompt).

    Goals are packed under :data:`GOAL_CHAR_BUDGET` (margin under API 2000).
    Never silent-truncates facts; oversized content is hard-split with part labels.
    """
    turns = chat.turns
    if max_turns > 0:
        turns = turns[:max_turns]
    if dry_run:
        goals = pack_ingest_goals(
            turns, ingest_mode=ingest_mode, batch_n=batch_n
        )
        return {
            "entity_id": entity_id,
            "turns": len(turns),
            "goals": len(goals),
            "dry_run": True,
        }

    user_id = f"beam_u_{chat.chat_id}"
    chat_key = f"beam_c_{chat.chat_id}"
    mode = (ingest_mode or "turn").strip().lower()
    goals = pack_ingest_goals(turns, ingest_mode=mode, batch_n=batch_n)

    for goal in goals:
        if len(goal) > API_GOAL_MAX_CHARS:
            raise RuntimeError(
                f"ingest goal exceeds API max ({len(goal)} > {API_GOAL_MAX_CHARS})"
            )
        client.run(
            goal,
            entity_id=entity_id,
            platform="beam",
            user_id=user_id,
            chat_id=chat_key,
            max_spirals=3,
        )
    return {
        "entity_id": entity_id,
        "turns_ingested": len(turns),
        "goals_sent": len(goals),
        "dry_run": False,
    }


def probe_ww(
    client: WWRunClient,
    entity_id: str,
    chat: BeamChat,
    question: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if dry_run:
        return {
            "llm_response": "",
            "raw": {"dry_run": True},
            "status": "dry_run",
        }
    raw = client.run(
        question,
        entity_id=entity_id,
        platform="beam",
        user_id=f"beam_u_{chat.chat_id}",
        chat_id=f"beam_c_{chat.chat_id}",
        max_spirals=5,
    )
    resp = str(raw.get("response") or "").strip()
    return {"llm_response": resp, "raw": raw, "status": raw.get("status")}


# Default B1 window large enough for full 100K chat text; override via --b1-max-chars.
DEFAULT_B1_MAX_CHARS = 350_000

_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    """Load repo-root ``.env`` into os.environ when keys may be missing.

    Never prints values. Safe to call repeatedly.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path, override=False)
        return
    except Exception:
        pass
    # Manual KEY=VALUE parse (no secrets logged)
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, _, val = line.partition("=")
            key = key.strip()
            if not key or key in os.environ:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            os.environ[key] = val
    except Exception:
        return


def _resolve_answer_model(explicit: Optional[str] = None) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    env = (
        (os.environ.get("WW_BEAM_ANSWER_MODEL") or "").strip()
        or (os.environ.get("WW_MODEL") or "").strip()
    )
    if env:
        return env
    try:
        from core.llm import DEFAULT_MODEL  # type: ignore

        return DEFAULT_MODEL
    except Exception:
        return "deepseek-v4-flash"


def answer_b1(question: str, chat: BeamChat, max_chars: int = DEFAULT_B1_MAX_CHARS) -> str:
    blob = chat_text_blob(chat)
    return b1_context_prompt(question, blob, max_chars=max_chars)


def answer_b2(question: str, chat: BeamChat, top_k: int = 5) -> str:
    blob = chat_text_blob(chat)
    return b2_rag_prompt(question, blob, top_k=top_k)


def _simple_llm_answer(
    prompt: str,
    *,
    json_mode: bool = False,
    model: Optional[str] = None,
) -> str:
    """Live LLM answer for B1/B2/judge when keys present; else honest empty string.

    Builds ``LLMClient(model=...)`` correctly and calls
    ``chat(messages=[...], phase="", json_mode=..., temperature=0.0)``.
    On any failure returns ``""`` (no fake answers).
    """
    _ensure_env_loaded()
    try:
        from core.llm import LLMClient  # type: ignore
    except Exception:
        return ""
    model_name = _resolve_answer_model(model)
    try:
        client = LLMClient(model=model_name)
        out = client.chat(
            messages=[{"role": "user", "content": prompt}],
            phase="",
            json_mode=bool(json_mode),
            temperature=0.0,
        )
        return str(out or "")
    except Exception:
        # Offline / API error: empty so judge records fail honestly (no fake scores)
        return ""


def run_system_on_chat(
    system: str,
    chat: BeamChat,
    *,
    entity_id: str,
    client: Optional[WWRunClient],
    dry_run: bool,
    max_abilities: int,
    abilities: Optional[Sequence[str]],
    done: Set[str],
    answers_path: Path,
    seed: int,
    run_tag: str,
    ingest_mode: str,
    batch_n: int,
    max_turns: int,
    b1_max_chars: int,
    b2_top_k: int,
    judge_model: str,
    use_llm_judge: bool,
    answer_model: str = "",
) -> List[dict]:
    rows: List[dict] = []
    # Resume: skip WW ingest when all expected probes for this chat are already done
    # (avoids multi-hour re-ingest of completed chats while answers stay frozen).
    expected = expected_probe_keys(
        system,
        chat,
        max_abilities=max_abilities,
        abilities=abilities,
    )
    probes_complete = bool(expected) and expected.issubset(done)

    # Ingest once per chat for WW (unless probes already complete on resume)
    if system == "ww" and not dry_run and client is not None:
        if probes_complete:
            print(
                f"[ww] chat={chat.chat_id} skip_ingest=1 reason=probes_complete",
                flush=True,
            )
        else:
            print(
                f"[ww] chat={chat.chat_id} skip_ingest=0",
                flush=True,
            )
            ingest_ww(
                client,
                chat,
                entity_id,
                ingest_mode=ingest_mode,
                batch_n=batch_n,
                max_turns=max_turns,
                dry_run=False,
            )
    elif system == "ww" and dry_run:
        if probes_complete:
            print(
                f"[ww] chat={chat.chat_id} skip_ingest=1 reason=probes_complete",
                flush=True,
            )
        else:
            ingest_ww(
                client or WWRunClient(),  # type: ignore
                chat,
                entity_id,
                dry_run=True,
                max_turns=max_turns,
            )

    ability_filter = set(abilities) if abilities else None
    seen_abilities: List[str] = []
    ans_model = (answer_model or "").strip() or None
    for probe in chat.probes:
        if ability_filter and probe.ability not in ability_filter:
            continue
        if max_abilities > 0:
            if probe.ability not in seen_abilities:
                if len(seen_abilities) >= max_abilities:
                    continue
                seen_abilities.append(probe.ability)

        key = f"{chat.chat_id}:{probe.ability}:{probe.index}:{system}"
        if key in done:
            continue

        if system == "ww":
            if dry_run:
                ans = {
                    "llm_response": "[dry-run: no WW call]",
                    "raw": {"dry_run": True},
                    "status": "dry_run",
                }
            else:
                assert client is not None
                ans = probe_ww(client, entity_id, chat, probe.question, dry_run=False)
            llm_response = ans.get("llm_response") or ""
            raw_extract = ans
        elif system == "b1":
            prompt = answer_b1(probe.question, chat, max_chars=b1_max_chars)
            if dry_run:
                llm_response = "[dry-run: b1 prompt built]"
            else:
                llm_response = _simple_llm_answer(
                    prompt, json_mode=False, model=ans_model
                )
            raw_extract = {"prompt_chars": len(prompt), "system": "b1"}
        elif system == "b2":
            prompt = answer_b2(probe.question, chat, top_k=b2_top_k)
            if dry_run:
                llm_response = "[dry-run: b2 prompt built]"
            else:
                llm_response = _simple_llm_answer(
                    prompt, json_mode=False, model=ans_model
                )
            raw_extract = {"prompt_chars": len(prompt), "system": "b2"}
        else:
            raise ValueError(f"unknown system: {system}")

        judgment = judge_one(
            probe.ability,
            probe.question,
            probe.ideal,
            probe.rubric,
            llm_response if not dry_run else "",
            llm_chat=None,
            model=judge_model,
            temperature=DEFAULT_JUDGE_TEMP,
        )
        if use_llm_judge and not dry_run:
            # Live judge: same helper; json_mode so judge can emit structured scores
            # (judge.py also parses free-text JSON as fallback).
            def _chat(p: str, _m: Optional[str] = ans_model) -> str:
                return _simple_llm_answer(p, json_mode=True, model=_m)

            judgment = judge_one(
                probe.ability,
                probe.question,
                probe.ideal,
                probe.rubric,
                llm_response,
                llm_chat=_chat,
                model=judge_model,
                temperature=DEFAULT_JUDGE_TEMP,
            )

        row = {
            "key": key,
            "system": system,
            "scale": chat.scale,
            "chat_id": chat.chat_id,
            "entity_id": entity_id,
            "ability": probe.ability,
            "probe_index": probe.index,
            "question": probe.question,
            "llm_response": llm_response,
            "raw_extract": {
                k: raw_extract.get(k)
                for k in ("status", "prompt_chars", "system", "dry_run")
                if isinstance(raw_extract, dict) and k in raw_extract
            },
            "judgment": judgment,
            "seed": seed,
            "run_tag": run_tag,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _append_jsonl(answers_path, row)
        done.add(key)
        rows.append(row)
    return rows


def _row_score(row: dict) -> float:
    j = row.get("judgment") or {}
    try:
        return float(j.get("score") if j.get("score") is not None else 0.0)
    except (TypeError, ValueError):
        return 0.0


def worst_case_rows(all_rows: List[dict], n: int = 20) -> List[dict]:
    """Lowest-score probe rows (real scores only; empty list if no rows)."""
    if not all_rows:
        return []
    ranked = sorted(all_rows, key=_row_score)
    return ranked[: max(0, int(n))]


def write_summary(
    out_dir: Path,
    scale: str,
    systems: Sequence[str],
    all_rows: List[dict],
    meta: dict,
) -> None:
    # Aggregate per system × ability
    by_sys: Dict[str, Dict[str, List[float]]] = {s: {a: [] for a in ABILITY_KEYS} for s in systems}
    for row in all_rows:
        s = row.get("system") or ""
        a = row.get("ability") or ""
        j = row.get("judgment") or {}
        if s in by_sys and a in by_sys[s]:
            try:
                by_sys[s][a].append(float(j.get("score") or 0.0))
            except (TypeError, ValueError):
                by_sys[s][a].append(0.0)

    scores: Dict[str, Any] = {}
    for s in systems:
        scores[s] = {}
        for a in ABILITY_KEYS:
            vals = by_sys[s][a]
            if not vals:
                scores[s][a] = {"n": 0, "mean": None, "pass_rate": None}
            else:
                mean = sum(vals) / len(vals)
                # pass from judgments when present
                passes = [
                    1
                    for row in all_rows
                    if row.get("system") == s
                    and row.get("ability") == a
                    and (row.get("judgment") or {}).get("pass")
                ]
                scores[s][a] = {
                    "n": len(vals),
                    "mean": round(mean, 4),
                    "pass_rate": round(sum(passes) / len(vals), 4),
                }
        (out_dir / f"scores_{s}.json").write_text(
            json.dumps(scores[s], indent=2), encoding="utf-8"
        )

    protocol_complete = bool(meta.get("protocol_complete"))
    official_claim = bool(meta.get("official_claim"))
    lines = [
        f"# BEAM eval summary — {scale}",
        "",
        f"- git: `{meta.get('git_sha')}`",
        f"- seed: `{meta.get('seed')}`",
        f"- judge_model: `{meta.get('judge_model')}` temp=`{meta.get('judge_temp')}`",
        f"- answer_model: `{meta.get('answer_model') or 'n/a'}`",
        f"- systems: {', '.join(systems)}",
        f"- protocol_complete: **{str(protocol_complete).lower()}**",
        f"- official_claim: **{str(official_claim).lower()}** "
        "(stays false until manager promotes; do not treat as official 100K)",
        "",
        "## Ability table (mean score)",
        "",
        "| ability | " + " | ".join(systems) + " |",
        "|---|" + "|".join(["---"] * len(systems)) + "|",
    ]
    for a in ABILITY_KEYS:
        cells = []
        for s in systems:
            m = scores.get(s, {}).get(a, {}).get("mean")
            n = scores.get(s, {}).get(a, {}).get("n") or 0
            cells.append("—" if m is None else f"{m:.3f} (n={n})")
        lines.append(f"| {a} | " + " | ".join(cells) + " |")

    worst = worst_case_rows(all_rows, n=20)
    lines.extend(["", "## Worst cases (lowest score, top 20)", ""])
    if not worst:
        lines.append("_No scored rows in this run (empty or all skipped via resume)._")
    else:
        lines.extend(
            [
                "| rank | score | system | chat | ability | probe | pass | rationale |",
                "|---:|---:|---|---|---|---:|---|---|",
            ]
        )
        for i, row in enumerate(worst, start=1):
            j = row.get("judgment") or {}
            score = _row_score(row)
            rationale = str(j.get("rationale") or "").replace("|", "/").replace("\n", " ")
            if len(rationale) > 120:
                rationale = rationale[:117] + "..."
            lines.append(
                f"| {i} | {score:.3f} | {row.get('system')} | {row.get('chat_id')} | "
                f"{row.get('ability')} | {row.get('probe_index')} | "
                f"{bool(j.get('pass'))} | {rationale} |"
            )
            # Also include a short response snippet under the table as details
        lines.append("")
        lines.append("### Worst-case response snippets")
        lines.append("")
        for i, row in enumerate(worst, start=1):
            resp = str(row.get("llm_response") or "").replace("\n", " ").strip()
            if len(resp) > 200:
                resp = resp[:197] + "..."
            q = str(row.get("question") or "").replace("\n", " ").strip()
            if len(q) > 160:
                q = q[:157] + "..."
            lines.append(
                f"{i}. **{row.get('system')}** `{row.get('chat_id')}` "
                f"{row.get('ability')}#{row.get('probe_index')} "
                f"score={_row_score(row):.3f}"
            )
            lines.append(f"   - Q: {q or '(empty)'}")
            lines.append(f"   - A: {resp or '(empty)'}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Mini harness ≠ official 100K.",
            "- Scores here may use heuristic judge when LLM judge is off.",
            "- `protocol_complete` means full chats/abilities with resume-safe probe coverage "
            "and live LLM judge; `official_claim` still requires manager promotion.",
            "- Never hand-edit score files.",
            "",
        ]
    )
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )


def expected_probe_keys(
    system: str,
    chat: BeamChat,
    *,
    max_abilities: int = 0,
    abilities: Optional[Sequence[str]] = None,
) -> Set[str]:
    """Keys that a full (non-dry, resume-safe) pass should cover for one chat."""
    ability_filter = set(abilities) if abilities else None
    seen_abilities: List[str] = []
    keys: Set[str] = set()
    for probe in chat.probes:
        if ability_filter and probe.ability not in ability_filter:
            continue
        if max_abilities > 0:
            if probe.ability not in seen_abilities:
                if len(seen_abilities) >= max_abilities:
                    continue
                seen_abilities.append(probe.ability)
        keys.add(f"{chat.chat_id}:{probe.ability}:{probe.index}:{system}")
    return keys


def compute_protocol_complete(
    *,
    dry_run: bool,
    use_llm_judge: bool,
    max_abilities: int,
    abilities: Optional[Sequence[str]],
    chat_filter: str,
    all_chat_ids: Sequence[str],
    processed_chat_ids: Sequence[str],
    systems: Sequence[str],
    out_dir: Path,
    scale: str,
    data_root: Path,
) -> bool:
    """True when full-scale live run finished with full probe coverage (resume-safe).

    ``official_claim`` remains false until a manager promotes the run.
    """
    if dry_run or not use_llm_judge:
        return False
    if max_abilities > 0:
        return False
    if abilities:
        return False
    if (chat_filter or "").strip():
        # Single-chat runs are not full protocol
        return False
    if not all_chat_ids:
        return False
    if set(str(c) for c in processed_chat_ids) != set(str(c) for c in all_chat_ids):
        return False

    for system in systems:
        answers_path = out_dir / f"answers_{system}.jsonl"
        done = _load_done_keys(answers_path)
        for cid in all_chat_ids:
            try:
                chat = load_chat(scale, str(cid), data_root)
            except FileNotFoundError:
                return False
            expected = expected_probe_keys(
                system, chat, max_abilities=0, abilities=None
            )
            if not expected:
                return False
            if not expected.issubset(done):
                return False
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ensure_env_loaded()
    ap = argparse.ArgumentParser(description="WW official BEAM runner skeleton")
    ap.add_argument("--scale", default="100K", choices=list(SCALES))
    ap.add_argument(
        "--systems",
        default="ww",
        help="comma list: ww,b1,b2",
    )
    ap.add_argument("--chat", default="", help="single chat id (e.g. 1)")
    ap.add_argument("--list-chats", action="store_true")
    ap.add_argument("--resume", action="store_true", help="skip keys already in jsonl")
    ap.add_argument("--dry-run", action="store_true", help="no live LLM/WW; path smoke")
    ap.add_argument("--data-root", default="", help="override WW_BEAM_DATA")
    ap.add_argument("--run-tag", default="", help="entity isolation tag")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-abilities", type=int, default=0, help="0=all; smoke use 1")
    ap.add_argument("--abilities", default="", help="comma filter of ability keys")
    ap.add_argument("--ingest-mode", default="turn", choices=["turn", "batch_n"])
    ap.add_argument("--batch-n", type=int, default=5)
    ap.add_argument("--max-turns", type=int, default=0, help="0=all; limit ingest")
    ap.add_argument(
        "--b1-max-chars",
        type=int,
        default=DEFAULT_B1_MAX_CHARS,
        help=(
            f"B1 context window chars (default {DEFAULT_B1_MAX_CHARS} for full 100K; "
            "lower for smoke)"
        ),
    )
    ap.add_argument("--b2-top-k", type=int, default=5)
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    ap.add_argument(
        "--answer-model",
        default="",
        help="B1/B2 answer model (else WW_BEAM_ANSWER_MODEL / WW_MODEL / DEFAULT_MODEL)",
    )
    ap.add_argument("--llm-judge", action="store_true", help="use LLM judge (else heuristic)")
    ap.add_argument("--base-url", default="", help="WW server for ww system")
    args = ap.parse_args(list(argv) if argv is not None else None)

    data_root = resolve_data_root(args.data_root or None)
    if args.list_chats:
        ids = list_chat_ids(args.scale, data_root)
        print(f"data_root={data_root}")
        print(f"scale={args.scale} chats={len(ids)}")
        for i in ids[:50]:
            print(i)
        if len(ids) > 50:
            print(f"... +{len(ids) - 50} more")
        return 0

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    for s in systems:
        if s not in ("ww", "b1", "b2"):
            print(f"unknown system: {s}", file=sys.stderr)
            return 2

    run_tag = args.run_tag or f"r{int(time.time())}"
    ability_list = [a.strip() for a in args.abilities.split(",") if a.strip()] or None
    answer_model = (args.answer_model or "").strip()
    if answer_model:
        os.environ["WW_BEAM_ANSWER_MODEL"] = answer_model

    cfg = {
        "scale": args.scale,
        "systems": systems,
        "seed": args.seed,
        "ingest_mode": args.ingest_mode,
        "batch_n": args.batch_n,
        "max_turns": args.max_turns,
        "b1_max_chars": args.b1_max_chars,
        "b2_top_k": args.b2_top_k,
        "judge_model": args.judge_model,
        "judge_temp": DEFAULT_JUDGE_TEMP,
        "answer_model": answer_model or _resolve_answer_model(),
        "llm_judge": bool(args.llm_judge),
        "dry_run": bool(args.dry_run),
        "max_abilities": args.max_abilities,
        "abilities": ability_list,
    }
    out_dir = _results_dir(args.scale, cfg)
    print(f"results_dir={out_dir}")
    print(f"data_root={data_root}")

    all_available_chat_ids = list_chat_ids(args.scale, data_root)
    if args.chat:
        chat_ids = [str(args.chat)]
    else:
        chat_ids = list(all_available_chat_ids)
    if not chat_ids:
        print(
            f"No chats found under {data_root}/chats/{args.scale}. "
            "Set WW_BEAM_DATA or clone BEAM into ~/.ww/beam_cache (see docs/beam-eval.md).",
            file=sys.stderr,
        )
        return 1

    client = None
    if "ww" in systems and not args.dry_run:
        client = WWRunClient(base_url=args.base_url)
        if not client.key:
            print(
                "WW_API_KEY missing for live ww system "
                "(set env or ~/.ww/api_key). Use --dry-run for path smoke.",
                file=sys.stderr,
            )
            return 1

    all_rows: List[dict] = []
    processed_chat_ids: List[str] = []
    for cid in chat_ids:
        try:
            chat = load_chat(args.scale, cid, data_root)
        except FileNotFoundError as e:
            print(f"skip chat {cid}: {e}", file=sys.stderr)
            continue
        processed_chat_ids.append(str(cid))
        entity_id = entity_for(args.scale, cid, run_tag)
        # Isolation: unique entity per chat — never share poisoned session
        for system in systems:
            answers_path = out_dir / f"answers_{system}.jsonl"
            done: Set[str] = set()
            if args.resume:
                done = _load_done_keys(answers_path)
            rows = run_system_on_chat(
                system,
                chat,
                entity_id=entity_id,
                client=client,
                dry_run=bool(args.dry_run),
                max_abilities=int(args.max_abilities),
                abilities=ability_list,
                done=done,
                answers_path=answers_path,
                seed=args.seed,
                run_tag=run_tag,
                ingest_mode=args.ingest_mode,
                batch_n=args.batch_n,
                max_turns=args.max_turns,
                b1_max_chars=args.b1_max_chars,
                b2_top_k=args.b2_top_k,
                judge_model=args.judge_model,
                use_llm_judge=bool(args.llm_judge),
                answer_model=answer_model,
            )
            all_rows.extend(rows)
            print(
                f"[{system}] chat={cid} turns={len(chat.turns)} "
                f"probes_written={len(rows)} entity={entity_id}"
            )

    protocol_complete = compute_protocol_complete(
        dry_run=bool(args.dry_run),
        use_llm_judge=bool(args.llm_judge),
        max_abilities=int(args.max_abilities),
        abilities=ability_list,
        chat_filter=str(args.chat or ""),
        all_chat_ids=all_available_chat_ids,
        processed_chat_ids=processed_chat_ids,
        systems=systems,
        out_dir=out_dir,
        scale=args.scale,
        data_root=data_root,
    )

    # Prefer full jsonl (resume-safe) over this-session-only rows for summary/worst-case
    summary_rows = _load_answer_rows(out_dir, systems) or all_rows

    meta = {
        "git_sha": _git_sha(),
        "scale": args.scale,
        "systems": systems,
        "seed": args.seed,
        "run_tag": run_tag,
        "data_root": str(data_root),
        "judge_model": args.judge_model,
        "judge_temp": DEFAULT_JUDGE_TEMP,
        "answer_model": answer_model or _resolve_answer_model(),
        "llm_judge": bool(args.llm_judge),
        "config": cfg,
        "config_hash": _config_hash(cfg),
        "results_dir": str(out_dir),
        "timestamps": {
            "finished": datetime.now(timezone.utc).isoformat(),
        },
        "protocol_complete": protocol_complete,
        "official_claim": False,  # manager must promote; never auto-claim
        "chats_processed": len(processed_chat_ids),
        "chats_available": len(all_available_chat_ids),
        "rows_this_session": len(all_rows),
        "rows_in_summary": len(summary_rows),
        "note": (
            "Gate 0.6+ runner — official_claim stays false until manager promotes. "
            "protocol_complete reflects full-scale live coverage only."
        ),
    }
    write_summary(out_dir, args.scale, systems, summary_rows, meta)
    print(f"wrote {out_dir / 'summary.md'}")
    print(f"protocol_complete={str(protocol_complete).lower()}")
    print("official_claim=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
