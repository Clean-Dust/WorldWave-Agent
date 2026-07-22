# BEAM evaluation on WorldWave memory

## Gate order (product honesty first)

1. **Gate 0** — product honesty green on live `/ww/run` (plant → probe, no empty response, no memory-dump replies, entity isolation).
2. **Gate 0.x** — remember reliability, sequential multi-field conversation keys, public_reply hygiene.
3. **Official tiers** — BEAM **100K → 500K → 1M** only after Gate 0 stays green.

**Never treat mini harness scores as official 100K.**  
`scripts/beam_mini_prove.py` is a local honesty probe; it is **not** the ICLR 100K leaderboard path.

## Official runner

```bash
# List chats in local cache
.venv/bin/python scripts/beam_runner.py --scale 100K --list-chats

# Dry-run smoke (no live scores claimed)
.venv/bin/python scripts/beam_runner.py --scale 100K --systems b1,b2 --chat 1 --dry-run --max-abilities 1

# Live WW + baselines (requires server + WW_API_KEY or ~/.ww/api_key)
.venv/bin/python scripts/beam_runner.py --scale 100K --systems ww,b1,b2 --chat 1
.venv/bin/python scripts/beam_runner.py --scale 100K --systems ww --resume

# Resume behavior:
# - Skips probe keys already present in answers_*.jsonl under the run dir.
# - Also skips WW ingest_ww when **all** expected probes for that chat are already
#   complete (avoids multi-hour re-ingest of finished chats). Incomplete chats still
#   full-ingest then probe only missing keys.

# Full-scale live path (all chats, all abilities, LLM judge)
# --b1-max-chars defaults to 350000 for 100K-scale context windows
# --answer-model optional (else WW_BEAM_ANSWER_MODEL / WW_MODEL / DEFAULT_MODEL)
.venv/bin/python scripts/beam_runner.py --scale 100K --systems ww,b1,b2 \
  --llm-judge --resume --answer-model deepseek-v4-flash
```

### Data layout

Default: `WW_BEAM_DATA` or `~/.ww/beam_cache` (fallback smoke: `/tmp/BEAM-data`).

```
chats/<scale>/<id>/chat.json
chats/<scale>/<id>/probing_questions/probing_questions.json
```

Clone / place the official BEAM sparse JSON release into that tree (no pickle required).  
Do not commit the full dataset into this repo.

### Systems

| id | Path |
|----|------|
| `ww` | Turn-by-turn ingest via product `/ww/run` + unique `entity_id` per chat |
| `b1` | Context-only truncated long window |
| `b2` | Chunk + BM25 top-k into prompt |

### Results

```
results/beam/{scale}/{HEAD}_{config_hash}/
  answers_ww.jsonl
  answers_b1.jsonl
  answers_b2.jsonl
  scores_*.json
  summary.md
  meta.json
```

- **Do not hand-edit scores.**
- `meta.json` records git SHA, model, temp, seed, scale; `official_claim` stays **false** until a manager promotes the run.
- `protocol_complete` is **true** only when the run covers all chats and abilities (not dry-run), uses `--llm-judge`, and every expected probe key is present (resume-safe).
- Judge: env `WW_BEAM_JUDGE_MODEL` / `--judge-model`; default temp 0.0.
- Answer model (B1/B2): `--answer-model` or `WW_BEAM_ANSWER_MODEL` / `WW_MODEL`.
- B1 window: `--b1-max-chars` (default **350000** for full 100K chats).

### Isolation rules

- Each chat uses `entity_id = beam_{scale}_{chat_id}_{run_tag}`.
- Ingest must mirror product memory (not stuffing the full chat into one “pass” prompt as cheating).
- Session/interrupt poison from one conversation must not empty-reply another (core StateManager prepare_for_run).

### P0 product fixes (retrieval floor · fact extract · fail-fast)

These land in product code so BEAM uses real `/ww/run` (not a side channel):

1. **Probe retrieval floor** (`core/beam_remediation.py`, applied in `core/loop.py` + `server.py` when `platform=beam`, and in `probe_ww` goal wrap): every probe goal forces memory search/recall first; if atoms hit, the model must not say “no record”; empty search may abstain. Optional pre-search injects a `retrieved:` evidence block (belt + suspenders). **No stable separate HTTP search is required** — tool-forcing + pre-search via in-process memory is the path.
2. **Deterministic fact extract** (`core/memory/fact_extract.py`, default **on** via `WW_BEAM_FACT_EXTRACT=1`): on `ingest_turn`, regex/light parse writes numbers/dates/names/metric updates through v-next `remember` so same-key Updates supersede (e.g. commits 10 → 165 → current_truth is 165 only). Batch ingest headers also instruct the agent to extract/remember durable facts.
3. **Interrupt clear**: every `/ww/run` calls `prepare_for_run` before work; LLM/API hard failures surface `status=error` with a short reason rather than silent `interrupted` when possible.
4. **API collapse fail-fast** (`beam_runner.py`): ≥10 consecutive empty `llm_response` across systems aborts the run with nonzero exit and log `api_collapse_suspected` (chat-9 dead-API pattern). Empty answers without interrupt mark get `raw_extract.status=api_empty`.
5. **Diag** (`scripts/beam_diag_chat.py`): offline report of turn count, blob size, B2 BM25 snippets, keyword hit rate vs chat text; writes `results/beam/diag/chat_<id>_<ts>.md`. Optional `--url --live` only when key+balance exist.

**LLM balance = 0 means no live full 100K.** Deliver code + unit/fixture tests first; re-run official 100K only when the answer/judge key has credit. Mini harness and single-chat diag ≠ official 100K.

```bash
# Offline chat diag (cache optional; exit 2 if chat missing)
.venv/bin/python scripts/beam_diag_chat.py --chat 1 --scale 100K

# P0 unit tests (no network LLM)
.venv/bin/python -m pytest -q tests/test_beam_p0_remediation.py tests/test_memory_vnext.py --tb=short
```

## Related

- `scripts/memory_prove.py --product` — Gate 0 live plant/probe
- `scripts/beam_mini_prove.py` — mini honesty only
- `scripts/beam_diag_chat.py` — P0.1 offline/live chat diagnostic
- `core/public_reply.py` — user-facing reply extraction (never metrics dumps)
- `core/beam_remediation.py` — probe floor + API collapse guard
- `core/memory/fact_extract.py` — deterministic durable-fact extract
