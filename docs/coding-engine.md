# Coding Engine (WW-PM 0.8)

WorldWave's default engineering harness lives in `coding/`. It turns coding from a pile of `coding_*` tools into a closed loop: **perceive → edit → verify → replan**.

## Capabilities

| Layer | Modules | Tools (selection) |
|-------|---------|-------------------|
| Perception | `code_graph`, `perception`, `code_search`, `code_rag` | `coding_repo_map`, `coding_grep`, `coding_graph_*`, `coding_outline`, `coding_ast_search`, `coding_call_graph` |
| ACI edits | `aci` | `coding_edit_symbol`, `coding_apply_patch`, `coding_edit_lines`, `coding_write_file` |
| Policy | `policy` | deny-first on `coding_exec` / `coding_sandbox_exec`; secret scan; causal commit gate |
| Verify | `harness`, `circuit` | `coding_verify`, circuit 3-strike + same-fingerprint trip |
| Control | `harness`, `planning` | `coding_replan`, `coding_mark_ticket_done`, `coding_tool_search` |
| Bound outputs | `microcompact` | all `coding_*` tool results head+tail + fingerprint (~6000 chars) |

## Code graph

- Directed multigraph: nodes = file / class / function; edges = calls, imports, inherits, defines.
- Python via stdlib `ast` (always). Optional tree-sitter remains progressive elsewhere.
- Store: `<project>/.ww/code_graph.db` with mtime + content-hash incremental updates.
- Successful edits append `<project>/.ww/edit_log.jsonl`.

## Policy

- Dangerous patterns denied with semantic reasons (`rm -rf /`, `mkfs`, dangerous `dd`, `curl|bash`, …).
- Extra denials: `WW_CODING_DENY_EXTRA` (comma-separated regexes).
- Causal default **ON** (`WW_CODING_CAUSAL=0` disables): after coding writes, `git_commit` / `check_git_commit_allowed()` blocks until `coding_verify` is green.
- Secret scan blocks `sk-…`, `api_key=…`, `PRIVATE KEY` on patch/commit.
- Capability mutex: **architect cannot edit**; default role is **coder**.

## Permissions

`register_tools` honors each tool's `permission` key:

- Read-only perception tools → `safe`
- Write / exec tools → `requires_approval` or `destructive`

## Playbook

See `coding/CODING_AGENT.md` for the recommended map → grep → graph → edit_symbol → verify loop.

## Prove

```bash
python scripts/coding_prove.py --all
python -m pytest tests/test_coding_upgrade.py tests/test_coding.py -q --tb=line -k "not lsp"
```

Assertions V1–V10 cover who_calls, blast_radius, deny policy, microcompact, rollback, AST edit, circuit trip, causal gate, secret scan, and prove exit code.
