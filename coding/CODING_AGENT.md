# CODING_AGENT.md — WorldWave Coding Playbook

Default engineering harness for coding tasks. Prefer this sequence over ad-hoc shell edits.

## Loop

1. **Map** — `coding_repo_map` for a ranked signature overview (token-budgeted).
2. **Grep** — `coding_grep` (ripgrep if present, else `grep -R`) for exact text.
3. **Graph** — `coding_graph_build` once per project; then:
   - `coding_graph_who_calls` before changing a leaf API
   - `coding_graph_blast_radius` before changing a hub
   - `coding_graph_hubs` / `coding_graph_path` for orientation
4. **Outline** — `coding_outline` on the target file for line-accurate symbols.
5. **Edit** — Prefer `coding_edit_symbol` (AST, syntax check, rollback). Use `coding_apply_patch` for multi-hunk unified diffs. Avoid raw `rm`/`dd`/pipe-to-shell.
6. **Verify** — `coding_verify` (execution-grounded). Causal policy blocks `git commit` until verify is green after coding writes.
7. **Explain / replan** — On failure: `coding_explain_failure` → `coding_replan` with failure fingerprints. Circuit trips after 3 same-fingerprint strikes → handoff report, stop thrashing.
8. **Optional** — `coding_sample_repair` when `WW_CODING_SAMPLES=k>0`; `coding_adversarial_tests` for edge drafts; `coding_worktree_start` / `finish` for isolation.

## Policy (deny-first)

- `coding_exec` / `coding_sandbox_exec` block `rm -rf /`, `mkfs`, dangerous `dd`, `curl|bash`, etc. Extend via `WW_CODING_DENY_EXTRA`.
- Secret scan blocks `sk-…`, `api_key=…`, `PRIVATE KEY` on patch/commit.
- Architect role cannot edit; default role is **coder**.
- `WW_CODING_CAUSAL=0` disables the post-edit commit gate (default ON).
- `WW_CODING_REQUIRE_TEST=1` makes `coding_mark_ticket_done` require a green verify.

## Tool search

`coding_tool_search` remains available for discovering tools by description when unsure.
