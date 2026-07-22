# BEAM memory eval notes (ops + skill mirror)

In-repo mirror of beam-memory-eval operational notes for Apple / Banana.
External skill files under `~/.hermes/skills/` may copy this later.

## What is / is not official

| Artifact | Official 100K? |
|----------|----------------|
| `pytest` beam P0/P1/P2 unit tests | No — engineering gate only |
| `beam_diag_chat.py` offline/live | No — P0.1 coverage / retrieval diag |
| `beam_mini_prove.py` | No — Gate 0 honesty mini |
| `memory_prove.py --product` / `--narrative` | No — Gate 0 product |
| `beam_runner.py --scale 100K` full protocol | **Candidate** — still `official_claim=false` until manager promotion |

## Gates (order)

1. **Engineering green (V0)** — unit tests, no network LLM:
   ```bash
   cd ~/worldwave
   .venv/bin/python -m pytest -q \
     tests/test_beam_p0_remediation.py \
     tests/test_beam_p1_p2.py \
     tests/test_memory_vnext.py --tb=short
   ```
2. **Diag (V1)** — offline always; live optional when key+balance:
   ```bash
   export WW_PROVE_URL=http://127.0.0.1:9300
   .venv/bin/python scripts/beam_diag_chat.py --chat 1 --scale 100K
   # optional live atom-search note:
   .venv/bin/python scripts/beam_diag_chat.py --chat 1 --scale 100K \
     --url "$WW_PROVE_URL" --live
   ```
3. **Gate 0 (V2)** — same PID, no wipe mid-run:
   ```bash
   export WW_PROVE_URL=http://127.0.0.1:9300
   export WW_PROVE_SKIP_L0=1
   .venv/bin/python scripts/memory_prove.py --product
   .venv/bin/python scripts/memory_prove.py --narrative
   PYTHONUNBUFFERED=1 .venv/bin/python -u scripts/beam_mini_prove.py  # 10/10
   # do not restart server; mini again → 10/10
   ```
4. **Single-chat / diag set (V3 / P0.6)** — WW mean ≥ B2 mean on fixed set before full 100K claim.
5. **Full 100K (V4 / P2.5)** — only after V3:
   ```bash
   .venv/bin/python scripts/beam_runner.py --scale 100K --systems ww,b1,b2 \
     --llm-judge --resume --run-tag rem100k_<git>
   ```

## Fail-fast + status fields

- `ApiCollapseGuard` threshold **10** consecutive empty answers → fatal `api_collapse_suspected`.
- WW `raw_extract` fields on probe rows:
  - `status`: `completed` | `interrupted` | `api_empty` | `error`
  - `retrieval_hits`: int from `state_metrics.beam_retrieval`
  - `retrieval_empty`: bool
- Interrupted target on probe path: **≤1%**.

## official_claim = false

- Default in every new run `meta.json`.
- Never flip true from mini/diag/unit/partial resume.
- Manager-only after S1–S5 and S7 (see destination contract).

## Product iron rules

- **One memory system** (v-next SoT). No dual-inject / dual-track regression.
- search/recall must hit **atoms** (narrative atom_hit green).
- `public_reply` must never dump memory tools.
- Entity isolation on sequential live runs.
- English code/commits; no secrets; no external AI vendor names in commits.

## P0–P2 code map (short)

| Layer | Modules |
|-------|---------|
| Probe wrap + policies | `core/beam_remediation.py` |
| Loop pre-search metrics | `core/loop.py` → `state_metrics.beam_retrieval` |
| Runner raw_extract | `scripts/beam_runner.py` (`probe_ww`, answer rows) |
| Fact extract + conflict | `core/memory/fact_extract.py` |
| Timeline | `core/memory/timeline.py` (wired in `vnext.ingest_turn`) |
| Diag | `scripts/beam_diag_chat.py` |
| Docs | `docs/beam-eval.md`, this file |

## Banana discipline

- Main port **9300**; health `GET /ww/health`.
- One `beam_runner` at a time; prefer `--resume` + skip-ingest when probes complete.
- Do not re-enable crash-loop systemd units for memory-v2/blockchain.
- Do not change judge/answer model mid-100K run.
- Do not open 500K/1M until 100K stop criteria are green and a new goal says so.

## Re-run checklist

1. Align git SHA on Banana with Apple HEAD.
2. Single healthy `server.py` on :9300.
3. Gate 0 product + narrative + mini×2 same PID.
4. Optional diag chat + single-chat beam if debugging KU/IE/temporal.
5. Full 100K with frozen judge/answer; new results dir `results/beam/100K/<git>_<cfg>/`.
6. Read `summary.md` / scores: mean WW vs max(B1,B2); KU/IE/temporal; interrupted rate.
7. Leave `official_claim=false` unless manager promotes.

## Residual (out of gbc code brief)

- Live Banana 100K / mini / memory_prove (Apple after push).
- Server restart decisions.
- Setting `official_claim=true`.
- 500K / 1M tiers.
