# Coding Engine (WW-PM 0.9)

WorldWave's default engineering harness lives in `coding/`. PM 0.9 productizes the coding path so real tasks go through a closed loop: **mode → map/grep/graph → edit → verify → circuit/replan**, with steerable redirect and AutoCompact.

## Default path

| Step | Module / tool | Behavior |
|------|---------------|----------|
| Coding mode | `mode.py` | Auto-detect coding goals; inject CODING_AGENT essence + AGENTS.md; role=**coder** |
| Orchestrate | `orchestrator.py` / `coding_run_ticket` | repo_map → locate → edit → verify → one replan / circuit handoff |
| Steer | `coding_redirect` / `apply_redirect` | Mid-task subgoal/plan update (observable) |
| AutoCompact | `autocompact.py` | Structured summary near token budget; keeps edit_log |
| Require test | policy default | `WW_CODING_REQUIRE_TEST=1` — mark_ticket_done needs green verify |

```
coding mode → repo_map → grep/graph → outline → edit_symbol → verify
                                              ↘ fail → circuit + replan (×1) → handoff
```

## Capabilities

| Layer | Modules | Tools (selection) |
|-------|---------|-------------------|
| Mode | `mode` | auto inject; `is_coding_goal` |
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

## Permissions

`register_tools` honors each tool's `permission` key:

- Read-only perception tools → `safe`
- Write / exec tools → `requires_approval` or `destructive`

## Playbook

See `coding/CODING_AGENT.md` for the default-path diagram and worktree guidance.

## Scale fixture

```bash
python scripts/coding_scale_fixture.py --out tests/fixtures/coding_scale --count 200
```

Gates: graph_build completes; repo_map truncates under budget; grep finds a known symbol; time bound configurable.

## Prove

```bash
python scripts/coding_prove.py --all
python scripts/coding_prove.py --e2e
python -m pytest tests/test_coding_upgrade.py tests/test_coding.py tests/test_coding_agent_path.py -q --tb=line -k "not lsp"
```

- **V1–V10** — who_calls, blast_radius, deny, microcompact, rollback, AST edit, circuit, causal, secret, meta.
- **E1–E6** — failing test setup → graph/grep → edit_symbol fix → verify green → circuit trip → secret/deny/causal still pass.
