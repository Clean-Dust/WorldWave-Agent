# Memory — single system (v-next spine)

**Product law:** one mental model, one primary store for agent memory.
Legacy flat-key Entity WM is **not** a parallel product path. Anything
legacy still won (labels, core protect, recency/access, tools, entity
scoping) lives **inside** this system.

Design contract: `core/memory/{topic,topic_stm,atom_nets,ltm_vfs,dreaming,labeled_wm,vnext,tools}.py`.

## Completeness checklist (single system)

| # | Gap | Status |
|---|-----|--------|
| 1 | Dual-write shim off by default; product SoT = MemoryVNext / LabeledFactStore / AtomNet / LTM | **Closed** — `WW_ENTITY_WM_DUAL_WRITE=1` emergency only |
| 2 | Auto topic split in live loop (`ingest_turn` + loop + `switch_topic` tool) | **Closed** — markers / gap / lexical heuristics |
| 3 | Stronger rule atom extract on leave (no dual LLM per write); optional LLM extract off | **Closed** — `WW_ATOM_LLM_EXTRACT` default off |
| 4 | Deeper async dreaming: supersede by dual-ts, peer cards / summary under dreaming/ | **Closed** — `WW_DREAMING_ENABLED` kill switch |
| 5 | Optional RRF (STM + atoms + labeled facts); default off | **Closed** — `WW_MEMORY_RRF=1` |
| 6 | Progressive inject: Abstract first; Overview only if budget; core always | **Closed** |
| 7 | Prove harness: mechanism / product / narrative; `--telegram` / `--restart` | **Closed** (env-gated) |
| 8 | Docs + env table + deprecations | **This file** |
| 9 | Repo systemd user unit + optional deploy enable | **`deploy/ww.user.service`** |
| 10 | Gate 0 product honesty: never promote memory dumps as chat; BEAM-mini | **`core/public_reply.py` + `scripts/beam_mini_prove.py`** |

Out of scope: auto-rewrite system prompt; Neo4j / commercial graph; printing secrets; Banana deploy.

### Gate 0 / BEAM-mini (honesty only — not official 100K)

User-facing replies go only through `core/public_reply.extract_user_response`
(`reflex_text` / `respond` / `reply` / `final_answer`). Memory tools never
become the chat reply. Dump-like `key: value` blocks and spiral JSON are rejected.

Live mini prove (server required):

```bash
.venv/bin/python scripts/beam_mini_prove.py
# or: WW_PROVE_URL=http://127.0.0.1:8765 .venv/bin/python scripts/beam_mini_prove.py
```

## Flow

```
Labeled facts (kind/core/recency)  +  Working Memory (one active topic + digests)
        │  topic switch / park (auto heuristic or switch_topic tool)
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
| LTM tree | `ww://` content + index (`agent/memories/dreaming/` for dream outputs) |

## Absorbed from legacy

| Feature | Where it lives now |
|---------|-------------------|
| Explicit **kind** labels (constraint/commitment/outcome/rationale) | `LabeledFactStore` + `remember(kind=…)`; product name 标签 |
| Eviction order constraint > commitment > outcome > rationale | Same scoring pure functions; applied on labeled store |
| **is_core** / persona hard protect | `LabeledFactStore` core set + topic `is_core` |
| Recency × access scoring (B6) | `wm_eviction_score` / `LabeledFactStore` |
| Entity / Same Timeline coupling | `MemoryVNext.set_entity` + facts keyed by entity_id |
| `remember` / `forget` / `recall_mine` / `switch_topic` tools | Write/read **v-next only** for product path |
| Sleep consolidation value | Behind `MemorySystem.sleep()`; also queues dreaming |

**No keyword guessing for kind** — only explicit API.

## Must not ship (dual product)

1. **No dual inject** — context builder injects one memory picture via
   `MemoryVNext.inject_for_turn()` / `MemorySystem.memory_context_block()`.
   Entity continuity inject skips flat WM dump when v-next is active
   (`include_working_memory=False`).
2. **No long-term “legacy-only mode”** — `WW_MEMORY_VNEXT=0` is an
   **emergency kill switch** only; default ON.
3. **EntityState.working_memory** — **not product SoT**. Dual-write is
   **off** by default. Emergency only: `WW_ENTITY_WM_DUAL_WRITE=1`.
   EntityState remains for identity continuity and isolated unit fixtures.

## Modules

| Module | Role |
|--------|------|
| `core/memory/labeled_wm.py` | Kind-labeled fact buffer (capacity, core, recency) |
| `core/memory/topic.py` | Topic, Digest, WorkingTopicStore, split heuristics |
| `core/memory/topic_stm.py` | BM25 STM, promote/purge, atom extract on leave |
| `core/memory/atom_nets.py` | Four nets + Connect + dual timestamps + rule extract |
| `core/memory/ltm_vfs.py` | `ww://` tree, categories, tiers |
| `core/memory/dreaming.py` | Async worker (queue; supersede + peer cards) |
| `core/memory/vnext.py` | Orchestrator + progressive inject + optional RRF |
| `core/memory/system.py` | Single API; sleep/dream cold path |
| `core/memory/tools.py` | remember / forget / recall_mine / switch_topic |

## Write tracks

1. **Hot tools** — `remember` / `forget` / `reflect` / `switch_topic` (kind explicit; no dual LLM)
2. **Passive lossless** — loop `ingest_turn` → Experience atom + topic body; auto topic split
3. **Cold** — Dreaming crawls atoms, supersedes conflicts by dual-ts, writes peer cards; `MemorySystem.sleep()` consolidates + queues dream

