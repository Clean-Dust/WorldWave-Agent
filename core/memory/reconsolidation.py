"""
ww/core/memory/reconsolidation.py — reconsolidation

Reconsolidation is the update process that occurs with each recall:
- Stability trace: stable memories are not easily pruned
- Context integration: recall will merge context into memory
- Emotion adjustment: modify emotion tag based on confirmation/rejection signals
- Confidence adjustment: Fact Store facts are corrected with validation
"""

from __future__ import annotations
import json
import logging
import math
import os
import time
from typing import Dict, List, Optional, Set, Tuple

from .atom import FactStore, MemoryAtom
from .hippocampus import Hippocampus

logger = logging.getLogger("ww.memory.reconsolidation")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
MEMORY_DIR = os.path.join(_WW_CFG, "memory")


class Reconsolidation:
    """
    reconsolidationengine. 

    Each recall auto-updates related memories:
    1. Stability decay slows (recall increases memory stability)
    2. Context trace (stores recall context)
    3. Emotion confirmation adjustment (if user confirms/refutes)

    Stability formula:
        stability += recall_bonus × (1 - stability)
        stability -= time_decay × age_in_days

    recall_bonus = 0.05 (per recall)
    time_decay = 0.02 (per day)
    """

    def __init__(
        self,
        recall_bonus: float = 0.05,
        time_decay: float = 0.02,
        max_stability: float = 10.0,
        data_dir: str = "",
    ):
        self.recall_bonus = recall_bonus
        self.time_decay = time_decay
        self.max_stability = max_stability
        self.data_dir = data_dir or MEMORY_DIR

    def on_recall(self, atom: MemoryAtom, context: str = "") -> dict:
        """
        Update with each recall.

        Args:
            atom: The recalled memory atom
            context: The recall context (selective)

        Returns:
            updatelog
        """
        updates = {}

        # 1. Increase stability
        old_stability = atom.stability
        atom.stability = min(
            self.max_stability,
            atom.stability + self.recall_bonus * (1 - atom.stability / self.max_stability),
        )
        updates["stability"] = {"old": round(old_stability, 3),
                                 "new": round(atom.stability, 3)}

        # 2. Update recall count
        atom.recall_count += 1
        atom.last_recalled = time.time()
        updates["recall_count"] = atom.recall_count

        # 3. Context trace (if provided)
        if context:
            trace = {
                "time": time.time(),
                "context": context[:200],
            }
            atom.context_trace.append(trace)
            updates["context_traced"] = True

        return updates

    def decay_stability(self, atoms: List[MemoryAtom]) -> dict:
        """
        Execute decay on all memories.

        Should be called during sleep consolidation / periodically.
        """
        now = time.time()
        decayed_count = 0
        for atom in atoms:
            if atom.is_core:  # Core memory is immune to decay
                continue
            if atom.is_immutable:  # Code memory — never decay
                continue
            age_days = (now - atom.timestamp) / 86400
            decay = self.time_decay * age_days
            if decay > 0 and atom.stability > 1.0:
                atom.stability = max(1.0, atom.stability - decay)
                decayed_count += 1
        return {"decayed_count": decayed_count}

    def confirm_fact(self, fact_store: FactStore, fact_id: int,
                      confirmed: bool, feedback: str = ""):
        """
        User confirms or refutes a fact.

        Args:
            fact_store: FactStore instance
            fact_id: fact ID
            confirmed: True indicates confirmation
            feedback: Feedback text
        """
        delta = 0.1 if confirmed else -0.2
        fact_store.update_trust(fact_id, delta)
        if feedback:
            fact_store.add(
                content=f"[FEEDBACK] {feedback}",
                entities=["feedback"],
                tags=["feedback"],
            )

    def merge_experience(self, atom: MemoryAtom,
                          prev_context: str, outcome: str) -> MemoryAtom:
        """
        Will merge the result of an experience back into the original memory.

        Args:
            atom: Original memory
            prev_context: Previous context
            outcome: Outcome description

        Returns:
            update  atom (in-place) 
        """
        # mergecontent
        atom.content = f"{atom.content} | Result: {outcome[:100]}"
        # Emotion update: adjust based on outcome
        outcome_lower = outcome.lower()
        if any(w in outcome_lower for w in ["success", "completed", "passed", "✓"]):
            atom.emotion = min(1.0, atom.emotion + 0.1)
        elif any(w in outcome_lower for w in ["fail", "error", "error", "✗"]):
            atom.emotion = max(-1.0, atom.emotion - 0.2)
        return atom

    def explain(self, atom: MemoryAtom) -> dict:
        """Explain memory reconsolidation state."""
        return {
            "atom_id": atom.atom_id,
            "stability": round(atom.stability, 3),
            "recall_count": atom.recall_count,
            "last_recalled": atom.last_recalled,
            "age_days": round((time.time() - atom.timestamp) / 86400, 1),
        }
