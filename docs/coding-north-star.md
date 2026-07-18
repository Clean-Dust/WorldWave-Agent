# Coding North Star — Endpoint (WW-PM 0.13.0-endpoint)

**True north:** WorldWave coding is a measurable, honest engineering harness — map → locate → edit → verify → circuit — with a **hard closed-book arena**, a **strong same-model baseline (SB1)**, real medium-repo pressure, and open protocol attach.

**Stop when = F1 ∧ F2 ∧ F3 ∧ F4 ∧ F5 ∧ F6** (this document).

## Foundation only (not the endpoint)

| Milestone | Note |
|-----------|------|
| PM 0.12 closed-book path | Real `run_ww_llm_agent`, never applies `gold_fix` |
| Historical score | **20/22 vs weak baseline 2/22** (~868d450) — **foundation only**, not product complete |
| Index facade / large_repo self-bootstrap / MCP smoke | Necessary plumbing; tools_list must be ≥3 for F5 |

Do **not** celebrate foundation scores as the coding endpoint.

---

## F1 — Hard Arena v2 (PRIMARY)

| Requirement | Definition |
|-------------|------------|
| Suite size | ≥ **30** multi-file hidden-test tasks under `tests/fixtures/coding_arena/tasks/` |
| Harder additions | ≥8 multi-file / path / timezone / TDD / realrepo-style tasks |
| Closed-book | `WW_ARENA_LLM=1`; `gold_applied=false` for all WW rows; agent never sees hidden/gold |
| Thrash targets | `t09_path_join_safety`, `t16_timezone_naive` must be solvable (harness anti-thrash + prompts) |
| Coverage flags | redirect≥3, adversarial≥8, samples hard≥3 |
| Hard bars (Apple LLM) | `ww_pass_rate ≥ 0.90` **and** `delta_pass_rate ≥ 0.15` vs SB1 |
| Report fields | `ww_pass_rate`, `baseline_pass_rate`, `baseline_kind`, `delta_pass_rate`, `thrash_rate`, `gold_applied_any`, `f1_pass_ok`, `f1_delta_ok` |

```bash
# CI mock
python scripts/coding_arena.py --smoke
python scripts/coding_arena.py --full --vs-baseline

# Closed-book (Apple; needs keys)
WW_ARENA_LLM=1 WW_ARENA_TIMEOUT=300 \
  python scripts/coding_arena.py --full --vs-baseline
```

Mock + gold **≠** F1 product success.

---

## F2 — Strong baseline + optional external H2H

### F2a SB1 (required)

| ID | Definition |
|----|------------|
| **SB1** `baseline_kind=strong_react` | Same-model simplified multi-turn ReAct: **read / grep / write / run tests only** — **no** code_graph, **no** index_facade privileges; same timeout/sandbox/model env class |
| Legacy weak | `WW_ARENA_BASELINE=legacy_weak` optional regression only; **F1 delta uses SB1** |

### F2b Head-to-head (optional)

```bash
python scripts/coding_h2h.py --suite hard_subset10
```

| Case | Outcome |
|------|---------|
| No `claude`/`codex` on PATH | exit 0, `skipped=true`, `external_claim=forbidden` |
| CLI present + scored WW ≥ each opponent | `external_claim=allowed` (only then README may say exceeds) |
| Otherwise | **forbidden** — keep “powerful tools + persistent layer” tone |

---

## F3 — Blueprint essence (product-traceable)

| ID | Expectation |
|----|-------------|
| B1 | `require_test` default on; hidden pass is the arena truth |
| B2 | Default locate via **index_facade**; Hard runs show `graph_calls` and `grep_calls` ≫ 0 aggregate |
| B3 | `who_calls` / `blast_radius` used on ≥5 Hard tasks (metrics/trace) |
| B4 | ACI edit_symbol/patch + syntax rollback; secret/deny green |
| B5 | write→verify→fix; Hard thrash_rate **≤ 10%** (Apple verifies) |
| B6 | ≥3 redirect tasks; metrics.redirects observable |
| B7 | micro+autocompact countable on long tasks |
| B8 | samples hard ≥3 tasks record samples path |
| B9 | ≥8 adversarial including path/time |
| B10 | coding model route; `model_id` on results |

**Out of endpoint scope:** base-model training, full AdverMCTS productization, default eBPF, IDE marketplace extensions.

---

## F4 — Large repo real

```bash
python scripts/coding_large_repo_prove.py          # self-bootstrap offline
python scripts/coding_large_repo_prove.py --real   # ≥2 allowlisted public pure-Python repos in cache
```

- Cache: `~/.cache/worldwave/coding_corpus` (never vendor into main git)
- Per repo: graph_build, repo_map budget, grep anchors, JSON/MD report
- **C-task:** ≥3 arena tasks tagged `realrepo` (offline mock + closed-book structure)

---

## F5 — Protocol real tools

```bash
python scripts/coding_protocol_smoke.py   # fails if coding tools_list < 3
```

MCP and/or ACP must register **≥3** coding tools (`coding_repo_map`, `coding_grep`, `coding_edit_symbol` / `coding_verify`, …). No skip-pass on empty `tools/list`.

See `docs/coding-engine.md` § attaching from other agents.

---

## F6 — Docs / honesty / CI

| Item | Rule |
|------|------|
| This doc | Endpoint definition F1–F6 |
| PM | **0.13.0-endpoint** (`coding/__init__.py`, CODING_AGENT, coding-engine) |
| README | No “exceeds Claude Code/Codex” unless F2b `external_claim=allowed` |
| CI | Green without API keys; closed-book full is Apple-side |

---

## Commands (regression)

```bash
python scripts/coding_prove.py --all
python scripts/coding_prove.py --e2e --scale --live
python scripts/coding_arena.py --smoke --vs-baseline
python scripts/coding_large_repo_prove.py --real
python scripts/coding_protocol_smoke.py
python scripts/coding_h2h.py   # usually skipped
python -m pytest tests/test_coding*.py tests/test_transport.py -q --tb=line -k "not lsp"
```

---

## Handoff honesty

| Layer | Status owner |
|-------|--------------|
| Engineering endpoint (F1 harness + F2a + F3–F6 offline) | gbc push + offline green |
| F1 pass@1≥0.90 and delta≥0.15 | **Apple** closed-book LLM run |
| F2b external exceeds claim | Only if CLI present and scored win |

**Endpoint-engineering complete** ≠ **external-claim complete**.
