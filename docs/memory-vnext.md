# Memory v-next — architecture

Topic-centric pipeline for WW. Design contract: `ww-memory-design-v-next` §1–§7.

## Flow

```
Working Memory (exactly one active topic + bound digests)
        │  topic switch / park
        ▼
Topic Hippocampus (STM)  — BM25 + six-weight composite
        │  leave: promote OR purge  →  MUST extract atoms first
        ▼
Atom nets (World / Experience / Observation / Opinion)
        │  dual timestamps; Updates/Extends/Derives; no hard delete
        ▼
LTM VFS (ww:// content layer + index layer)
        │  Abstract → Overview → Detail progressive inject
        ▼
Dreaming (async cold path; does not block chat)
```

Legacy flat-key entity working memory (labels: constraint/commitment/outcome/rationale)
remains for `remember(kind=…)` tools and Same Timeline identity. v-next dual-writes
hot remember into atom nets when `MemorySystem.vnext` is active.

## Modules

| Module | Role |
|--------|------|
| `core/memory/topic.py` | Topic, Digest, WorkingTopicStore |
| `core/memory/topic_stm.py` | BM25 STM, promote/purge, atom extract on leave |
| `core/memory/atom_nets.py` | Four nets + Connect + dual timestamps |
| `core/memory/ltm_vfs.py` | `ww://` tree, eight user categories + dreaming, tiers |
| `core/memory/dreaming.py` | Async worker (queue; cheap no-op if empty) |
| `core/memory/vnext.py` | Orchestrator + prompt isolation blocks |
| `core/memory/system.py` | Wires v-next when enabled (default on) |

## Write tracks

1. **Hot tools** — `remember` / `forget` / `reflect` (kind explicit; no keyword guessing)
2. **Passive lossless** — `ingest_turn` → Experience atom + topic body (no dual LLM)
3. **Cold** — Dreaming crawls atoms, fills gaps, peer cards → `agent/memories/dreaming/`

Forbidden: every write does two full LLM calls.

## Prompt isolation

- System prompt: persona + hard rules only
- Retrieved memory / peer / working topic: separate context blocks via
  `MemoryVNext.build_context_blocks()` / `inject_for_turn()` / `MemorySystem.memory_context_block()`

## LTM layout (`ww://`, alias `viking://`)

```
ww://
├── resources/
├── user/memories/
│   ├── profile.md          # merge single file
│   ├── preferences/        # append
│   ├── entities/           # append
│   ├── events/             # immutable
│   ├── trajectories/       # immutable
│   ├── experiences/        # merge-update
│   ├── tools/              # merge-update
│   └── skills/             # merge-update
└── agent/
    ├── skills/
    └── memories/
        └── dreaming/       # merge-update (9th category)
```

Content tiers (not bare storage “L0”): **Abstract** (~100 tok) / **Overview** (~2k) / **Detail**.

## Hippocampus scoring (defaults)

| Signal | Weight |
|--------|--------|
| Relevance | 0.30 |
| Frequency | 0.24 |
| Query diversity | 0.15 |
| Recency (14d half-life) | 0.15 |
| Consolidation | 0.10 |
| Conceptual richness | 0.06 |

Promote: hard-filter chatter / multi-fact blobs / unresolved pronouns, then
`composite ≥ 0.8` **AND** `recall_count ≥ 3`. Light/REM boost stubs default 0.

## Env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `WW_MEMORY_VNEXT` | **on** | Topic pipeline; set `0` for legacy-only |
| `WW_DREAMING_ENABLED` | **on** | Async dreaming; cheap no-op if empty |
| `WW_WM_TOKEN_BUDGET` | `min(32000, 0.25 * 128k)` | Active topic token budget |
| `WW_WM_BODY_KEEP_TURNS` | `8` | Body turns kept after digest compress |
| `WW_WM_BODY_KEEP_TOKENS` | `2000` | Tighter bound wins with keep-turns |
| `WW_TOPIC_HIPPO_CAP` | `200` | Topic STM capacity (else `WW_HIPPOCAMPUS_CAP`) |
| `WW_HIPPO_PROMOTE_MIN_SCORE` | `0.8` | Promote composite threshold |
| `WW_HIPPO_PROMOTE_MIN_RECALL` | `3` | Promote min recall_count |
| `WW_MEMORY_RRF` | **off** | Optional RRF fusion |
| `WW_MEMORY_CROSS_ENCODER` | **off** | Optional rerank (not required for green) |
| `WW_MEMORY_HRR` | **off** | If on without full backend → **fail-loud** |

Entity WM capacity / labels (unchanged): `WW_WORKING_MEMORY_CAPACITY`,
`WW_WM_RECENCY_*`, `WW_WM_WEIGHT_*`.

## Tests / prove

```bash
python -m pytest tests/test_working_memory.py tests/test_memory.py \
  tests/test_memory_*.py tests/test_memory_vnext.py -q --tb=short

python scripts/memory_prove.py --mechanism
```

Mechanism adds: `B-topic`, `B-summary`, `B-atom`, `B-hippo-promote`, `B-ltm-tier`, `B-dream`
(no live LLM; fixture/time OK). Product mode still forbids identity plant / sole
`POST /ww/memory` store path.

## Out of scope (this slice)

- Banana deploy, enterprise multi-tenant, Neo4j requirement
- Auto-rewriting system prompt via background metaprompt (default off)
- RRF / cross-encoder / HRR as required path (optional, default off)
