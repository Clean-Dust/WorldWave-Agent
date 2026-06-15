"""ww/core/task_queue.py — Async Task Queue v0.1

Replaces the fire-and-forget background thread with a proper
task queue supporting:
- Task ID generation and tracking
- Status lifecycle: pending → running → completed/failed
- Result retrieval by task_id
- Configurable max concurrent tasks
- Thread-safe operations

Usage:
    q = TaskQueue(max_workers=3)
    task_id = q.submit(lambda: do_work())
    status = q.status(task_id)    # "pending"/"running"/"completed"/"failed"
    result = q.result(task_id)    # blocks until done
    tasks = q.list_tasks()        # all tasks with status
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Task:
    """A single queued/executed task."""
    task_id: str
    goal: str
    status: str = "pending"   # pending, running, completed, failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    max_spirals: int = 10


class TaskQueue:
    """Thread-safe async task queue with result tracking."""

    def __init__(self, max_workers: int = 3, max_history: int = 100):
        self._max_workers = max_workers
        self._max_history = max_history
        self._lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}
        self._order: List[str] = []  # insertion order
        self._active_count = 0
        self._semaphore = threading.Semaphore(max_workers)

    def submit(self, goal: str, fn: Callable[[], Dict],
               max_spirals: int = 10) -> str:
        """Submit a task for execution. Returns task_id immediately."""
        task_id = uuid.uuid4().hex[:12]
        task = Task(task_id=task_id, goal=goal, max_spirals=max_spirals)

        with self._lock:
            self._tasks[task_id] = task
            self._order.append(task_id)
            # Trim history
            while len(self._order) > self._max_history:
                old_id = self._order.pop(0)
                if self._tasks[old_id].status in ("completed", "failed"):
                    del self._tasks[old_id]

        # Start in background thread
        t = threading.Thread(
            target=self._run_task,
            args=(task, fn),
            daemon=True,
            name=f"task-{task_id}",
        )
        t.start()
        return task_id

    def _run_task(self, task: Task, fn: Callable[[], Dict]):
        """Execute a task in a worker thread."""
        self._semaphore.acquire()
        try:
            with self._lock:
                self._active_count += 1
                task.status = "running"
                task.started_at = time.time()

            result = fn()
            task.result = result
            task.status = "completed"

        except Exception as e:
            task.status = "failed"
            task.error = str(e)

        finally:
            task.finished_at = time.time()
            with self._lock:
                self._active_count -= 1
            self._semaphore.release()

    def status(self, task_id: str) -> Optional[Dict]:
        """Get task status."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        return {
            "task_id": task.task_id,
            "goal": task.goal[:200],
            "status": task.status,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "has_result": task.result is not None,
            "error": task.error,
        }

    def result(self, task_id: str) -> Optional[Dict]:
        """Get task result (blocks until task completes)."""
        task = self._tasks.get(task_id)
        if not task:
            return None

        # Wait for completion
        while task.status in ("pending", "running"):
            time.sleep(0.1)

        return {
            "task_id": task.task_id,
            "status": task.status,
            "result": task.result,
            "error": task.error,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
        }

    def list_tasks(self, status: str = None, limit: int = 20) -> List[Dict]:
        """List tasks, optionally filtered by status."""
        tasks = []
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            for tid in ids:
                t = self._tasks.get(tid)
                if t and (status is None or t.status == status):
                    tasks.append({
                        "task_id": t.task_id,
                        "goal": t.goal[:100],
                        "status": t.status,
                        "created_at": t.created_at,
                        "has_result": t.result is not None,
                    })
        return tasks

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def total_tracked(self) -> int:
        return len(self._tasks)
