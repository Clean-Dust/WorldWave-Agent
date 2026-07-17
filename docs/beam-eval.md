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
- `meta.json` records git SHA, model, temp, seed, scale; `official_claim` stays false until a managed official run.
- Judge: env `WW_BEAM_JUDGE_MODEL` / `--judge-model`; default temp 0.0.

### Isolation rules

- Each chat uses `entity_id = beam_{scale}_{chat_id}_{run_tag}`.
- Ingest must mirror product memory (not stuffing the full chat into one “pass” prompt as cheating).
- Session/interrupt poison from one conversation must not empty-reply another (core StateManager prepare_for_run).

## Related

- `scripts/memory_prove.py --product` — Gate 0 live plant/probe
- `scripts/beam_mini_prove.py` — mini honesty only
- `core/public_reply.py` — user-facing reply extraction (never metrics dumps)
