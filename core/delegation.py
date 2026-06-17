"""
ww/core/delegation.py — Worldwave subtask delegation system v0.1

Allows WW to spawn child Agents and process subtasks inline:
- Child agents  is isolate  Worldwave instance
- Each subtask has independent context and session
- Supports sync wait or background execution
- Limit maximum parallel count (prevent resource exhaustion)
- Result collection and merge
"""

from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.logger import get_logger


# ── defaultvalue ──
MAX_CONCURRENT_CHILDREN = 3
MAX_CHILD_TOKENS = 4096
DELEGATION_TIMEOUT = 300  # 5 minutes


class ChildTask:
    """a subtask"""

    def __init__(
        self,
        task_id: str,
        goal: str,
        context: str = "",
        tools: List[str] = None,
        max_spirals: int = 3,
        timeout: int = DELEGATION_TIMEOUT,
    ):
        self.task_id = task_id
        self.goal = goal
        self.context = context
        self.tools = tools or []
        self.max_spirals = max_spirals
        self.timeout = timeout
        self.status = "pending"  # pending | running | done | failed | timed_out
        self.result: Optional[Dict] = None
        self.error: Optional[str] = None
        self.started_at: Optional[str] = None
        self.completed_at: Optional[str] = None
        self.spirals_used: int = 0
        self.tokens_used: int = 0

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal[:100],
            "status": self.status,
            "spirals_used": self.spirals_used,
            "tokens_used": self.tokens_used,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class DelegationManager:
    """
    subtask delegation management 

    usage: 
        dm = DelegationManager()
        results = dm.delegate([
            ChildTask(goal="Analyze A"),
            ChildTask(goal="Analyze B"),
        ])
    """

    def __init__(
        self,
        ww_factory=None,  # Callable that returns a new Worldwave instance
        max_concurrent: int = MAX_CONCURRENT_CHILDREN,
        default_timeout: int = DELEGATION_TIMEOUT,
    ):
        self._ww_factory = ww_factory or self._default_ww
        self.max_concurrent = max_concurrent
        self.default_timeout = default_timeout
        self._active_count = 0
        self._log = get_logger()

    def delegate(
        self,
        tasks: List[ChildTask],
        max_concurrent: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> List[ChildTask]:
        """
    Assign multiple subtasks and execute them in parallel.

    Each subtask runs on an independent Worldwave instance.

        Args:
            tasks: subtask list
            max_concurrent: maximum parallel count (override)
            timeout: single task timeout seconds (override)

        Returns:
            Complete task list (each task result padding)
        """
        if not tasks:
            return []

        concurrent = min(max_concurrent or self.max_concurrent, MAX_CONCURRENT_CHILDREN)
        to = timeout or self.default_timeout

        self._log(f"🌱 Delegating {len(tasks)} tasks (max {concurrent} concurrent)")

        with ThreadPoolExecutor(max_workers=concurrent) as executor:
            futures = {}
            for task in tasks:
                future = executor.submit(self._run_child, task, to)
                futures[future] = task

            for future in as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                    task.result = result
                    task.status = "done"
                except Exception as e:
                    task.status = "failed"
                    task.error = str(e)[:200]
                    self._log(f"  ❌ {task.task_id}: {task.error}")

        done = sum(1 for t in tasks if t.status == "done")
        failed = sum(1 for t in tasks if t.status == "failed")
        self._log(f"🌱 Delegation complete: {done} done, {failed} failed")
        return tasks

    def delegate_sync(
        self,
        goal: str,
        context: str = "",
        max_spirals: int = 3,
        timeout: Optional[int] = None,
    ) -> Dict:
        """single sync sub-task (simplified delegate)"""
        task = ChildTask(
            task_id=uuid.uuid4().hex[:8],
            goal=goal,
            context=context,
            max_spirals=max_spirals,
            timeout=timeout or self.default_timeout,
        )
        results = self.delegate([task])
        return results[0].result or {"error": results[0].error, "status": results[0].status}

    def _run_child(self, task: ChildTask, timeout: int) -> Dict:
        """execute a sub-task in an isolated environment"""
        task.status = "running"
        task.started_at = datetime.now(timezone.utc).isoformat()

        try:
            ww = self._ww_factory()
            result = ww.run(task.goal, max_spirals=task.max_spirals)
            task.spirals_used = result.get("spirals_completed", 0)
            task.completed_at = datetime.now(timezone.utc).isoformat()
            return result
        except Exception as e:
            task.status = "failed"
            task.error = str(e)[:200]
            task.completed_at = datetime.now(timezone.utc).isoformat()
            return {"error": str(e), "status": "failed", "spirals_completed": 0}

    def _default_ww(self):
        """default WW instance factory"""
        from core.loop import Worldwave
        return Worldwave()

    def stats(self) -> Dict:
        """when  statistics"""
        return {
            "max_concurrent": self.max_concurrent,
            "active_count": self._active_count,
        }


class ParallelPlanner:
    """
    parallel planning: auto decompose big goal into sub-tasks → parallel execute → merge results

    usage:
        planner = ParallelPlanner(ww)
        result = planner.plan_and_execute("Analyze three documents")
    """

    def __init__(self, ww, delegator: Optional[DelegationManager] = None):
        self.ww = ww
        self.delegator = delegator or DelegationManager()

    def decompose(self, goal: str) -> List[ChildTask]:
        """Use LLM to decompose the big goal into multiple sub-tasks that can be executed in parallel"""
        try:
            plan = self.ww.llm.chat_json(
                messages=[{"role": "user", "content": (
                    f"will decompose the goal into up to {MAX_CONCURRENT_CHILDREN} parallel executable subtasks."
                    f"each subtask should be able to complete independently.\n\ngoal: {goal}\n\n"
                    f"output JSON format: {{\"tasks\": [{{\"goal\": \"...\", "
                    f"\"context\": \"...\", \"max_spirals\": 3}}]}}"
                )}],
                phase="plan",
            )
            tasks_data = plan.get("tasks", []) if isinstance(plan, dict) else []
        except Exception:
            tasks_data = []

        if not tasks_data:
            # Fallback: single task
            return [ChildTask(
                task_id=uuid.uuid4().hex[:8],
                goal=goal,
                max_spirals=5,
            )]

        return [
            ChildTask(
                task_id=uuid.uuid4().hex[:8],
                goal=t.get("goal", goal),
                context=t.get("context", ""),
                max_spirals=t.get("max_spirals", 3),
            )
            for t in tasks_data[:MAX_CONCURRENT_CHILDREN]
        ]

    def plan_and_execute(self, goal: str, auto_decompose: bool = True) -> Dict:
        """plan and execute"""
        if auto_decompose:
            tasks = self.decompose(goal)
        else:
            tasks = [ChildTask(
                task_id="main",
                goal=goal,
                max_spirals=5,
            )]

        if len(tasks) <= 1:
            # single task, directly execute at main loop
            return self.ww.run(goal)

        # multi-task parallel
        results = self.delegator.delegate(tasks)

        # merge result
        merged = {
            "status": "completed" if any(t.status == "done" for t in results) else "failed",
            "goal": goal,
            "sub_tasks": len(results),
            "done": sum(1 for t in results if t.status == "done"),
            "failed": sum(1 for t in results if t.status == "failed"),
            "results": [t.to_dict() for t in results],
            "total_spirals": sum(t.spirals_used for t in results),
        }
        return merged
