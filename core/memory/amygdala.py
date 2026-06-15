"""
ww/core/memory/amygdala.py — amygdala scoring system

Amygdala is responsible for affect annotation and priority scoring of memory atoms.

Five-factor weighted model:
1. emotion intensity (Emotion Intensity) — strength of memory's emotional coloring
2. link density (Link Density) — link count with other memories
3. recency (Recency) — newer memories have higher weight
4. recall frequency (Recall Frequency) — number of recalls
5. link intensity (Link Strength) — weighted average intensity of links
"""

from __future__ import annotations
import json
import logging
import math
import os
import time
from typing import Dict, List, Optional, Set

from .atom import MemoryAtom
from .hippocampus import Hippocampus

logger = logging.getLogger("ww.memory.amygdala")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
MEMORY_DIR = os.path.join(_WW_CFG, "memory")


class Amygdala:
    """
    amygdala: affect scoring + priority sort.

    each memory atom ultimately produces a salience score [0, 1],
    for deciding which memories should be prioritized for retention, recall, or consolidation.

    Five-factor default weights (adjustable):
      - emotion_weight:    0.25  — emotionintensity
      - link_weight:       0.20  — link density
      - recency_weight:    0.20  — recency
      - recall_weight:     0.15  — recall frequency
      - strength_weight:   0.20  — linkintensity
    """

    def __init__(
        self,
        emotion_weight: float = 0.25,
        link_weight: float = 0.20,
        recency_weight: float = 0.20,
        recall_weight: float = 0.15,
        strength_weight: float = 0.20,
        decay_half_life: float = 86400 * 7,
        data_dir: str = "",
    ):
        self.emotion_weight = emotion_weight
        self.link_weight = link_weight
        self.recency_weight = recency_weight
        self.recall_weight = recall_weight
        self.strength_weight = strength_weight
        self.decay_half_life = decay_half_life
        self.data_dir = data_dir or MEMORY_DIR

        # ── Salience Cache (dirty bit) ──
        # avoid recalculating five factors every time score() is called
        # invalidate(atom_id) called when atom changes
        self._cache: Dict[str, float] = {}     # atom_id -> salience
        self._dirty: Set[str] = set()          # needs recalculation atom_id
        self._cache_ttl: float = 60.0          # cache auto-expires in 60 seconds

    # ── Five-factor scoring (each returns [0,1]) ──

    def _score_emotion(self, atom: MemoryAtom) -> float:
        """emotion intensity score: absolute value."""
        return min(1.0, abs(atom.emotion) * 1.2)

    def _score_link_density(self, atom: MemoryAtom) -> float:
        """link density score: log scale."""
        n = len(atom.links)
        if n == 0:
            return 0.0
        # log_10(100) ≈ 2.0; use 2.0 as denominator
        return min(1.0, math.log10(n + 1) / 2.0)

    def _score_recency(self, atom: MemoryAtom) -> float:
        """recency score: exponential decay."""
        age = time.time() - atom.timestamp
        lam = math.log(2) / self.decay_half_life
        return math.exp(-lam * age)

    def _score_recall_freq(self, atom: MemoryAtom) -> float:
        """recall frequency score."""
        if atom.recall_count == 0:
            return 0.0
        # consider age normalization
        age = time.time() - max(atom.timestamp, 1)
        freq = atom.recall_count / max(1, age / 86400)  # times/day
        return min(1.0, freq / 10.0)  # 10 times per day = full score

    def _score_link_strength(self, atom: MemoryAtom) -> float:
        """link intensity score: weighted average."""
        if not atom.links:
            return 0.0
        avg = sum(atom.links.values()) / len(atom.links)
        return min(1.0, avg * 1.5)
    # ══════════════════════════════════════════
    # Heuristic quantification function
    # ══════════════════════════════════════════

    @staticmethod
    def compute_urgency(prompt: str) -> float:
        """
        calculate urgency from prompt text [0, 1].

        Formula:
            urgency = min(1.0, keyword_score × 0.4 + pattern_score × 0.4 + exclamation_score × 0.2)

        keyword_score: proportion of urgent words (urgent/asap/critical/blocker...)
        pattern_score: all caps / repeated punctuation / restricted mode
        exclamation_score: exclamation mark density
        """
        if not prompt:
            return 0.0

        text = prompt.lower()

        # ── Lexical score ──
        urgency_words = {
            "urgent": 1.0, "asap": 1.0, "immediately": 1.0,
            "critical": 1.0, "emergency": 1.0, "hotfix": 0.9,
            "p0": 1.0, "p1": 0.8, "p2": 0.5,
            "blocker": 0.9, "blocking": 0.8, "deadline": 0.8,
            "soon": 0.4, "important": 0.5, "priority": 0.6,
            "now": 0.6, "hurry": 0.7, "as soon as possible": 1.0,
            "stop": 0.6, "fix": 0.5, "broken": 0.7,
            "crash": 0.9, "down": 0.7, "fail": 0.6,
        }
        words = text.split()
        matched = sum(urgency_words.get(w, 0.0) for w in words)
        keyword_score = min(1.0, matched / max(1, len(words)) * 5)

        # ── Mode score ──
        caps_ratio = sum(1 for c in prompt if c.isupper()) / max(1, len(prompt))
        pattern_score = min(1.0, caps_ratio * 3)  # 33% caps = full score

        # ── Punctuation score ──
        excl = prompt.count("!") + prompt.count("!!!")
        exclamation_score = min(1.0, excl / 3)

        urgency = min(1.0, keyword_score * 0.4 + pattern_score * 0.4 + exclamation_score * 0.2)
        return round(urgency, 3)

    @staticmethod
    def compute_penalty(error: str) -> float:
        """
        calculate system error penalty score [0, 1].

        Formula:
            penalty = min(1.0, fatal_score × 0.6 + traceback_score × 0.2 + runtime_score × 0.2)

        fatal_score: fatal error type weight
        traceback_score: Traceback depth (call stack depth)
        runtime_score: runtime error type weight

        Typical mapping:
            SyntaxError / ImportError = 0.3 (easy to fix)
            ValueError / TypeError = 0.5
            TimeoutError / ConnectionError = 0.7
            MemoryError / OSError = 0.8
            SegmentationFault / Fatal = 1.0
            HTTP 5xx = 0.8
            HTTP 4xx = 0.4
        """
        if not error:
            return 0.0

        text = error.lower()

        # ── Fatality level ──
        fatal_patterns = {
            "segmentation fault": 1.0, "fatal": 1.0, "panic": 1.0,
            "kernel": 1.0, "memoryerror": 0.8,
            "out of memory": 0.8, "killed": 0.9,
            "oserror": 0.8, "permission denied": 0.6,
            "timeout": 0.7, "timed out": 0.7,
            "connection refused": 0.6, "connection reset": 0.6,
            "syntaxerror": 0.3, "importerror": 0.3, "modulenotfounderror": 0.3,
            "valueerror": 0.5, "typeerror": 0.5, "keyerror": 0.4,
            "indexerror": 0.4, "attributeerror": 0.4,
            "zerodivisionerror": 0.3, "filenotfounderror": 0.4,
            "runtimeerror": 0.6, "exception": 0.5,
            "5": 0.8, "50": 0.8, "40": 0.4, "403": 0.5,
            "404": 0.3, "429": 0.6,
        }
        fatal_score = 0.0
        for pattern, score in fatal_patterns.items():
            if pattern in text:
                fatal_score = max(fatal_score, score)

        # ── Traceback depth ──
        tb_depth = error.count('  File "')
        traceback_score = min(1.0, tb_depth / 10)

        # ── Runtime type ──
        runtime_score = 0.4
        if "interrupt" in text or "signal" in text:
            runtime_score = 0.3
        if "disk" in text or "i/o" in text:
            runtime_score = 0.7
        if "database" in text or "sql" in text:
            runtime_score = 0.6

        penalty = min(1.0, fatal_score * 0.6 + traceback_score * 0.2 + runtime_score * 0.2)
        return round(penalty, 3)

    @staticmethod
    def compute_reward(outcome: str) -> float:
        """
        calculate task success reward score [0, 1].

        Formula:
            reward = min(1.0, success_score × 0.5 + efficiency_score × 0.3 + novelty_score × 0.2)

        success_score: success signal (completed/passed/success etc.) score
        efficiency_score: efficiency metric (quick/fast/optimized etc. reduced something)
        novelty_score: new knowledge / new discovery

        Typical mapping:
            routine task completion = 0.3
            bug fix = 0.5
            performance optimization (2x+ faster) = 0.7
            new feature success = 0.6
            important milestone = 0.8
            breakthrough discovery = 1.0
        """
        if not outcome:
            return 0.0

        text = outcome.lower()

        # ── Success level ──
        success_patterns = {
            "success": 0.7, "succeeded": 0.7, "completed": 0.5,
            "passed": 0.5, "done": 0.4, "fixed": 0.5,
            "resolved": 0.5, "solved": 0.5,
            "deployed": 0.6, "released": 0.6, "shipped": 0.7,
            "milestone": 0.8, "breakthrough": 1.0,
            "achieved": 0.7, "delivered": 0.6,
            "approved": 0.5, "merged": 0.5, "published": 0.6,
            # text
            "success": 0.7, "completed": 0.5, "fixed": 0.5, "resolved": 0.5,
            "implemented": 0.6, "breakthrough": 0.9, "released": 0.6, "deploy": 0.6,
            "merge": 0.5, "passed": 0.5, "optimized": 0.6, "improved": 0.5,
        }
        success_score = 0.3  # base score (routine completion)
        for pattern, score in success_patterns.items():
            if pattern in text:
                success_score = max(success_score, score)

        # ── Efficiency improvement ──
        efficiency_score = 0.0
        if any(w in text for w in ["faster", "quick", "optimized", "optimized",
                                    "reduced", "improved performance"]):
            efficiency_score = 0.5
        if any(w in text for w in ["2x", "3x", "10x", "doubled", "halved"]):
            efficiency_score = max(efficiency_score, 0.7)

        # ── Novelty ──
        novelty_score = 0.0
        if any(w in text for w in ["new", "novel", "first", "discovered",
                                    "invented", "created", "discovery", "invented",
                                    "first", "innovative", "breakthrough"]):
            novelty_score = 0.5
        if any(w in text for w in ["breakthrough", "discovery", "innovation"]):
            novelty_score = max(novelty_score, 0.8)

        reward = min(1.0, success_score * 0.5 + efficiency_score * 0.3 + novelty_score * 0.2)
        return round(reward, 3)

    # ══════════════════════════════════════════
    # Main scoring API
    # ══════════════════════════════════════════

    def score(self, atom: MemoryAtom) -> float:
        """Calculate salience score for a single memory [0, 1].

        use dirty-bit cache: if atom_id in cache and not marked as dirty
        and TTL not exceeded, directly return cached value.
        """
        aid = atom.atom_id
        now = time.time()

        # check cache
        if aid in self._cache and aid not in self._dirty:
            # TTL check
            if hasattr(atom, '_cached_at') and now - atom._cached_at < self._cache_ttl:
                return self._cache[aid]

        # recalculate
        s = (
            self.emotion_weight * self._score_emotion(atom)
            + self.link_weight * self._score_link_density(atom)
            + self.recency_weight * self._score_recency(atom)
            + self.recall_weight * self._score_recall_freq(atom)
            + self.strength_weight * self._score_link_strength(atom)
        )
        s = max(0.0, min(1.0, s))

        # write cache
        self._cache[aid] = s
        self._dirty.discard(aid)
        # record cache on atom (avoid impure function side effects)
        # use _cached_at as weak reference proxy
        if not hasattr(atom, '_cached_at'):
            object.__setattr__(atom, '_cached_at', now)
        else:
            atom._cached_at = now

        return s

    def invalidate(self, atom_id: str):
        """Mark an atom's salience as dirty, next score() will recalculate."""
        self._dirty.add(atom_id)

    def clear_cache(self):
        """Clear entire salience cache (e.g., during sleep cycle)."""
        self._cache.clear()
        self._dirty.clear()

    def explain(self, atom: MemoryAtom) -> dict:
        """
        Return factor breakdown (for debug / UX).

        Returns:
            {"emotion": 0.3, "link_density": 0.5, ...,
             "total": 0.42, "importance": 0.6}
        """
        return {
            "emotion": round(self._score_emotion(atom), 3),
            "link_density": round(self._score_link_density(atom), 3),
            "recency": round(self._score_recency(atom), 3),
            "recall_freq": round(self._score_recall_freq(atom), 3),
            "link_strength": round(self._score_link_strength(atom), 3),
            "total": round(self.score(atom), 3),
            "importance_raw": round(atom.importance, 3),
        }

    def rank(self, atoms: List[MemoryAtom], top_k: int = 0) -> List[tuple]:
        """
        Sort memory list (salience descending).

        Args:
            atoms: memory atom list
            top_k: return K items (0=all)

        Returns:
            [(atom, salience), ...] in descending order of salience
        """
        scored = [(a, self.score(a)) for a in atoms]
        scored.sort(key=lambda x: -x[1])
        if top_k > 0:
            scored = scored[:top_k]
        return scored

    # ── state ──

    def weights(self) -> dict:
        return {
            "emotion_weight": self.emotion_weight,
            "link_weight": self.link_weight,
            "recency_weight": self.recency_weight,
            "recall_weight": self.recall_weight,
            "strength_weight": self.strength_weight,
        }

    def to_dict(self) -> dict:
        return self.weights()

    @classmethod
    def from_dict(cls, d: dict) -> "Amygdala":
        return cls(
            emotion_weight=d.get("emotion_weight", 0.25),
            link_weight=d.get("link_weight", 0.20),
            recency_weight=d.get("recency_weight", 0.20),
            recall_weight=d.get("recall_weight", 0.15),
            strength_weight=d.get("strength_weight", 0.20),
        )
