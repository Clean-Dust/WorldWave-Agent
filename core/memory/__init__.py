"""
Worldwave Memory System — Bionic Architecture

WW's memory is a first-class module of the framework (not an external service).
Third pillar alongside main consciousness and subconscious.

Core principles:
- Memory is private - does not participate in federated learning
- Memory system is integral to WW, not separable
- Bounded online buffers + long-term store (no infinite-prompt promise)
- Recall via pattern completion + diffuse activation, not keyword search

Layers (v-next product slice, WW_MEMORY_VNEXT default on):
  Working Memory — single active topic (+ bound digests); entity RAM labels retained
  → Hippocampus / Topic STM — BM25 + six-weight eval; atom extract on leave
  → Atom nets (World/Experience/Observation/Opinion) + dual timestamps
  → LTM VFS (ww:// content + index; Abstract/Overview/Detail tiers)
  → Dreaming (async cold path; WW_DREAMING_ENABLED default on)

Legacy path (still available as fallback):
  Entity working_memory labels → Hippocampus atoms → sleep/promote

Subconscious is referee/gating only (BG safe gate + optional WM score
tie-break); it does not replace WM or hippocampus.

Modules:
atom.py          Memory atom + Entity Resolution + Fact Store
atom_nets.py     Four logical nets + Connect (Updates/Extends/Derives)
topic.py         Topic / Digest / WorkingTopicStore
topic_stm.py     Topic hippocampus (BM25 + promote/purge)
ltm_vfs.py       ww:// LTM content+index layers
dreaming.py      Async dream worker
vnext.py         Pipeline orchestrator
encoder.py       Encoder layer: entity extraction + emotional quantization
hippocampus.py   Short-term buffer (100 FIFO + forced sleep when full)
amygdala.py      Amygdala scoring (5-factor weighted)
sleep.py         Sleep consolidation + daily scheduler + dynamic idle detection
recall.py        Recall engine (pattern completion + diffuse activation)
reconsolidation.py  Reconsolidation (stability tracking + context integration)
code_memory.py   Immutable code memory store (exact hash, Merkle tree, call graph)

Usage:
    from core.memory.system import MemorySystem
    mem = MemorySystem()
    mem.store("Learned about FastAPI dependency injection")
    mem.recall("FastAPI injection")
    mem.sleep()  # manual consolidation trigger
"""

from .atom import MemoryAtom, FactStore, EntityResolver, maybe_promote_core
from .encoder import EncodingLayer, EmotionMapper
from .hippocampus import Hippocampus
from .amygdala import Amygdala
from .sleep import IdleDetector, SleepConsolidation, DailyScheduler, SleepDaemon
from .recall import RecallEngine
from .reconsolidation import Reconsolidation

from .system import MemorySystem
from .vnext import MemoryVNext, memory_vnext_enabled
from .topic import Topic, Digest, WorkingTopicStore
from .topic_stm import TopicHippocampus
from .atom_nets import AtomNetStore, MemoryAtomV2
from .ltm_vfs import LTMVFS, ContentTier, ImmutableLTMError
from .dreaming import DreamingWorker, dreaming_enabled

__all__ = [
    "MemoryAtom", "FactStore", "EntityResolver", "maybe_promote_core",
    "EncodingLayer", "EmotionMapper", "Hippocampus", "Amygdala",
    "SleepConsolidation", "DailyScheduler", "SleepDaemon", "IdleDetector",
    "RecallEngine", "Reconsolidation", "MemorySystem",
    "MemoryVNext", "memory_vnext_enabled",
    "Topic", "Digest", "WorkingTopicStore", "TopicHippocampus",
    "AtomNetStore", "MemoryAtomV2",
    "LTMVFS", "ContentTier", "ImmutableLTMError",
    "DreamingWorker", "dreaming_enabled",
]
