"""
ww/core/subconscious/scheduler.py — TRIAGE-inspired Resource Scheduler

Dynamic allocation of token budgets and task resources across parallel
workstreams.  Implements the TRIAGE framework primitives:

  - Feasibility: can this subtask succeed with remaining budget?
  - Cost: how many tokens will it consume?
  - Selection: which subtasks to prioritise?
  - Sequencing: what order to run them?

Pure Python, zero external dependencies.
"""

from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.subconscious.scheduler")

SCHED_DIR = os.path.expanduser("~/worldwave/data/subconscious/scheduling")


@dataclass
class TaskSlot:
    """One tracked subtask.

    Attributes:
        task_id: unique identifier
        name: human-readable name
        estimated_cost: predicted token cost (set externally)
        actual_cost: actual tokens consumed so far
        budget: max tokens allocated
        priority: 0=highest, higher=lower
        success_probability: 0.0-1.0, estimated chance of success
        active: whether currently being executed
        completed: whether finished
        abandoned: whether terminated early
        created_at: timestamp
    """
    task_id: str = ""
    name: str = ""
    estimated_cost: int = 0
    actual_cost: int = 0
    budget: int = 1000
    priority: int = 5
    success_probability: float = 0.5
    active: bool = False
    completed: bool = False
    abandoned: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "estimated_cost": self.estimated_cost,
            "actual_cost": self.actual_cost,
            "budget": self.budget,
            "priority": self.priority,
            "success_probability": round(self.success_probability, 3),
            "active": self.active,
            "completed": self.completed,
            "abandoned": self.abandoned,
        }


class TaskBudgetTracker:
    """Tracks token budgets across all active subtasks."""

    def __init__(self, global_budget: int = 100000):
        self.global_budget = global_budget
        self._tasks: Dict[str, TaskSlot] = {}
        self._order: List[str] = []

    def register(self, task_id: str, name: str = "",
                 estimated_cost: int = 0, budget: int = 1000,
                 priority: int = 5) -> TaskSlot:
        """Register a new task."""
        slot = TaskSlot(
            task_id=task_id, name=name,
            estimated_cost=estimated_cost, budget=budget,
            priority=priority,
        )
        self._tasks[task_id] = slot
        self._order.append(task_id)
        return slot

    def get(self, task_id: str) -> Optional[TaskSlot]:
        return self._tasks.get(task_id)

    def consume(self, task_id: str, tokens: int):
        """Record token consumption for a task."""
        slot = self._tasks.get(task_id)
        if slot:
            slot.actual_cost += tokens

    def mark_complete(self, task_id: str):
        slot = self._tasks.get(task_id)
        if slot:
            slot.active = False
            slot.completed = True

    def mark_active(self, task_id: str):
        slot = self._tasks.get(task_id)
        if slot:
            slot.active = True

    def abandon(self, task_id: str):
        slot = self._tasks.get(task_id)
        if slot:
            slot.active = False
            slot.abandoned = True

    def set_success_prob(self, task_id: str, prob: float):
        slot = self._tasks.get(task_id)
        if slot:
            slot.success_probability = max(0.0, min(1.0, prob))

    @property
    def total_consumed(self) -> int:
        return sum(t.actual_cost for t in self._tasks.values())

    @property
    def remaining_budget(self) -> int:
        return self.global_budget - self.total_consumed

    def active_tasks(self) -> List[TaskSlot]:
        return [t for t in self._tasks.values() if t.active and not t.completed]

    def pending_tasks(self) -> List[TaskSlot]:
        return [t for t in self._tasks.values()
                if not t.active and not t.completed and not t.abandoned]

    def stats(self) -> dict:
        active = len(self.active_tasks())
        pending = len(self.pending_tasks())
        completed = sum(1 for t in self._tasks.values() if t.completed)
        abandoned = sum(1 for t in self._tasks.values() if t.abandoned)
        return {
            "total_tasks": len(self._tasks),
            "active": active,
            "pending": pending,
            "completed": completed,
            "abandoned": abandoned,
            "consumed": self.total_consumed,
            "remaining": self.remaining_budget,
            "budget_used_pct": round(self.total_consumed / max(1, self.global_budget) * 100, 1),
        }

    def reset(self):
        self._tasks.clear()
        self._order.clear()


