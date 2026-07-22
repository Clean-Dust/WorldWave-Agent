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
5. **Diag** (`scripts/beam_diag_chat.py`): offline report of turn count, blob size, B2 BM25 snippets, keyword hit rate vs chat text; writes `results/beam/diag/chat_<id>_<ts>.md`. Optional `--url --live` only when key+balance exist. Live path notes `state_metrics.beam_retrieval` (atom search hit counts) when the server attaches them.
6. **raw_extract hit counts** (P0 polish): WW probe rows record `retrieval_hits`, `retrieval_empty`, and `status` (`completed` | `interrupted` | `api_empty` | `error`) from `/ww/run` → `state_metrics.beam_retrieval` (set in `core/loop.py` when pre-search runs).

### P0–P2 map (failure form → fix)

| Failure form | Priority | Code path |
|--------------|----------|-----------|
| Has facts but says “no record” | P0.3 / P0.5 / P1.3 | `beam_remediation` retrieval floor + abstention policy |
| Wrong number / date | P0.4 / P1.1 / P1.2 | `fact_extract` + Updates; `timeline.days_between`; quantity exact-number instruct |
| Should abstain / invents bio | P1.3 | empty-retrieval abstain policy |
| Instruction / format miss | P1.4 | fenced code / bullets / JSON / numbered steps in probe wrap |
| Preference ignored | P1.5 | preference kind extract + honor-preferences instruct |
| Multi-session miss | P1.6 | multi-hop “combine all snippets” + retrieval floor |
| interrupted / empty reply | P0.2 | `prepare_for_run` + status=error surface + fail-fast |
| Contradiction not acknowledged | P2.1 | conflict-tagged atoms + `format_contradiction_evidence` |
| Event order wrong | P2.2 | `timeline` + `format_event_order` |
| Summary invents / tool dump | P2.3 | evidence-only summarize rule in probe wrap |

### P1 mechanisms (correctness · time · quantity · IF)

| ID | Delivery |
|----|----------|
| **P1.1 Timeline** | `core/memory/timeline.py` — dated events on ingest; `list_events`, `days_between`; temporal probes get structured-date instruct |
| **P1.2 Quantity** | Metric extract (commits, latency ms, counts); `answer_from_quantity_evidence`; probe wrap demands exact number |
| **P1.3 Abstention** | Hits > 0 forbids “no record”; empty → short abstain, no invented biography |
| **P1.4 IF** | Format obedience (fences, bullets, numbered steps, JSON) + code-fence detector |
| **P1.5 Preference** | Preference facts tagged on extract; wrap honors stated preferences |
| **P1.6 Multi-session** | Retrieval floor + multi-hop “combine all retrieved snippets” |

### P2 mechanisms (hard items · fixture-proven)

| ID | Delivery |
|----|----------|
| **P2.1 Contradiction** | Dual values / “but earlier|actually” → `conflict=true` atoms; `format_contradiction_evidence` must acknowledge both sides |
| **P2.2 Event ordering** | Timeline ordered list + `format_event_order` numbered sequence |
| **P2.3 Summarization** | Summarize only from retrieved evidence; no tool dump; no inventing |
| **P2.4 Docs** | This file + `docs/beam-memory-eval-notes.md` (ops / re-run / gates) |

### official_claim discipline

- **`official_claim` defaults to `false`** in every `meta.json`.
- Do **not** set `official_claim=true` from code, mini prove, single-chat diag, or an incomplete 100K resume.
- Only a manager written promotion after full protocol (20 chats × abilities, WW+B1+B2, frozen judge, S1–S5/S7) may flip it.
- Mini harness / diag / unit tests **never** equal official 100K.

### Fail-fast

- ≥10 consecutive empty `llm_response` → abort with `api_collapse_suspected` (nonzero exit).
- Empty product answers without interrupt → `raw_extract.status=api_empty`.
- Interrupted rate target on probe path: **≤1%** (or eliminated via prepare_for_run + error surface).

**LLM balance = 0 means no live full 100K.** Deliver code + unit/fixture tests first; re-run official 100K only when the answer/judge key has credit. Mini harness and single-chat diag ≠ official 100K.

```bash
# Offline chat diag (cache optional; exit 2 if chat missing)
.venv/bin/python scripts/beam_diag_chat.py --chat 1 --scale 100K

# P0 + P1/P2 unit tests (no network LLM)
.venv/bin/python -m pytest -q \
  tests/test_beam_p0_remediation.py \
  tests/test_beam_p1_p2.py \
  tests/test_memory_vnext.py --tb=short

# Single-chat diag live (optional; needs balance)
.venv/bin/python scripts/beam_diag_chat.py --chat 1 --scale 100K \
  --url http://127.0.0.1:9300 --live

# Official 100K re-run (Banana; single server :9300; frozen judge/answer)
.venv/bin/python scripts/beam_runner.py --scale 100K --systems ww,b1,b2 \
  --llm-judge --resume --run-tag rem100k_<git>
```

## Related

- `scripts/memory_prove.py --product` — Gate 0 live plant/probe
- `scripts/beam_mini_prove.py` — mini honesty only
- `scripts/beam_diag_chat.py` — P0.1 offline/live chat diagnostic
- `docs/beam-memory-eval-notes.md` — ops notes (re-run, gates, skill mirror)
- `core/public_reply.py` — user-facing reply extraction (never metrics dumps)
- `core/beam_remediation.py` — probe floor, P1/P2 policies, API collapse guard
- `core/memory/fact_extract.py` — deterministic durable-fact extract
- `core/memory/timeline.py` — timeline store for temporal / ordering
