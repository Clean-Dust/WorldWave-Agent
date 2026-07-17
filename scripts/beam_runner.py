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


def entity_for(scale: str, chat_id: str, run_tag: str) -> str:
    return f"beam_{scale}_{chat_id}_{run_tag}"


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
    """Feed chat into real WW memory path turn-by-turn (not one giant prompt)."""
    turns = chat.turns
    if max_turns > 0:
        turns = turns[:max_turns]
    n = 0
    if dry_run:
        return {"entity_id": entity_id, "turns": len(turns), "dry_run": True}

    user_id = f"beam_u_{chat.chat_id}"
    chat_key = f"beam_c_{chat.chat_id}"
    mode = (ingest_mode or "turn").strip().lower()

    if mode == "batch_n":
        batch_n = max(1, int(batch_n))
        buf: List[str] = []
        for t in turns:
            buf.append(f"{t['role']}: {t['content']}")
            if len(buf) >= batch_n:
                goal = (
                    "Ingest the following conversation turns into memory. "
                    "Use remember for durable user facts. Acknowledge briefly.\n\n"
                    + "\n".join(buf)
                )
                client.run(
                    goal,
                    entity_id=entity_id,
                    platform="beam",
                    user_id=user_id,
                    chat_id=chat_key,
                    max_spirals=3,
                )
                n += len(buf)
                buf = []
        if buf:
            goal = (
                "Ingest the following conversation turns into memory. "
                "Use remember for durable user facts. Acknowledge briefly.\n\n"
                + "\n".join(buf)
            )
            client.run(
                goal,
                entity_id=entity_id,
                platform="beam",
                user_id=user_id,
                chat_id=chat_key,
                max_spirals=3,
            )
            n += len(buf)
    else:
        for t in turns:
            role = t["role"]
            content = t["content"]
            if role == "assistant":
                # Store assistant side as context note (product path still via /ww/run)
                goal = (
                    "Note this prior assistant message for conversation continuity "
                    f"(do not invent new user facts):\n{content[:2000]}"
                )
            else:
                goal = content
            client.run(
                goal,
                entity_id=entity_id,
                platform="beam",
                user_id=user_id,
                chat_id=chat_key,
                max_spirals=3,
            )
            n += 1
    return {"entity_id": entity_id, "turns_ingested": n, "dry_run": False}


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


def answer_b1(question: str, chat: BeamChat, max_chars: int = 12000) -> str:
    blob = chat_text_blob(chat)
    return b1_context_prompt(question, blob, max_chars=max_chars)


def answer_b2(question: str, chat: BeamChat, top_k: int = 5) -> str:
    blob = chat_text_blob(chat)
    return b2_rag_prompt(question, blob, top_k=top_k)


def _simple_llm_answer(prompt: str) -> str:
    """Optional live LLM for B1/B2 when keys present; else return prompt stub."""
    # Prefer in-process Worldwave LLM if available; skeleton keeps offline-safe.
    try:
        from core.llm import LLMClient  # type: ignore

        client = LLMClient({})
        out = client.chat(prompt)
        return str(out or "")
    except Exception:
        # Offline: return empty so judge records fail honestly (no fake scores)
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
) -> List[dict]:
    rows: List[dict] = []
    # Ingest once per chat for WW
    if system == "ww" and not dry_run and client is not None:
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
        ingest_ww(
            client or WWRunClient(),  # type: ignore
            chat,
            entity_id,
            dry_run=True,
            max_turns=max_turns,
        )

    ability_filter = set(abilities) if abilities else None
    seen_abilities: List[str] = []
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
                llm_response = _simple_llm_answer(prompt)
            raw_extract = {"prompt_chars": len(prompt), "system": "b1"}
        elif system == "b2":
            prompt = answer_b2(probe.question, chat, top_k=b2_top_k)
            if dry_run:
                llm_response = "[dry-run: b2 prompt built]"
            else:
                llm_response = _simple_llm_answer(prompt)
            raw_extract = {"prompt_chars": len(prompt), "system": "b2"}
        else:
            raise ValueError(f"unknown system: {system}")

        judgment = judge_one(
            probe.ability,
            probe.question,
            probe.ideal,
            probe.rubric,
            llm_response if not dry_run else "",
            llm_chat=None,  # wire LLM judge when use_llm_judge and keys present
            model=judge_model,
            temperature=DEFAULT_JUDGE_TEMP,
        )
        if use_llm_judge and not dry_run:
            # Optional live judge
            def _chat(p: str) -> str:
                return _simple_llm_answer(p)

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

    lines = [
        f"# BEAM eval summary — {scale}",
        "",
        f"- git: `{meta.get('git_sha')}`",
        f"- seed: `{meta.get('seed')}`",
        f"- judge_model: `{meta.get('judge_model')}` temp=`{meta.get('judge_temp')}`",
        f"- systems: {', '.join(systems)}",
        f"- official_claim: **false** (skeleton / Gate 0.6+; do not treat as official 100K)",
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
    lines.extend(
        [
            "",
            "## Worst cases",
            "",
            "_Placeholder: fill from lowest-score rows in answers_*.jsonl._",
            "",
            "## Notes",
            "",
            "- Mini harness ≠ official 100K.",
            "- Scores here may use heuristic judge when LLM judge is off.",
            "- Never hand-edit score files.",
            "",
        ]
    )
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
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
    ap.add_argument("--b1-max-chars", type=int, default=12000)
    ap.add_argument("--b2-top-k", type=int, default=5)
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
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
        "dry_run": bool(args.dry_run),
        "max_abilities": args.max_abilities,
        "abilities": ability_list,
    }
    out_dir = _results_dir(args.scale, cfg)
    print(f"results_dir={out_dir}")
    print(f"data_root={data_root}")

    if args.chat:
        chat_ids = [str(args.chat)]
    else:
        chat_ids = list_chat_ids(args.scale, data_root)
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
    for cid in chat_ids:
        try:
            chat = load_chat(args.scale, cid, data_root)
        except FileNotFoundError as e:
            print(f"skip chat {cid}: {e}", file=sys.stderr)
            continue
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
            )
            all_rows.extend(rows)
            print(
                f"[{system}] chat={cid} turns={len(chat.turns)} "
                f"probes_written={len(rows)} entity={entity_id}"
            )

    meta = {
        "git_sha": _git_sha(),
        "scale": args.scale,
        "systems": systems,
        "seed": args.seed,
        "run_tag": run_tag,
        "data_root": str(data_root),
        "judge_model": args.judge_model,
        "judge_temp": DEFAULT_JUDGE_TEMP,
        "config": cfg,
        "config_hash": _config_hash(cfg),
        "results_dir": str(out_dir),
        "timestamps": {
            "finished": datetime.now(timezone.utc).isoformat(),
        },
        "official_claim": False,
        "note": "Gate 0.6 skeleton — not official BEAM ICLR scores",
    }
    write_summary(out_dir, args.scale, systems, all_rows, meta)
    print(f"wrote {out_dir / 'summary.md'}")
    print("official_claim=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
