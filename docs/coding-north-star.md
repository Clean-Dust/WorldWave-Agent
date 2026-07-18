# Coding North Star (WW-PM 0.12)

**True north:** exceed frontier coding agents on real multi-file engineering work.  
**Product essence:** map → locate (grep/graph via index facade) → edit → verify → circuit/replan, with steerable redirect, bound context, deny-first policy, and honest metrics — 去芜存菁 of agent coding loops without cargo-cult complexity.

**Baseline foundation:** PM 0.10–0.11 (live multi-turn, CodingMetrics, model route, corpus stress, arena harness).  
**PM 0.12 hardens Outcome A closed-book:** real `run_ww_llm_agent` that **never** applies `gold_fix`.

> Do **not** claim “exceeds Claude Code / Codex” (or any external product) in README unless a full-suite **closed-book** arena report shows Outcome A met **and** product policy allows competitive claims. Default README language links here and reports how to run the arena only.

---

## What “done” means (A ∧ B ∧ C ∧ D ∧ E)

| Gate | Meaning |
|------|---------|
| **A** Closed-book arena | `WW_ARENA_LLM=1` runs real coding path on scaffold only; `gold_applied=false`; pass@1 > baseline on full suite (Apple runs with real keys) |
| **B** Blueprint essence | require_test default on; index facade; graph on default locate; ACI/circuit/redirect/autocompact/samples/adversarial/model_route wired |
| **C** Large codebase | scale + corpus + `coding_large_repo_prove.py` green offline |
| **D** Protocol smoke | MCP/ACP initialize + list + one read-only coding tool |
| **E** Honesty | docs PM 0.12; mock ≠ A; no README claim over external agents; CI green without API keys |

**Mock + gold is NOT Outcome A product success.** Mock proves the harness and WW path machinery; closed-book is the hard bar.

---

## Outcome A — Closed-book arena (PRIMARY)

| Requirement | Status definition |
|-------------|-------------------|
| Task suite | ≥20 multi-file tasks under `tests/fixtures/coding_arena/tasks/` |
| Hidden tests | Each task has `hidden_tests/` **not** shown in the agent prompt |
| WW mock path | Deterministic gold-fix **through** orchestrator/ACI (CI) — `mode=mock`, `gold_applied=true` |
| WW LLM path | `WW_ARENA_LLM=1` → closed-book driver: scaffold copy + goal/prompt only → multi-turn tools / orchestrator — **never** `_apply_gold_fix`, never reads `gold_fix` for edits |
| No silent mock-as-llm | If no API key / LLM unavailable: honest fail or skip with `mode=llm`, `gold_applied=false` |
| Reference baseline | Fixed simplified harness on the **same** tasks, timeouts, sandbox |
| Metrics | pass@1, tool rounds, wall time, circuit trips, dump violations, graph/grep, **failure_taxonomy** (`locate\|edit\|verify\|timeout\|thrash\|model`) |
| Timeout | `WW_ARENA_TIMEOUT` (mock default 45s; LLM default **300s** when unset) |
| Flags | `--smoke` (≤3), `--full`, `--vs-baseline` |
| Reports | JSON + Markdown under `results/coding_arena/` |
| Contract test | Offline unit test asserts LLM path never applies gold |

### Commands

```bash
# CI-safe smoke (mock)
python scripts/coding_arena.py --smoke
python scripts/coding_arena.py --full --vs-baseline

# Closed-book (needs API key; never applies gold)
WW_ARENA_LLM=1 WW_CODING_MODEL=your-model WW_ARENA_TIMEOUT=300 \
  python scripts/coding_arena.py --full --vs-baseline
```

### Outcome A met?

| Layer | When met |
|-------|----------|
| **Harness / mock** | ≥20 tasks, isolation, reports, mock CI green |
| **Closed-book path real** | Code + contract tests prove `gold_applied=false` on LLM path |
| **Product hard bar** | Full-suite closed-book WW pass@1 **>** baseline pass@1 |

If scores fail the hard bar, re-dispatch uses failure taxonomy in report JSON — do not claim north-star complete.

---

## Outcome B — Blueprint gates

| ID | Gate | Expectation |
|----|------|-------------|
| B1 | `require_test` default on | `WW_CODING_REQUIRE_TEST=1`; arena records `require_test=true` |
| B2 | **Index facade** | `coding/index_facade.py`: `build` / `update` / `query(kind)` unifies map, graph, outline/symbols, BM25 code_rag; `.ww` lifecycle; counters |
| B3 | Graph on default locate | Orchestrator default path uses facade → `graph_calls > 0` |
| B4 | redirect | ≥3 tasks `supports_redirect` |
| B5 | autocompact / microcompact | Counters when triggered |
| B6 | samples ≥ 2 | Hard tasks / `WW_CODING_SAMPLES` |
| B7 | adversarial | ≥5 tasks `adversarial: true` |
| B8 | circuit max_same_fp + replan | Metrics include trips/replans |
| B9 | model_route | `model_id` on results |
| B10 | ACI / policy | Existing prove V3/V5/V9 + e2e green |

---

## Outcome C — Large codebase

```bash
python scripts/coding_prove.py --scale
python scripts/coding_corpus_prove.py
python scripts/coding_large_repo_prove.py
```

- Cache: `~/.cache/worldwave/coding_corpus` or fixture cache (gitignored)
- Optional sparse clone when `WW_CODING_CORPUS_CLONE=1` (never vendors into main)
- Reports: `results/coding_large_repo/`

---

## Outcome D — Protocol smoke

```bash
python scripts/coding_protocol_smoke.py
```

- Prefer `core/mcp.py` (initialize + tools/list + read-only map/grep)
- ACP capabilities covered; LSP optional skip
- See `docs/coding-engine.md` § Protocol for IDE attach notes

---

## Outcome E — Honesty

- README does **not** claim exceeds Claude Code / Codex
- This doc + `docs/coding-engine.md` + `coding/CODING_AGENT.md` state: **mock ≠ A**; closed-book hard bar; **PM 0.12.0**
- CI stays green without API keys (mock + contract tests)
- `user_summary` / public reply: no raw tool dump

---

## Task layout

```
tests/fixtures/coding_arena/
  SOURCES.md
  tasks/<id>/
    task.json       # goal, gold_fix (mock only), flags
    prompt.md       # agent-visible notes
    scaffold/       # agent sees this
    hidden_tests/   # pass@1 only; never in agent prompt
```

`gold_fix` is used **only** by the mock WW driver. Closed-book LLM path must not read it for edit content.

---

## Prove matrix (offline)

```bash
python -m pytest tests/test_coding*.py -q --tb=line -k "not lsp" --timeout=30
python scripts/coding_prove.py --all --e2e --scale --live
python scripts/coding_arena.py --smoke --vs-baseline
python scripts/coding_arena.py --full --vs-baseline
python scripts/coding_large_repo_prove.py
python scripts/coding_protocol_smoke.py
```

---

## Version

| Field | Value |
|-------|-------|
| PM_VERSION | **0.12.0** |
| Prior | 0.11.0 (arena harness), 0.10.0 (live path) |

See also: `docs/coding-engine.md`, `coding/CODING_AGENT.md`.
