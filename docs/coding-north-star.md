# Coding North Star (WW-PM 0.11)

**True north:** exceed frontier coding agents on real multi-file engineering work.  
**Product essence:** map → locate (grep/graph) → edit → verify → circuit/replan, with steerable redirect, bound context, deny-first policy, and honest metrics — 去芜存菁 of agent coding loops without cargo-cult complexity.

**Baseline foundation:** PM 0.10.0 (live multi-turn path, CodingMetrics, model route, corpus stress).

This document defines **Outcome A / B / C** for the coding arena.  
**Fixture-prove green alone is not success.** Arena pass@1 vs the reference baseline is the A-gate.

> Do **not** claim “exceeds Claude Code / Codex” (or any external product) in README unless a full-suite arena report shows Outcome A met **and** product policy allows competitive claims. Default README language links here and reports how to run the arena only.

---

## Outcome A — Arena infrastructure (primary)

| Requirement | Status definition |
|-------------|-------------------|
| Task suite | ≥20 multi-file tasks under `tests/fixtures/coding_arena/tasks/` (alt: `coding_arena/tasks/`) |
| Hidden tests | Each task has `hidden_tests/` **not** shown in the agent prompt |
| WW path | Run coding agent path (orchestrator / mode) in a sandbox workdir (scaffold only) |
| Reference baseline | Fixed simplified harness (single-shot naive patch) on the **same** tasks, timeouts, sandbox |
| Metrics | pass@1 (all hidden tests), tool rounds, wall time, circuit trips, dump violations |
| Flags | `--smoke` (≤3 tasks), `--full`, `--vs-baseline` |
| Reports | JSON + Markdown under `results/coding_arena/` (gitignored via `results/`) |
| Offline | Smoke/full mock mode needs **no network** |
| Inspired tasks | ≥5 tasks document real issue **patterns** in `SOURCES.md` (no vendored third-party commits) |

### Reference baseline (fair comparison)

| Dimension | WW | Baseline |
|-----------|----|----------|
| Timeout | `task.timeout_s` / `WW_ARENA_TIMEOUT` (default 45s) | Same |
| Sandbox | Temp workdir, scaffold copy only | Same |
| Model env | `WW_CODING_MODEL` (mock default `arena-mock-model`) | Same env; unused in mock baseline |
| Driver | Orchestrator: map → grep/graph → edit → verify (+ redirect / autocompact / samples when tagged) | Single-shot naive pattern edits only (no graph/grep/circuit) |
| Mock CI | Deterministic gold-fix application **through** WW path | Deterministic limited heuristics |

Mock mode is **default** (stable without API keys).  
Optional: `WW_ARENA_LLM=1` for a real LLM path (not required for CI).

### Commands

```bash
# CI-safe smoke (≤3 tasks)
python scripts/coding_arena.py --smoke

# Full suite + baseline comparison
python scripts/coding_arena.py --full --vs-baseline

# Optional real LLM (skip in CI)
WW_ARENA_LLM=1 WW_CODING_MODEL=your-model python scripts/coding_arena.py --smoke
```

### Outcome A met?

**A is met for infrastructure** when the harness loads ≥20 tasks, isolates hidden tests, runs WW + optional baseline, and writes reports.

**A “exceeds baseline”** is met only when full-suite WW pass@1 rate **>** baseline pass@1 rate.  
If WW ≤ baseline: still ship harness + report scores; **do not** write “north star complete” in README.

---

## Outcome B — Gates (wire + prove)

| ID | Gate | Arena / code expectation |
|----|------|---------------------------|
| B1 | `require_test` default on | `WW_CODING_REQUIRE_TEST=1`; arena records `require_test=true` on success path; mark_ticket_done gated |
| B2 | graph/grep usage metrics | `CodingMetrics.graph_calls` / `grep_calls` when locate uses them |
| B3 | secret / deny / rollback | Existing `coding_prove.py` V3/V5/V9 + e2e still green |
| B4 | redirect injection | ≥3 tasks with `supports_redirect`; arena exercises `coding_redirect` / loop_bridge |
| B5 | autocompact / microcompact counters | Metrics fields increment when triggered in arena path |
| B6 | samples ≥ 2 | Hard tasks set `samples≥2` / `WW_CODING_SAMPLES`; sample_repair path available |
| B7 | adversarial hidden tests | ≥5 tasks with `adversarial: true` edge cases |
| B8 | circuit max_same_fp + replan | Metrics include `max_same_fp`, `replans`, `trips` |
| B9 | model_route model id | Arena results record `model_id` from `resolve_coding_model` / `WW_CODING_MODEL` |

---

## Outcome C — Product honesty

- README links to this doc + arena run instructions.
- README does **not** claim exceeds external coding agents unless A exceed-baseline is met **and** explicitly approved.
- Arena metrics field `public_reply_dump_count` must be **0** for WW user summaries (no raw tool JSON as public reply).

---

## Task layout

```
tests/fixtures/coding_arena/
  SOURCES.md                 # inspired-by patterns (no third-party commits)
  tasks/
    t01_…/
      task.json              # id, goal, gold_fix, flags, timeout
      prompt.md              # agent-visible notes
      scaffold/              # multi-file buggy project (agent sees this)
      hidden_tests/          # NOT in agent prompt; used only for pass@1
```

`gold_fix` is used only by the **mock WW driver** to apply a correct edit through the real orchestrator/ACI path. It is not part of the agent-visible prompt.

---

## Prove matrix (keep green)

```bash
python scripts/coding_prove.py --all
python scripts/coding_prove.py --e2e
python scripts/coding_prove.py --scale
python scripts/coding_prove.py --live
python scripts/coding_arena.py --smoke
python scripts/coding_arena.py --full --vs-baseline
python -m pytest tests/test_coding*.py -q --tb=line -k "not lsp"
```

---

## Version

| Field | Value |
|-------|-------|
| PM_VERSION | **0.11.0** |
| Prior foundation | 0.10.0 |

See also: `docs/coding-engine.md`, `coding/CODING_AGENT.md`.
