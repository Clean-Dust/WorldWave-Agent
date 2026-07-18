# Coding Engine (WW-PM 0.13.0-endpoint)

WorldWave's default engineering harness lives in `coding/`. PM **0.13.0-endpoint** delivers Hard Arena v2 (≥30), **SB1 strong_react baseline**, anti-thrash closed-book loop, large-repo `--real` dual corpus, and MCP/ACP with **≥3 coding tools**. Foundation (PM 0.12 20/22 vs weak baseline) is history only — see `docs/coding-north-star.md` for F1–F6.

## Default path

| Step | Module / tool | Behavior |
|------|---------------|----------|
| Coding mode | `mode.py` | Auto-detect coding goals; inject CODING_AGENT + AGENTS.md; role=**coder** |
| Model route | `model_route.py` | Prefer `WW_CODING_MODEL` (+ optional provider); fallback to main model |
| Index facade | `index_facade.py` | `build` / `update` / `query(map\|grep\|graph\|outline\|rag)` — unified `.ww` lifecycle |
| Orchestrate | `orchestrator.py` / `coding_run_ticket` | facade map → locate (graph+grep) → edit → verify → replan/circuit |
| Explain → replan | `perception` + `harness` | `coding_explain_failure` → `coding_replan` |
| Steer | `coding_redirect` / loop_bridge | Mid-task subgoal update |
| AutoCompact | `autocompact.py` + loop_bridge | Near token budget; keeps edit_log |
| Metrics | `CodingMetrics` | rounds, tools, verifies, redirects, trips, graph_calls, grep_calls, … |
| Require test | policy default | `WW_CODING_REQUIRE_TEST=1` |

```
coding mode → model route → index_facade.build
           → repo_map → grep/graph → outline → edit_symbol → verify
                                              ↘ fail → explain → circuit + replan → handoff
user message ──► loop_bridge (redirect | autocompact)
```

## Index facade (B2)

```python
from coding.index_facade import IndexFacade

fac = IndexFacade(project_root=".")
fac.build()                          # graph + BM25/code_rag under .ww/
fac.update(["pkg/foo.py"])           # incremental refresh
fac.query("map", token_budget=4000)
fac.query("grep", pattern="def add")
fac.query("graph", action="who_calls", target="add")
fac.query("outline", path="pkg/math_ops.py")
fac.query("rag", query="rate limit")
fac.metrics()                        # map/grep/graph/rag/symbol counters
```

Default orchestrator locate path goes through the facade so **graph_calls > 0**.

## Capabilities

| Layer | Modules | Tools (selection) |
|-------|---------|-------------------|
| Mode | `mode`, `model_route`, `loop_bridge` | auto inject; model prefer; user-message path |
| Index | `index_facade`, `code_graph`, `perception`, `code_rag` | map / grep / graph / outline / BM25 |
| ACI edits | `aci` | `coding_edit_symbol`, `coding_apply_patch`, … |
| Policy | `policy` | deny-first; secret scan; causal; require-test default ON |
| Verify | `harness`, `circuit` | `coding_verify`; same-fingerprint trip |
| Control | `orchestrator`, `planning` | `coding_run_ticket`, `coding_redirect`, … |
| Bound outputs | `microcompact`, `autocompact` | no raw tool dump as user reply |

## Code graph

- Directed multigraph: nodes = file / class / function; edges = calls, imports, inherits, defines.
- Python via stdlib `ast` (always). Store: `<project>/.ww/code_graph.db`.
- Successful edits append `<project>/.ww/edit_log.jsonl`.

## Policy

- Dangerous patterns denied (`rm -rf /`, `mkfs`, dangerous `dd`, `curl|bash`, …).
- `WW_CODING_DENY_EXTRA` for extra regex denials.
- Causal default **ON**; secret scan on patch/commit.
- Architect cannot edit; default role **coder**.
- `WW_CODING_REQUIRE_TEST` default **1**.

## Orchestrator bounds

| Env | Default | Meaning |
|-----|---------|---------|
| `WW_CODING_MAX_TOOL_ROUNDS` | 20 | Cap rounds per ticket |
| `WW_CODING_MAX_SAME_FP` | 3 | Same fingerprint → handoff |
| `WW_CODING_MAX_REPLANS` | 1 | Replans after verify fail |
| `WW_CODING_MODEL` | (unset) | Preferred coding model |
| `WW_CODING_LIVE_LLM` | 0 | Live prove mock unless 1 |
| `WW_ARENA_LLM` | 0 | Arena closed-book LLM path |
| `WW_ARENA_TIMEOUT` | 45 mock / 300 LLM | Per-task wall budget |
| `WW_CODING_SAMPLES` | 0 | Sample repair when k>0 |

User-facing `user_summary` never dumps raw tool JSON.

## Coding arena (PM 0.13 Hard Arena v2)

