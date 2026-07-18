# Coding Engine (WW-PM 0.12)

WorldWave's default engineering harness lives in `coding/`. PM 0.12 hardens **closed-book arena** + **index facade** on the 0.10–0.11 foundation: mode → map/grep/graph → edit → verify → circuit/replan, with steerable redirect, AutoCompact, CodingMetrics, model route, corpus/large-repo stress, and protocol smoke — offline-provable for CI; closed-book LLM path for the product hard bar.

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

## Coding arena (PM 0.12)

Hidden-test pass@1 vs fixed reference baseline.

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
```

Reports: `results/coding_arena/` include `gold_applied`, `mode`, `failure_taxonomy`.

**Mock green ≠ Outcome A product success.** Closed-book pass@1 > baseline on the full suite is the hard bar. See `docs/coding-north-star.md`.

## Protocol (MCP / ACP) — IDE attach

WorldWave can expose coding tools to IDEs/agents via MCP or ACP.

### MCP (preferred)

- Implementation: `core/mcp.py` (`WWMCPServer` over stdio JSON-RPC).
- Handshake: `initialize` → `notifications/initialized` → `tools/list` → `tools/call`.
- Smoke (offline):

```bash
python scripts/coding_protocol_smoke.py
```

Example IDE config (stdio; adjust command to your install):

```json
{
  "mcpServers": {
    "worldwave": {
      "command": "python",
      "args": ["-c", "import asyncio; from core.mcp import WWMCPServer; asyncio.run(WWMCPServer().run_stdio())"],
      "cwd": "/path/to/worldwave"
    }
  }
}
```

Prefer registering the full tool registry in a long-lived `ww` process when available. Smoke uses in-process initialize + facade read-only map/grep so CI needs no daemon.

### ACP

- Implementation: `core/acp.py` — capabilities over stdio (`ready`, `capabilities`, tool invoke).
- Smoke registers `coding_repo_map` / `coding_grep` capabilities.

### LSP

Optional. If language servers are missing, protocol smoke skips LSP without failing the gate.

## Large repo / corpus

```bash
python scripts/coding_corpus_prove.py
python scripts/coding_large_repo_prove.py
python scripts/coding_prove.py --scale
```

- Self-bootstrap: in-repo `coding/` + `core/`
- Cache: `~/.cache/worldwave/coding_corpus` (optional clone with `WW_CODING_CORPUS_CLONE=1`)
- Reports: `results/coding_large_repo/`
- Never vendors third-party trees into the main repo

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