Forbidden: every write does two full LLM calls.

## Topic split heuristics

On user `ingest_turn` (and live loop), switch when any of:

1. Explicit markers (`by the way`, `unrelated:`, `换个话题`, …)
2. Long gap (`WW_TOPIC_GAP_SECONDS`, default 3600s) **and** subject change
3. Low lexical overlap (< 0.15) on non-trivial turns

Agent tool: `switch_topic(title=…)` always parks current topic fully to STM.

## Atom extract on leave

Before promote/purge from hippocampus:

- Rule extract: sentence split, multi-fact blob split, entity-ish tokens,
  drop chatter / pronoun-only
- Optional: `WW_ATOM_LLM_EXTRACT=1` + cheap model → one background enrich
  (default **off**; never dual LLM on every write)

## Progressive inject

`inject_for_turn(query, max_chars=…)`:

1. Core / persona always (protected)
2. Labeled facts + working topic under remaining budget
3. LTM: **Abstract** first; expand **Overview** only if budget allows
4. Soft truncate retrieval; never drop core first

## Prompt isolation

- System prompt: persona + hard rules only
- Retrieved memory / peer / labeled facts / working topic: separate context
  blocks via `build_context_blocks()` / `inject_for_turn()` / `memory_context_block()`

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
| `WW_ENTITY_WM_DUAL_WRITE` | **off** | Emergency dual-write EntityState WM; product path ignores when off |
| `WW_DREAMING_ENABLED` | **on** | Async dreaming; cheap no-op if empty |
| `WW_ATOM_LLM_EXTRACT` | **off** | Optional background LLM atom extract on leave |
| `WW_ATOM_LLM_MODEL` | (none) | Cheap model id when LLM extract enabled |
| `WW_WM_TOKEN_BUDGET` | `min(32000, 0.25 * 128k)` | Active topic token budget |
| `WW_WM_BODY_KEEP_TURNS` | `8` | Body turns kept after digest compress |
| `WW_WM_BODY_KEEP_TOKENS` | `2000` | Tighter bound wins with keep-turns |
| `WW_TOPIC_HIPPO_CAP` | `200` | Topic STM capacity |
| `WW_TOPIC_GAP_SECONDS` | `3600` | Gap threshold for topic auto-split |
| `WW_WORKING_MEMORY_CAPACITY` | `32` | Labeled fact buffer capacity |
| `WW_WM_RECENCY_*` | on / 3600s / 0.4 | Recency decay for labeled facts |
| `WW_WM_WEIGHT_*` | 4/3/2/1 | kind weights constraint…rationale |
| `WW_MEMORY_RRF` | **off** | Optional RRF fusion (STM + atoms + labeled facts) |
| `WW_MEMORY_CROSS_ENCODER` | **off** | Fail-loud if on without backend |
| `WW_MEMORY_HRR` | **off** | Fail-loud if on without backend |
| `WW_OWNER_TELEGRAM_ID` | (none) | Required for prove `--telegram` identity path |
| `WW_PROVE_ALLOW_RESTART` | (none) | Required for prove `--restart` (restarts `ww.service`) |

## Deprecations

| Item | State |
|------|--------|
| EntityState dual-write as product path | **Removed** (default off; emergency env only) |
| Dual inject (flat WM + v-next in system) | **Removed** |
| `WW_MEMORY_VNEXT=0` as supported dual-brain mode | **Deprecated** — emergency only |
| Keyword guessing for WM `kind` | **Never supported** |
| Dual LLM on every remember / turn write | **Forbidden** |
| RRF / cross-encoder / HRR as required path | **Not required** — optional, default off |

## Migration notes

1. Facts written with `remember` land in `vnext/facts/{entity}.json` and atom nets.
2. EntityState SQLite is **not** the product inject source. Dual-write only if
   `WW_ENTITY_WM_DUAL_WRITE=1`.
3. `WW_MEMORY_VNEXT=0` does not restore a supported dual-brain product.
4. Sleep remains callable as `MemorySystem.sleep()`; cold-path plumbing only.

## systemd user unit

Repo template: `deploy/ww.user.service` (placeholders `@WW_HOME@`).

```bash
# Manual install
WW_HOME="${WW_HOME:-$HOME/worldwave}"
mkdir -p ~/.config/systemd/user
sed "s|@WW_HOME@|$WW_HOME|g" "$WW_HOME/deploy/ww.user.service" \
  > ~/.config/systemd/user/ww.service
systemctl --user daemon-reload
systemctl --user enable --now ww.service
# optional: survive logout
loginctl enable-linger "$USER"
```

`deploy.sh` may install/enable the unit when present; partner install path
without systemd remains unchanged.

## Tests / prove

```bash
python -m pytest tests/test_memory_vnext.py tests/test_working_memory.py \
  tests/test_basal_ganglia_memory_tools.py tests/test_memory.py -q --tb=short

python scripts/memory_prove.py --mechanism
# optional:
#   WW_OWNER_TELEGRAM_ID=… python scripts/memory_prove.py --telegram
#   WW_PROVE_ALLOW_RESTART=1 python scripts/memory_prove.py --restart
```

Mechanism: B1–B7 (kind/core/recency on single-system scoring) + B-topic/* +
product/narrative. B4–B7 assert **LabeledFactStore / MemoryVNext**; EntityStateManager
remains for identity continuity tests only.

## Out of scope

- Banana deploy, enterprise multi-tenant, Neo4j requirement
- Auto-rewriting system prompt via background metaprompt (default off)
- RRF / cross-encoder / HRR as required path (optional, default off)