Hidden-test pass@1 vs **SB1** `baseline_kind=strong_react` (read/grep/write/tests; no graph/facade).

| Mode | Env | Behavior |
|------|-----|----------|
| **mock** (CI default) | `WW_ARENA_LLM` unset/0 | Applies `gold_fix` through WW orchestrator/ACI — deterministic |
| **llm** (closed-book) | `WW_ARENA_LLM=1` | Multi-turn coding tools from goal+scaffold only; **never** gold; honest fail if no API key |

```bash
python scripts/coding_arena.py --smoke
python scripts/coding_arena.py --full --vs-baseline

# Closed-book (Apple / local with keys)
WW_ARENA_LLM=1 WW_CODING_MODEL=your-model WW_ARENA_TIMEOUT=300 \
  python scripts/coding_arena.py --full --vs-baseline

# Optional legacy weak baseline (not used for F1 delta)
WW_ARENA_BASELINE=legacy_weak python scripts/coding_arena.py --full --vs-baseline
```

Report summary fields: `ww_pass_rate`, `baseline_pass_rate`, `baseline_kind`, `delta_pass_rate`, `thrash_rate`, `gold_applied_any`, `f1_pass_ok`, `f1_delta_ok`.

**Mock green ≠ F1 product success.** Closed-book `ww_pass_rate≥0.90` and `delta≥0.15` vs SB1 is the hard bar. See `docs/coding-north-star.md`.

## Protocol (MCP / ACP) — attaching from other agents

WorldWave exposes coding tools so Cursor / other agents can attach without embedding WW internals.

### Minimum surface (≥3 tools)

| Tool | Role |
|------|------|
| `coding_repo_map` | Signature-level map (token budgeted) |
| `coding_grep` | Project text search |
| `coding_edit_symbol` / `coding_verify` | AST edit + test verify |

`scripts/coding_protocol_smoke.py` **fails** if `tools/list` returns fewer than 3 coding tools (no empty skip-pass).

### MCP (preferred)

- Implementation: `core/mcp.py` (`WWMCPServer` over stdio JSON-RPC).
- Handshake: `initialize` → `notifications/initialized` → `tools/list` → `tools/call`.
- Bootstraps `coding.register_tools` when the process registry is empty.

```bash
python scripts/coding_protocol_smoke.py
```

Example IDE / agent config (stdio; set `cwd` to your WorldWave checkout — do not commit absolute personal paths):

```json
{
  "mcpServers": {
    "worldwave": {
      "command": "python",
      "args": ["-c", "import asyncio; from core.mcp import WWMCPServer; asyncio.run(WWMCPServer().run_stdio())"],
      "cwd": "."
    }
  }
}
```

From another agent: list tools, call `coding_repo_map` / `coding_grep` read-only first, then `coding_edit_symbol` + `coding_verify` for write loops. Prefer a long-lived `ww` process with the full registry when available.

### ACP

- Implementation: `core/acp.py` — capabilities over stdio (`ready`, `capabilities`, tool invoke).
- `register_tools_as_capabilities()` bootstraps coding tools (≥3).

### LSP

Optional. If language servers are missing, protocol smoke skips LSP without failing the gate.

## Large repo / corpus

```bash
python scripts/coding_corpus_prove.py
python scripts/coding_large_repo_prove.py
python scripts/coding_large_repo_prove.py --real   # ≥2 allowlisted pure-Python repos in cache
python scripts/coding_prove.py --scale
```

- Self-bootstrap: in-repo `coding/` + `core/`
- Cache: `~/.cache/worldwave/coding_corpus` (allowlist shallow clones; never vendor into main)
- C-task: arena tasks tagged `realrepo` (≥3)
- Reports: `results/coding_large_repo/`

## Head-to-head (F2b optional)

```bash
python scripts/coding_h2h.py --suite hard_subset10
```

If neither `claude` nor `codex` is on PATH: exit 0 with `skipped=true`, `external_claim=forbidden`.

## Scale fixture

```bash
python scripts/coding_scale_fixture.py --out tests/fixtures/coding_scale --count 207
```

## Prove

```bash
python scripts/coding_prove.py --all
python scripts/coding_prove.py --e2e
python scripts/coding_prove.py --scale
python scripts/coding_prove.py --live
python scripts/coding_corpus_prove.py
python scripts/coding_large_repo_prove.py
python scripts/coding_protocol_smoke.py
python scripts/coding_arena.py --smoke --vs-baseline
python -m pytest tests/test_coding*.py -q --tb=line -k "not lsp" --timeout=30
```

## Playbook

See `coding/CODING_AGENT.md` for the default-path diagram and worktree guidance.

## Honesty

Do not claim the coding engine exceeds Claude Code / Codex in README. Arena scores and this north-star doc are the source of truth for product gates.
