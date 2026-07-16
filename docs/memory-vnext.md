# Memory — single system (v-next spine)

**Product law:** one mental model, one primary store for agent memory.
Legacy flat-key Entity WM is **not** a parallel product path. Anything
legacy still won (labels, core protect, recency/access, tools, entity
scoping) lives **inside** this system.

Design contract concepts: `core/memory/{topic,topic_stm,atom_nets,ltm_vfs,dreaming,labeled_wm,vnext}.py`.

## Flow

```
Labeled facts (kind/core/recency)  +  Working Memory (one active topic + digests)
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
Dreaming / sleep (async cold path; MemorySystem.sleep API — not a second product)
```

Primary data dir: `~/.ww/memory/vnext/` (or `MemorySystem` `data_dir/vnext`).

| Subpath | Role |
|---------|------|
| `facts/` | Labeled online facts (kind/core/access/recency) — SoT for `remember` |
| `wm/` | Active topic body + digests |
| `topic_stm/` | Parked topics |
| `atom_nets/` | Four nets + links |
| LTM tree | `ww://` content + index |

## Absorbed from legacy

| Feature | Where it lives now |
|---------|-------------------|
| Explicit **kind** labels (constraint/commitment/outcome/rationale) | `LabeledFactStore` + `remember(kind=…)`; product name 标签 |
| Eviction order constraint > commitment > outcome > rationale | Same scoring pure functions; applied on labeled store |
| **is_core** / persona hard protect | `LabeledFactStore` core set + topic `is_core` |
| Recency × access scoring (B6) | `wm_eviction_score` / `LabeledFactStore` |
| Entity / Same Timeline coupling | `MemoryVNext.set_entity` + facts keyed by entity_id |
| `remember` / `forget` / `recall_mine` tools | Write/read **v-next only** for product path |
| Sleep consolidation value | Behind `MemorySystem.sleep()`; also queues dreaming |

**No keyword guessing for kind** — only explicit API.

## Must not ship (dual product)

1. **No dual inject** — context builder injects one memory picture via
   `MemoryVNext.inject_for_turn()` / `MemorySystem.memory_context_block()`.
   Entity continuity inject skips flat WM dump when v-next is active
   (`include_working_memory=False`).
2. **No long-term “legacy-only mode”** — `WW_MEMORY_VNEXT=0` is an
   **emergency kill switch** only (one release); default ON. Prefer
   always-on; if init fails, do not re-enable parallel flat-WM as product brain.
3. **EntityState.working_memory** — compatibility dual-write shim from
   `MemoryTools` until **2026-08-31** (`_ENTITY_WM_DUAL_WRITE_REMOVE_BY` in
   `core/memory/tools.py`). Read path for product inject prefers v-next.
   End state: facts only under `vnext/facts/`.

## Modules

| Module | Role |
|--------|------|
| `core/memory/labeled_wm.py` | Kind-labeled fact buffer (capacity, core, recency) |
| `core/memory/topic.py` | Topic, Digest, WorkingTopicStore |
| `core/memory/topic_stm.py` | BM25 STM, promote/purge, atom extract on leave |
| `core/memory/atom_nets.py` | Four nets + Connect + dual timestamps |
| `core/memory/ltm_vfs.py` | `ww://` tree, categories, tiers |
| `core/memory/dreaming.py` | Async worker (queue; cheap no-op if empty) |
| `core/memory/vnext.py` | Orchestrator + prompt isolation blocks |
| `core/memory/system.py` | Single API; sleep/dream cold path |
| `core/memory/tools.py` | remember / forget / recall_mine → single system |

## Write tracks

1. **Hot tools** — `remember` / `forget` / `reflect` (kind explicit; no keyword guessing; no dual LLM)
2. **Passive lossless** — `ingest_turn` → Experience atom + topic body
3. **Cold** — Dreaming crawls atoms; `MemorySystem.sleep()` consolidates hippocampus + queues dream

Forbidden: every write does two full LLM calls.

## Prompt isolation

- System prompt: persona + hard rules only
- Retrieved memory / peer / labeled facts / working topic: separate context blocks via
  `MemoryVNext.build_context_blocks()` / `inject_for_turn()` / `MemorySystem.memory_context_block()`

## LTM layout (`ww://`, alias `viking://`)

```
ww://
├── resources/
├── user/memories/
│   ├── profile.md
│   ├── preferences/
│   ├── entities/
│   ├── events/
│   ├── trajectories/
│   ├── experiences/
│   ├── tools/
│   └── skills/
└── agent/
    ├── skills/
    └── memories/
        └── dreaming/
```

Content tiers: **Abstract** (~100 tok) / **Overview** (~2k) / **Detail**.

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
`composite ≥ 0.8` **AND** `recall_count ≥ 3`.

## Env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `WW_MEMORY_VNEXT` | **on** | Single system; `0` = emergency kill switch (deprecated as product mode) |
| `WW_DREAMING_ENABLED` | **on** | Async dreaming; cheap no-op if empty |
| `WW_WM_TOKEN_BUDGET` | `min(32000, 0.25 * 128k)` | Active topic token budget |
| `WW_WM_BODY_KEEP_TURNS` | `8` | Body turns kept after digest compress |
| `WW_WM_BODY_KEEP_TOKENS` | `2000` | Tighter bound wins with keep-turns |
| `WW_TOPIC_HIPPO_CAP` | `200` | Topic STM capacity |
| `WW_WORKING_MEMORY_CAPACITY` | `32` | Labeled fact buffer capacity |
| `WW_WM_RECENCY_*` | on / 3600s / 0.4 | Recency decay for labeled facts |
| `WW_WM_WEIGHT_*` | 4/3/2/1 | kind weights constraint…rationale |
| `WW_MEMORY_RRF` | **off** | Optional RRF fusion |
| `WW_MEMORY_CROSS_ENCODER` | **off** | Optional rerank |
| `WW_MEMORY_HRR` | **off** | Fail-loud if on without backend |

## Migration notes

1. Facts written with `remember` land in `vnext/facts/{entity}.json` and atom nets.
2. EntityState SQLite may still dual-write until shim removal date — do not
   treat it as the product inject source.
3. `WW_MEMORY_VNEXT=0` does not restore a supported dual-brain product; use
   only if v-next init is broken in an emergency.
4. Sleep remains callable as `MemorySystem.sleep()`; it is cold-path plumbing,
   not a second memory product.

## Tests / prove

```bash
python -m pytest tests/test_working_memory.py tests/test_memory.py \
  tests/test_memory_*.py tests/test_memory_vnext.py -q --tb=short

python scripts/memory_prove.py --mechanism
```

Mechanism: B1–B7 (kind/core/recency on single-system scoring) + B-topic/* +
product/narrative. B4–B7 assert **LabeledFactStore / MemoryVNext** (and shared
pure scoring helpers); EntityStateManager remains for identity continuity tests.

## Out of scope

- Banana deploy, enterprise multi-tenant, Neo4j requirement
- Auto-rewriting system prompt via background metaprompt (default off)
- RRF / cross-encoder / HRR as required path (optional, default off)
