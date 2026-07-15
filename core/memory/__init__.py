"""
Worldwave Memory System — Bionic Three-layer Architecture

WW's memory is a first-class module of the framework (not an external service).
Third pillar alongside main consciousness and subconscious.

Core principles:
- Memory is private - does not participate in federated learning
- Memory system is integral to WW, not separable
- Three-layer separation: Short-term Buffer (Hippocampus) -> Emotional Scoring (Amygdala) -> Long-term Consolidation (Sleep)
- Recall via pattern completion + diffuse activation, not keyword search

Modules:
atom.py          Memory atom + Entity Resolution + Fact Store
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

__all__ = [
    "MemoryAtom", "FactStore", "EntityResolver", "maybe_promote_core",
    "EncodingLayer", "EmotionMapper", "Hippocampus", "Amygdala",
    "SleepConsolidation", "DailyScheduler", "SleepDaemon", "IdleDetector",
    "RecallEngine", "Reconsolidation", "MemorySystem",
]
