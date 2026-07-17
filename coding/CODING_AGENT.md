# CODING_AGENT.md — WorldWave Coding Playbook

Default engineering harness for coding tasks (PM 0.10 live path). Prefer this sequence over ad-hoc shell edits.

## Default path (auto)

When a goal looks like coding (bugfix / implement / refactor / write tests — EN+ZH), WorldWave activates **coding mode**:

1. Inject this playbook essence into system context
2. Auto-load project `AGENTS.md` when present
3. Set capability role = **coder** (architect cannot edit)
4. Prefer `WW_CODING_MODEL` (optional `WW_CODING_PROVIDER`); fallback to main model
5. Prefer `coding_run_ticket` / the orchestrated loop below

```
┌─────────────┐
│ coding mode │  essence + AGENTS.md + role=coder + model route
└──────┬──────┘
       ▼
┌─────────────┐     ┌──────────┐     ┌────────────┐
│  repo_map   │ ──► │ grep /   │ ──► │  outline / │
│             │     │ graph    │     │  open      │
└─────────────┘     └──────────┘     └─────┬──────┘
                                           ▼
                                    ┌──────────────┐
                                    │ edit_symbol  │
                                    │ | apply_patch│
                                    └──────┬───────┘
                                           ▼
                                    ┌──────────────┐
                          fail ◄─── │   verify     │ ──► green → done
                            │       └──────────────┘
                            ▼
              explain_failure → circuit (same-fp) + replan
                            │
                    handoff (max_tool_rounds / max_same_fp)
```

## Loop steps

1. **Map** — `coding_repo_map` for a ranked signature overview (token-budgeted; truncates at scale).
2. **Grep** — `coding_grep` (ripgrep if present, else `grep -R`) for exact text.
3. **Graph** — `coding_graph_build` once per project; then:
   - `coding_graph_who_calls` before changing a leaf API
   - `coding_graph_blast_radius` before changing a hub
   - `coding_graph_hubs` / `coding_graph_path` for orientation
4. **Outline** — `coding_outline` on the target file for line-accurate symbols.
5. **Edit** — Prefer `coding_edit_symbol` (AST, syntax check, rollback). Use `coding_apply_patch` for multi-hunk unified diffs. Avoid raw `rm`/`dd`/pipe-to-shell.
6. **Verify** — `coding_verify` (execution-grounded). Causal policy blocks `git commit` until verify is green after coding writes.
7. **Explain / replan** — On failure: `coding_explain_failure` → `coding_replan` with failure fingerprints **and** explain bullets. Circuit trips after same-fingerprint strikes (`WW_CODING_MAX_SAME_FP`, default 3) → handoff report, stop thrashing. Bound total tool rounds with `WW_CODING_MAX_TOOL_ROUNDS` (default 20).
8. **Steer** — Mid-task user redirect via `coding_redirect` / `apply_redirect(message)`, or the loop user-message path (`coding.loop_bridge.handle_coding_user_message`) — updates subgoal/plan observably.
9. **AutoCompact** — Near context budget, `coding_autocompact` (also auto from loop_bridge when over threshold) keeps a structured summary (goal, files touched, tests, open issues) without destroying `.ww/edit_log.jsonl`.
10. **Optional** — `coding_sample_repair` when `WW_CODING_SAMPLES=k>0`; `coding_adversarial_tests` for edge drafts; **worktree isolation** via `coding_worktree_start` / `coding_worktree_finish` or `WW_CODING_USE_WORKTREE=1`.

## Orchestrator

`coding_run_ticket(goal, …)` runs the deterministic path:

`repo_map → grep/graph locate → edit_symbol|apply_patch → verify → on fail explain + circuit + replan`

- Same fingerprint threshold / max_tool_rounds → stop + structured handoff
- `user_summary` is reply-safe (never dump raw tool JSON as the user reply)
- `CodingMetrics` on the result: `rounds`, `tools`, `verifies`, `redirects`, `trips`, `autocompacts` (export via `.to_dict()` / `.export(path)`)

## Model route

```bash
export WW_CODING_MODEL=your-coding-model
export WW_CODING_PROVIDER=optional-provider   # optional
```

Coding mode prefers this model; if unset, falls back to the main agent model and logs the route.

## Policy (deny-first)

- `coding_exec` / `coding_sandbox_exec` block `rm -rf /`, `mkfs`, dangerous `dd`, `curl|bash`, etc. Extend via `WW_CODING_DENY_EXTRA`.
- Secret scan blocks `sk-…`, `api_key=…`, `PRIVATE KEY` on patch/commit.
- Architect role cannot edit; default role is **coder**.
- `WW_CODING_CAUSAL=0` disables the post-edit commit gate (default ON).
- `WW_CODING_REQUIRE_TEST` default **1** — `coding_mark_ticket_done` requires a green verify.
- `WW_CODING_SAMPLES` default **0** (sample repair off unless opted in).

## Worktree

For isolated multi-file work (optional; no new hard deps):

1. Set `WW_CODING_USE_WORKTREE=1` and/or call `coding_worktree_start` → new branch + worktree path
2. Edit/verify inside the worktree path
3. `coding_worktree_finish` with `action=remove` or `merge`

## Live prove (CI default = mock)

```bash
# Deterministic multi-turn (default)
WW_CODING_LIVE_LLM=0 python scripts/coding_prove.py --live
# Optional real LLM (skipped in default prove)
WW_CODING_LIVE_LLM=1 python scripts/coding_live_prove.py
```

## Tool search

`coding_tool_search` remains available for discovering tools by description when many tools are registered (coding mode hints this automatically).
