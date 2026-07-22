#!/usr/bin/env python3
"""P0.1 BEAM chat diagnostic — offline-friendly retrieval/coverage probe.

Usage::

    .venv/bin/python scripts/beam_diag_chat.py --chat 1 --scale 100K
    .venv/bin/python scripts/beam_diag_chat.py --chat 1 --scale 100K \\
        --data-root ~/.ww/beam_cache --url http://127.0.0.1:9300

Always writes markdown under results/beam/diag/ when chat data exists.
Exit 0 offline when data exists; exit 2 if chat missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from beam.baselines import SimpleBM25, b2_rag_prompt, chunk_text  # noqa: E402
from beam.data import BeamChat, chat_text_blob, load_chat, resolve_data_root  # noqa: E402


def _token_keywords(text: str, limit: int = 12) -> List[str]:
    toks = re.findall(r"[A-Za-z0-9_]{3,}", text or "")
    # Prefer rarer-looking tokens (longer first), drop stopish
    stop = {
        "the",
        "and",
        "for",
        "what",
        "when",
        "where",
        "which",
        "that",
        "this",
        "with",
        "from",
        "have",
        "was",
        "were",
        "are",
        "you",
        "your",
        "about",
        "please",
        "question",
        "answer",
        "how",
        "many",
        "much",
        "did",
        "does",
        "can",
        "could",
        "would",
        "should",
        "into",
        "over",
        "after",
        "before",
        "during",
        "conversation",
        "according",
    }
    seen = set()
    out: List[str] = []
    for t in sorted(toks, key=lambda x: (-len(x), x.lower())):
        tl = t.lower()
        if tl in stop or tl in seen:
            continue
        seen.add(tl)
        out.append(t)
        if len(out) >= limit:
            break
    # Always keep bare numbers
    for n in re.findall(r"\b\d{2,}\b", text or ""):
        if n not in out:
            out.append(n)
    return out[: limit + 5]


def _substring_hit_rate(keywords: Sequence[str], blob: str) -> Tuple[float, List[str], List[str]]:
    blob_l = (blob or "").lower()
    hits, miss = [], []
    for k in keywords:
        if (k or "").lower() in blob_l:
            hits.append(k)
        else:
            miss.append(k)
    rate = (len(hits) / len(keywords)) if keywords else 0.0
    return rate, hits, miss


def _sample_probes(chat: BeamChat, n: int = 3) -> List[Any]:
    if not chat.probes:
        return []
    # Prefer factual abilities if present
    prefer = (
        "knowledge_update",
        "information_extraction",
        "temporal_reasoning",
        "multi_session_reasoning",
    )
    picked: List[Any] = []
    for ab in prefer:
        for p in chat.probes:
            if p.ability == ab and p not in picked:
                picked.append(p)
                break
        if len(picked) >= n:
            return picked[:n]
    for p in chat.probes:
        if p not in picked:
            picked.append(p)
        if len(picked) >= n:
            break
    return picked[:n]


def _b2_snippets(question: str, blob: str, top_k: int = 5) -> List[Tuple[float, str]]:
    chunks = chunk_text(blob, chunk_chars=800, overlap=100)
    if not chunks:
        return []
    bm25 = SimpleBM25(chunks)
    hits = bm25.top_k(question, k=top_k)
    out: List[Tuple[float, str]] = []
    for idx, sc in hits:
        snip = chunks[idx]
        if len(snip) > 400:
            snip = snip[:397] + "..."
        out.append((float(sc), snip))
    return out


def _write_report(path: Path, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_diag(
    *,
    chat_id: str,
    scale: str,
    data_root: Optional[str] = None,
    url: str = "",
    live: bool = False,
) -> int:
    root = resolve_data_root(data_root)
    try:
        chat = load_chat(scale, str(chat_id), root)
    except FileNotFoundError as e:
        print(f"chat missing: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"failed to load chat: {e}", file=sys.stderr)
        return 2

    blob = chat_text_blob(chat)
    probes = _sample_probes(chat, 3)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = ROOT / "results" / "beam" / "diag" / f"chat_{chat.chat_id}_{ts}.md"

    lines: List[str] = [
        f"# BEAM diag — chat {chat.chat_id} ({scale})",
        "",
        f"- generated: `{datetime.now(timezone.utc).isoformat()}`",
        f"- data_root: `{root}`",
        f"- turns: **{len(chat.turns)}**",
        f"- probes: **{len(chat.probes)}**",
        f"- blob_chars: **{len(blob)}**",
        f"- mode: **offline**"
        + (f" + optional live url={url}" if url else ""),
        "",
        "## Sample probes vs B2 BM25 + keyword coverage",
        "",
    ]

    for i, p in enumerate(probes, 1):
        kws = _token_keywords(p.question)
        rate, hits, miss = _substring_hit_rate(kws, blob)
        snips = _b2_snippets(p.question, blob, top_k=3)
        lines.append(f"### Probe {i}: `{p.ability}` #{p.index}")
        lines.append("")
        qshow = p.question if len(p.question) < 300 else p.question[:297] + "..."
        lines.append(f"- question: {qshow}")
        lines.append(
            f"- keyword_substring_hit_rate: **{rate:.2f}** "
            f"({len(hits)}/{len(kws)})"
        )
        lines.append(f"- keywords_hit: {', '.join(hits) or '—'}")
        lines.append(f"- keywords_miss: {', '.join(miss) or '—'}")
        lines.append("- B2 top snippets:")
        if not snips:
            lines.append("  - _(empty)_")
        else:
            for sc, sn in snips:
                safe = sn.replace("\n", " ")
                lines.append(f"  - score={sc:.3f}: {safe}")
        lines.append("")

    # Offline fact-extract smoke on first few user turns
    lines.append("## Deterministic fact extract (offline sample)")
    lines.append("")
    try:
        from core.memory.fact_extract import extract_durable_facts

        sample_facts: List[Dict[str, str]] = []
        for t in chat.turns[:40]:
            if (t.get("role") or "") != "user":
                continue
            sample_facts.extend(extract_durable_facts(str(t.get("content") or "")))
            if len(sample_facts) >= 15:
                break
        if not sample_facts:
            lines.append("_No durable facts extracted from first user turns._")
        else:
            lines.append("| key | value |")
            lines.append("|---|---|")
            for f in sample_facts[:15]:
                lines.append(f"| `{f.get('key')}` | {f.get('value')} |")
    except Exception as e:
        lines.append(f"_fact_extract unavailable: {e}_")
    lines.append("")

    # Optional live path (only if url + key and --live)
    lines.append("## Live WW (optional)")
    lines.append("")
    api_key = (
        (os.environ.get("WW_API_KEY") or "").strip()
        or (
            (Path.home() / ".ww" / "api_key").read_text(encoding="utf-8").strip()
            if (Path.home() / ".ww" / "api_key").is_file()
            else ""
        )
    )
    if url and api_key and live:
        try:
            from beam.ww_client import WWRunClient

            client = WWRunClient(base_url=url, api_key=api_key, timeout=60.0)
            # Dry summary only — one tiny probe wrap, no full ingest
            from core.beam_remediation import build_beam_probe_goal

            q = probes[0].question if probes else "What facts do you remember?"
            goal = build_beam_probe_goal(q[:400])
            raw = client.run(
                goal[:1800],
                entity_id=f"beam_diag_{chat.chat_id}_{int(time.time())}",
                platform="beam",
                user_id=f"beam_diag_u_{chat.chat_id}",
                chat_id=f"beam_diag_c_{chat.chat_id}",
                max_spirals=2,
            )
            resp = str(raw.get("response") or "")[:500]
            lines.append(f"- live status: `{raw.get('status')}`")
            lines.append(f"- response_chars: {len(resp)}")
            lines.append(f"- response_preview: {resp or '_(empty)_'}")
            # Note atom / retrieval pre-search when product attaches metrics
            sm = raw.get("state_metrics") if isinstance(raw, dict) else None
            br = (sm or {}).get("beam_retrieval") if isinstance(sm, dict) else None
            if not br and isinstance(raw, dict):
                br = raw.get("beam_retrieval")
            if isinstance(br, dict):
                lines.append(
                    f"- atom_search / beam_retrieval: hits={br.get('retrieval_hits')} "
                    f"empty={br.get('retrieval_empty')}"
                )
            else:
                lines.append(
                    "- atom_search: live call completed (no beam_retrieval metrics "
                    "on response — server may predate P0 polish)."
                )
        except Exception as e:
            lines.append(f"- live call failed: `{e}`")
    else:
        lines.append(
            "- skipped (need `--url`, API key, and `--live` to call product `/ww/run`)."
        )
        lines.append(
            "- Offline report above still compares probe keywords vs chat blob "
            "and B2 BM25 snippets."
        )
        lines.append(
            "- Atom search note: available when `--live` hits a server that attaches "
            "`state_metrics.beam_retrieval` (retrieval_hits / retrieval_empty)."
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- P0.1 offline diag does not require LLM balance; live 100K is separate."
    )
    lines.append(
        "- Retrieval floor + fact extract land in product path "
        "(`core/beam_remediation.py`, `core/memory/fact_extract.py`)."
    )
    lines.append("")

    _write_report(out_path, lines)
    print(f"wrote {out_path}")
    print(f"turns={len(chat.turns)} blob_chars={len(blob)} probes={len(chat.probes)}")
    for i, p in enumerate(probes, 1):
        kws = _token_keywords(p.question)
        rate, hits, _miss = _substring_hit_rate(kws, blob)
        print(
            f"  probe{i} ability={p.ability} keyword_hit_rate={rate:.2f} "
            f"hits={len(hits)}/{len(kws)}"
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="BEAM P0.1 chat diagnostic")
    ap.add_argument("--chat", required=True, help="chat id (e.g. 1)")
    ap.add_argument("--scale", default="100K", choices=("100K", "500K", "1M"))
    ap.add_argument("--data-root", default="", help="override WW_BEAM_DATA / cache")
    ap.add_argument(
        "--url",
        default="",
        help="optional WW base URL for live dry probe (requires --live + API key)",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="actually call /ww/run (default: offline report only)",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)
    return run_diag(
        chat_id=str(args.chat),
        scale=args.scale,
        data_root=args.data_root or None,
        url=(args.url or "").strip(),
        live=bool(args.live),
    )


if __name__ == "__main__":
    raise SystemExit(main())
