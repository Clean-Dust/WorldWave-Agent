"""Subconscious Resource Scheduling Matrix.

Implements Gemini's TRIAGE framework for dynamic resource allocation:
  "The subconscious plays the role of a global dynamic resource allocator,
   outputting real-time inference hyperparameters and resource arrays."

Four primitives (TRIAGE):
  - Feasibility: Can the task be completed within budget?
  - Cost: Token cost estimation
  - Selection: Which subtask to prioritize
  - Sequencing: Task ordering for maximum expected utility

Outputs:
  - Token budget per subtask
  - Priority ranking
  - Termination conditions
  - Tool invocation caps
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TaskBudget:
    """Resource budget for a single subtask."""
    task_id: str = ""
    priority: int = 0
    token_budget: int = 2000         # Max tokens for this task
    remaining_tokens: int = 2000
    max_tool_calls: int = 10          # Cap tool invocations
    tool_calls_used: int = 0
    max_spirals: int = 5              # Max cognitive spirals
    spirals_used: int = 0
    deadline: float = 0.0             # Absolute time deadline (0 = none)
    status: str = "pending"           # pending, active, completed, exhausted

    @property
    def exhausted(self) -> bool:
        return (self.remaining_tokens <= 0 or
                self.tool_calls_used >= self.max_tool_calls or
                self.spirals_used >= self.max_spirals)


@dataclass
class ResourceSchedule:
    """Global resource schedule across all active tasks."""
    total_token_budget: int = 100000
    tokens_used: int = 0
    active_tasks: Dict[str, TaskBudget] = field(default_factory=dict)
    priority_queue: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def utilization(self) -> float:
        return self.tokens_used / max(self.total_token_budget, 1)


class ResourceScheduler:
    """Dynamic token budget allocation across concurrent subtasks.

    Usage:
        scheduler = ResourceScheduler(total_budget=50000)

        # Allocate budget for a new subtask
        budget = scheduler.allocate("task_1", priority=1)

        # Record consumption
        scheduler.consume("task_1", tokens=150, tool_calls=1)

        # Check if a task should be terminated
        if scheduler.should_terminate("task_1"):
            ...
    """

    def __init__(self, total_budget: int = 100000):
        self.schedule = ResourceSchedule(total_token_budget=total_budget)

    def allocate(
        self,
        task_id: str,
        priority: int = 0,
        estimated_tokens: int = 2000,
        max_tool_calls: int = 10,
        max_spirals: int = 5,
    ) -> TaskBudget:
        """Allocate a token budget for a new task.

        Higher priority tasks get larger budgets.
        Lower priority tasks get throttled if budget is tight.
        """
        # Check remaining global budget
        remaining = self.schedule.total_token_budget - self.schedule.tokens_used

        # Weight by priority: high priority gets more budget
        priority_factor = min(3.0, max(0.3, 1.0 + priority * 0.5))
        allocated = min(
            estimated_tokens,
            int(remaining * 0.3 * priority_factor),  # Max 30% of remaining per task
        )

        budget = TaskBudget(
            task_id=task_id,
            priority=priority,
            token_budget=allocated,
            remaining_tokens=allocated,
            max_tool_calls=max_tool_calls,
            max_spirals=max_spirals,
            status="active",
        )

        self.schedule.active_tasks[task_id] = budget
        self.schedule.priority_queue.append(task_id)
        # Sort by priority descending
        self.schedule.priority_queue.sort(
            key=lambda tid: self.schedule.active_tasks[tid].priority, reverse=True
        )

        return budget

    def consume(self, task_id: str, tokens: int = 0, tool_calls: int = 0,
                spirals: int = 0):
        """Record token/tool/spiral consumption for a task."""
        budget = self.schedule.active_tasks.get(task_id)
        if not budget:
            return

        budget.remaining_tokens = max(0, budget.remaining_tokens - tokens)
        budget.tool_calls_used += tool_calls
        budget.spirals_used += spirals
        self.schedule.tokens_used += tokens

    def should_terminate(self, task_id: str) -> bool:
        """Check if a task should be aborted (budget exhausted)."""
        budget = self.schedule.active_tasks.get(task_id)
        if not budget:
            return True
        return budget.exhausted

    def get_highest_priority(self) -> Optional[str]:
        """Get the highest-priority active task ID."""
        for tid in self.schedule.priority_queue:
            if tid in self.schedule.active_tasks:
                budget = self.schedule.active_tasks[tid]
                if not budget.exhausted and budget.status == "active":
                    return tid
        return None

    def release(self, task_id: str):
        """Release a task's budget back to the pool."""
        budget = self.schedule.active_tasks.pop(task_id, None)
        if budget:
            self.schedule.tokens_used -= (
                budget.token_budget - budget.remaining_tokens
            )
            if task_id in self.schedule.priority_queue:
                self.schedule.priority_queue.remove(task_id)

    def throttle_factor(self, task_id: str) -> float:
        """Get a throttle multiplier (0-1) for how aggressively to limit this task.

        Returns 1.0 for normal, <1.0 when budget is tight.
        """
        budget = self.schedule.active_tasks.get(task_id)
        if not budget:
            return 0.0

        utilization = self.schedule.utilization
        remaining_ratio = budget.remaining_tokens / max(budget.token_budget, 1)

        # Tight budget + low remaining → throttle hard
        if utilization > 0.8 and remaining_ratio < 0.2:
            return 0.3
        elif utilization > 0.6:
            return 0.6
        return 1.0

    def to_dict(self) -> dict:
        return {
            "total_budget": self.schedule.total_token_budget,
            "tokens_used": self.schedule.tokens_used,
            "utilization": round(self.schedule.utilization, 2),
            "active_tasks": len(self.schedule.active_tasks),
            "tasks": {
                tid: {
                    "priority": b.priority,
                    "remaining": b.remaining_tokens,
                    "total": b.token_budget,
                    "tools_used": b.tool_calls_used,
                    "spirals_used": b.spirals_used,
                    "status": b.status,
                    "exhausted": b.exhausted,
                }
                for tid, b in self.schedule.active_tasks.items()
            },
        }