class ResourceScheduler:
    """Makes dynamic resource allocation decisions using subconscious state.

    Integrates with the feature vector to inform:
      - which tasks to run next (selection)
      - how much budget to allocate (cost)
      - when to abandon ineffective paths (feasibility)

    Usage:
        rs = ResourceScheduler()
        rs.register_task("task_1", estimated_cost=500, priority=3)
        decision = rs.evaluate(feature_vector)
        # decision.next_task_id: which task to run next
        # decision.abandon_ids: which tasks to abandon
    """

    def __init__(
        self,
        tracker: Optional[TaskBudgetTracker] = None,
        abandon_threshold: float = 0.2,
        min_budget_per_task: int = 100,
        auto_persist: bool = True,
        data_dir: str = SCHED_DIR,
    ):
        """
        Args:
            abandon_threshold: abandon tasks with success_probability below this
            min_budget_per_task: minimum tokens to allocate to any active task
        """
        self.tracker = tracker or TaskBudgetTracker()
        self.abandon_threshold = abandon_threshold
        self.min_budget = min_budget_per_task
        self.auto_persist = auto_persist
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

        self._decision_count = 0
        self._last_features: Optional[List[float]] = None

    # ── Delegate to tracker ──

    def register_task(self, task_id: str, name: str = "",
                      estimated_cost: int = 0, budget: int = 1000,
                      priority: int = 5) -> TaskSlot:
        """Register a subtask."""
        return self.tracker.register(task_id, name, estimated_cost, budget, priority)

    def consume(self, task_id: str, tokens: int):
        self.tracker.consume(task_id, tokens)

    def complete(self, task_id: str):
        self.tracker.mark_complete(task_id)

    def mark_active(self, task_id: str):
        self.tracker.mark_active(task_id)

    def abandon(self, task_id: str):
        self.tracker.abandon(task_id)

    # ── Core decision logic ──

    def evaluate(self, features: Optional[List[float]] = None) -> Dict[str, Any]:
        """Make resource allocation decision.

        Args:
            features: 32-dim feature vector from FeatureExtractor

        Returns:
            dict with:
              - next_task_id: which task to execute next ("" if none)
              - abandon_ids: tasks to abandon
              - budget_allocation: {task_id: tokens}
              - priority_order: ordered task list
              - reasoning: short explanation
        """
        self._last_features = features
        decisions: Dict[str, Any] = {
            "next_task_id": "",
            "abandon_ids": [],
            "budget_allocation": {},
            "priority_order": [],
            "reasoning": "",
        }

        pending = self.tracker.pending_tasks()
        active = self.tracker.active_tasks()

        # 1. Check budget pressure from feature vector
        budget_strain = 0.0
        if features and len(features) > 17:
            ctx_pressure = features[17]  # context_window_pressure
            mem_free = features[16] if len(features) > 16 else 0.5
            budget_strain = ctx_pressure + (1.0 - mem_free) * 0.5

        # 2. Abandon low-probability tasks when under pressure
        for slot in list(self.tracker._tasks.values()):
            if slot.completed or slot.abandoned:
                continue
            if slot.active:
                # Check running tasks: if over budget and low success prob, abandon
                over_budget = slot.actual_cost > slot.budget * 1.5 if slot.budget > 0 else False
                if over_budget and slot.success_probability < self.abandon_threshold:
                    self.tracker.abandon(slot.task_id)
                    decisions["abandon_ids"].append(slot.task_id)

        # 3. Sort pending by priority * expected_value
        scored = []
        for slot in pending:
            # Priority score: lower priority value = higher importance
            priority_score = max(0.1, 10 - slot.priority) / 10.0
            # Expected value = success_prob * priority_score / estimated_cost
            expected_value = (slot.success_probability * priority_score /
                              max(1, slot.estimated_cost))
            scored.append((expected_value, slot))

        scored.sort(key=lambda x: -x[0])  # descending

        # 4. Allocate budget
        remaining = self.tracker.remaining_budget
        for _, slot in scored:
            alloc = min(slot.budget, max(self.min_budget, remaining // max(1, len(scored))))
            decisions["budget_allocation"][slot.task_id] = alloc
            decisions["priority_order"].append(slot.task_id)

        # 5. Pick next task
        if scored:
            decisions["next_task_id"] = scored[0][1].task_id
            reasons = []
            if budget_strain > 0.5:
                reasons.append(f"budget strain {budget_strain:.2f}")
            if decisions["abandon_ids"]:
                reasons.append(f"abandoned {len(decisions['abandon_ids'])} low-value tasks")
            decisions["reasoning"] = "; ".join(reasons) if reasons else "normal scheduling"

        self._decision_count += 1

        # Persist
        if self.auto_persist:
            self._persist(decisions)

        return decisions

    def update_success_probability(self, task_id: str, outcome: bool):
        """Update success probability for a task based on outcome.

        Uses exponential moving average.
        """
        slot = self.tracker.get(task_id)
        if not slot:
            return
        # Update estimate
        current = slot.success_probability
        new_prob = current * 0.7 + (1.0 if outcome else 0.0) * 0.3
        self.tracker.set_success_prob(task_id, new_prob)

    def stats(self) -> dict:
        return {
            "tracker": self.tracker.stats(),
            "decisions": self._decision_count,
            "abandon_threshold": self.abandon_threshold,
        }

    def _persist(self, decision: dict):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, f"decision_{self._decision_count}.json")
            with open(path, "w") as f:
                json.dump(decision, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Scheduler: persist failed: {e}")

    def reset(self):
        self.tracker.reset()
        self._decision_count = 0
        self._last_features = None
