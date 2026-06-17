"""Progressive Enhancement Engine for Code Search & Analysis.

Detects available native accelerators at runtime and automatically
selects the fastest available implementation.

Architecture:
  Layer 1 (Fallback): Pure Python stdlib — always available
  Layer 2 (Optional): Tree-sitter — C-based incremental parser, 5-50x faster AST
  Layer 3 (Optional): FAISS/hnswlib — GPU/CPU vector search for semantic code retrieval

Usage:
  from coding.progressive import ProgressiveEnhancement
  pe = ProgressiveEnhancement()
  engine = pe.get_search_engine()    # Returns best available
  engine.search(...)                  # Same API regardless of backend

Config:
  WW_DISABLE_NATIVE=1     → Force pure Python (disable all accelerators)
  WW_FORCE_TREESITTER=1   → Require tree-sitter, fail if unavailable
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("ww.coding.progressive")


@dataclass
class Capability:
    """A detected hardware/software capability."""
    name: str
    available: bool
    version: str = ""
    details: str = ""
    layer: int = 1  # 1=fallback, 2=optional, 3=accel


@dataclass
class CapabilityReport:
    """Complete capability inventory."""
    capabilities: List[Capability] = field(default_factory=list)
    timestamp: float = 0.0
    environment: str = ""

    def best_layer(self) -> int:
        """Highest layer available."""
        available = [c.layer for c in self.capabilities if c.available]
        return max(available) if available else 1

    def to_dict(self) -> Dict:
        return {
            "capabilities": [
                {"name": c.name, "available": c.available,
                 "version": c.version, "layer": c.layer}
                for c in self.capabilities
            ],
            "best_layer": self.best_layer(),
        }


class ProgressiveEnhancement:
    """Detection and selection of progressive enhancement layers.

    Each layer builds on the previous one:
    - Layer 1 (Pure Python): Always available, zero deps
    - Layer 2 (Tree-sitter): C-based, 5-50x faster, requires `pip install tree-sitter tree-sitter-python`
    - Layer 3 (Vector): FAISS/hnswlib, GPU acceleration, requires `pip install faiss-cpu`
    """

    def __init__(self):
        self._disabled = os.environ.get("WW_DISABLE_NATIVE", "") == "1"
        self._force_treesitter = os.environ.get("WW_FORCE_TREESITTER", "") == "1"

        self._capabilities: List[Capability] = []
        self._probe()

    # ── Probing ─────────────────────────────────────────────────

    def _probe(self):
        """Detect all available capabilities."""
        self._capabilities = []

        # Layer 1: Pure Python (always)
        self._capabilities.append(Capability(
            name="pure_python",
            available=True,
            version=f"Python {sys.version_info.major}.{sys.version_info.minor}",
            details="stdlib ast module",
            layer=1,
        ))

        # Layer 2: Tree-sitter
        ts_available, ts_version = self._probe_treesitter()
        if not self._disabled:
            self._capabilities.append(Capability(
                name="tree_sitter",
                available=ts_available,
                version=ts_version,
                details="C-based incremental parser" if ts_available else "not installed",
                layer=2,
            ))

        # Layer 3: Vector search
        vec_available, vec_backend = self._probe_vector_search()
        if not self._disabled:
            self._capabilities.append(Capability(
                name="vector_search",
                available=vec_available,
                version=vec_backend,
                details=f"Vector index via {vec_backend}" if vec_available else "not installed",
                layer=3,
            ))

        # Layer 3b: Dense vector (CooccurrenceEmbedding — always available, pure Python)
        self._capabilities.append(Capability(
            name="dense_vector_pure",
            available=True,
            version="cooccurrence",
            details="32-dim co-occurrence embedding (pure Python)",
            layer=3,
        ))

    @staticmethod
    def _probe_treesitter() -> tuple:
        """Check if tree-sitter and Python grammar are available."""
        try:
            import tree_sitter
            version = getattr(tree_sitter, "__version__", "unknown")

            # Verify Python grammar bindings
            import tree_sitter_python
            return True, f"tree-sitter {version}"
        except ImportError:
            return False, ""

    @staticmethod
    def _probe_vector_search() -> tuple:
        """Check if FAISS or hnswlib is available."""
        try:
            import faiss
            version = getattr(faiss, "__version__", "unknown")
            # Check GPU
            gpu_available = hasattr(faiss, "get_num_gpus") and faiss.get_num_gpus() > 0
            suffix = "-gpu" if gpu_available else "-cpu"
            return True, f"faiss {version}{suffix}"
        except ImportError:
            pass

        try:
            import hnswlib
            version = getattr(hnswlib, "__version__", "unknown")
            return True, f"hnswlib {version}"
        except ImportError:
            pass

        return False, ""

    # ── Public API ──────────────────────────────────────────────

    def has_treesitter(self) -> bool:
        return self.get_capability("tree_sitter") is not None

    def has_vector_search(self) -> bool:
        return self.get_capability("vector_search") is not None

    def get_capability(self, name: str) -> Optional[Capability]:
        for c in self._capabilities:
            if c.name == name and c.available:
                return c
        return None

    def get_best_code_search_engine(self):
        """Get the best available code search engine.

        Returns:
            An object with .search() compatible with ASTSearchEngine API.
        """
        if self.has_treesitter():
            try:
                from coding.treesitter_engine import TreeSitterEngine
                log.info("Using Tree-sitter (C-accelerated) for code search")
                return TreeSitterEngine()
            except ImportError:
                pass

        from coding.code_search import ASTSearchEngine
        log.info("Using pure Python ast for code search (fallback)")
        return ASTSearchEngine()

    def get_best_vector_index(self, dimension: int = 128):
        """Get the best available vector index.

        Returns:
            An index object with .add(vectors, ids) and .search(query, k) methods.
            Falls back to pure Python linear scan if nothing else available.
        """
        if self.has_vector_search():
            try:
                return FAISSIndex(dimension=dimension)
            except Exception:
                pass

        return PurePythonIndex(dimension=dimension)

    def report(self) -> CapabilityReport:
        import time
        return CapabilityReport(
            capabilities=self._capabilities,
            timestamp=time.time(),
            environment="WSL" if "microsoft" in os.uname().release.lower() else "native",
        )


# ── Vector Index Wrappers ────────────────────────────────────────

class PurePythonIndex:
    """Fallback: linear scan with cosine similarity."""

    def __init__(self, dimension: int = 128):
        self._vectors: List[List[float]] = []
        self._ids: List[str] = []
        self._dim = dimension

    def add(self, vectors: List[List[float]], ids: List[str]):
        self._vectors.extend(vectors)
        self._ids.extend(ids)

    def search(self, query: List[float], k: int = 10) -> List[Dict]:
        """Linear scan with cosine similarity."""
        import math

        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return dot / max(1e-10, na * nb)

        scored = []
        for i, vec in enumerate(self._vectors):
            sim = cosine(query, vec)
            scored.append((sim, i))

        scored.sort(reverse=True)
        return [
            {"id": self._ids[i], "score": score}
            for score, i in scored[:k]
        ]

    def __len__(self):
        return len(self._vectors)


class FAISSIndex:
    """FAISS-accelerated vector index."""

    def __init__(self, dimension: int = 128):
        import faiss
        self._index = faiss.IndexFlatIP(dimension)  # Inner product = cosine on normalized vecs
        self._ids: List[str] = []
        self._dim = dimension

    def add(self, vectors: List[List[float]], ids: List[str]):
        import numpy as np
        arr = np.array(vectors, dtype=np.float32)
        # Normalize for cosine similarity
        import faiss
        faiss.normalize_L2(arr)
        self._index.add(arr)
        self._ids.extend(ids)

    def search(self, query: List[float], k: int = 10) -> List[Dict]:
        import numpy as np
        import faiss
        q = np.array([query], dtype=np.float32)
        faiss.normalize_L2(q)
        scores, indices = self._index.search(q, k)
        return [
            {"id": self._ids[idx], "score": float(scores[0][i])}
            for i, idx in enumerate(indices[0])
            if idx >= 0 and idx < len(self._ids)
        ]

    def __len__(self):
        return self._index.ntotal
