# Coding Engine (WW-PM 0.10)

WorldWave's default engineering harness lives in `coding/`. PM 0.10 hardens the **live multi-turn path**: mode → map/grep/graph → edit → verify → circuit/replan, with steerable redirect, AutoCompact, CodingMetrics, model route, and corpus stress — all offline-provable.

## Default path

| Step | Module / tool | Behavior |
|------|---------------|----------|
| Coding mode | `mode.py` | Auto-detect coding goals (bugfix / implement / refactor / write tests, EN+ZH); inject CODING_AGENT + AGENTS.md; role=**coder** |
| Model route | `model_route.py` | Prefer `WW_CODING_MODEL` (+ optional `WW_CODING_PROVIDER`); fallback to main model + log |
| Orchestrate | `orchestrator.py` / `coding_run_ticket` | repo_map → locate → edit → verify → replan/circuit; `max_tool_rounds` / `max_same_fp` bounds |
| Explain → replan | `perception` + `harness` | `coding_explain_failure` bullets folded into `coding_replan` context |
| Steer | `coding_redirect` / loop_bridge | Mid-task subgoal update via tool **or** simulated loop user-message path |
| AutoCompact | `autocompact.py` + loop_bridge | Near token budget (or mock over threshold); keeps edit_log |
| Metrics | `CodingMetrics` | JSON: rounds, tools, verifies, redirects, trips, autocompacts |
| Require test | policy default | `WW_CODING_REQUIRE_TEST=1` — mark_ticket_done needs green verify |

```
coding mode → model route → repo_map → grep/graph → outline → edit_symbol → verify
                                                      ↘ fail → explain → circuit + replan → handoff
user message ──► loop_bridge (redirect | autocompact)
```

## Capabilities

| Layer | Modules | Tools (selection) |
|-------|---------|-------------------|
| Mode | `mode`, `model_route`, `loop_bridge` | auto inject; model prefer; user-message path |
| Perception | `code_graph`, `perception`, `code_search`, `code_rag` | `coding_repo_map`, `coding_grep`, `coding_graph_*`, `coding_outline` |
| ACI edits | `aci` | `coding_edit_symbol`, `coding_apply_patch`, `coding_edit_lines`, `coding_write_file` |
| Policy | `policy` | deny-first; secret scan; causal commit gate; require-test default ON |
| Verify | `harness`, `circuit` | `coding_verify`, circuit 3-strike + same-fingerprint trip |
| Control | `orchestrator`, `harness`, `planning` | `coding_run_ticket`, `coding_redirect`, `coding_replan`, `coding_mark_ticket_done` |
| Bound outputs | `microcompact`, `autocompact` | tool microcompact + structured coding summary |

## Code graph

- Directed multigraph: nodes = file / class / function; edges = calls, imports, inherits, defines.
- Python via stdlib `ast` (always). Optional tree-sitter remains progressive elsewhere.
- Store: `<project>/.ww/code_graph.db` with mtime + content-hash incremental updates.
- Successful edits append `<project>/.ww/edit_log.jsonl` (AutoCompact never deletes this).

## Policy

- Dangerous patterns denied with semantic reasons (`rm -rf /`, `mkfs`, dangerous `dd`, `curl|bash`, …).
- Extra denials: `WW_CODING_DENY_EXTRA` (comma-separated regexes).
- Causal default **ON** (`WW_CODING_CAUSAL=0` disables): after coding writes, `git_commit` / `check_git_commit_allowed()` blocks until `coding_verify` is green.
- Secret scan blocks `sk-…`, `api_key=…`, `PRIVATE KEY` on patch/commit.
- Capability mutex: **architect cannot edit**; default role is **coder**.
- `WW_CODING_REQUIRE_TEST` default **1**; `WW_CODING_SAMPLES` default **0**.
- Optional worktree isolation: `WW_CODING_USE_WORKTREE=1` and/or `coding_worktree_start` / `coding_worktree_finish` (see CODING_AGENT.md). No new hard dependencies.

## Orchestrator bounds (PM 0.10)

| Env | Default | Meaning |
|-----|---------|---------|
| `WW_CODING_MAX_TOOL_ROUNDS` | 20 | Cap map/locate/edit/verify rounds per ticket |
| `WW_CODING_MAX_SAME_FP` / `WW_CODING_SAME_FP_THRESHOLD` | 3 | Same fingerprint strikes → handoff |
| `WW_CODING_MAX_REPLANS` | 1 | Replans after verify fail |
| `WW_CODING_MODEL` | (unset) | Preferred coding model |
| `WW_CODING_PROVIDER` | (unset) | Optional provider for coding model |
| `WW_CODING_LIVE_LLM` | 0 | Live prove uses mock driver unless 1 |
| `WW_CODING_SAMPLES` | 0 | Sample repair scaffolds when k>0 |
| `WW_CODING_USE_WORKTREE` | 0 | Documented opt-in for worktree isolation |

User-facing `user_summary` never dumps raw tool JSON (`tool_calls`, handler dumps, etc.).

## Corpus stress

Self-bootstrap on in-repo `coding/` + `core/` (always offline):

```bash
python scripts/coding_corpus_prove.py
```

Optional allowlist sparse clone to `~/.cache/worldwave/coding_corpus` or `tests/fixtures/coding_corpus_cache` (gitignored) when `WW_CODING_CORPUS_CLONE=1`. Scale fixture (≥200 / 207 py) remains green via `coding_prove.py --scale`.

## Scale fixture

```bash
python scripts/coding_scale_fixture.py --out tests/fixtures/coding_scale --count 207
```

Gates: graph_build completes; repo_map truncates under budget; grep finds a known symbol; time bound configurable.

## Prove

```bash
python scripts/coding_prove.py --all
python scripts/coding_prove.py --e2e
python scripts/coding_prove.py --scale
python scripts/coding_prove.py --live   # or scripts/coding_live_prove.py
python scripts/coding_corpus_prove.py
python -m pytest tests/test_coding_upgrade.py tests/test_coding.py \
  tests/test_coding_agent_path.py tests/test_coding_live.py -q --tb=line -k "not lsp"
```

- **V1–V10** — who_calls, blast_radius, deny, microcompact, rollback, AST edit, circuit, causal, secret, meta.
- **E1–E6** — failing test setup → graph/grep → edit_symbol fix → verify green → circuit trip → secret/deny/causal still pass.
- **Live** — multi-turn fail → locate → fix → green → redirect → green; edit_log; metrics; no raw tool dump.
- **Corpus** — coding+core graph/map stress; scale cross-check.

## Playbook

See `coding/CODING_AGENT.md` for the default-path diagram and worktree guidance.
