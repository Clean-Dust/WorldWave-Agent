"""
ww/core/kanban.py — Worldwave lightweight task kanban v0.1

Built-in task management system:
- Four states: todo → in_progress → done
- Task assignment
- Priority sort
- JSON persistence
- supports blocking / dependencies

usage: 
    kb = KanbanBoard()
    kb.add("Fix bug", assignee="worker-a", priority=3)
    kb.list()
    kb.complete("task-001")
"""

from __future__ import annotations
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

DEFAULT_STATUSES = ["todo", "in_progress", "done"]
STATUS_ICONS = {"todo": "📋", "in_progress": "🔄", "done": "✅", "blocked": "🔴"}


class Task:
    """a kanban task"""

    def __init__(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        priority: int = 0,
        category: str = "general",
        depends_on: List[str] = None,
        tags: List[str] = None,
        task_id: str = "",
    ):
        self.task_id = task_id or uuid.uuid4().hex[:8]
        self.title = title
        self.description = description
        self.assignee = assignee
        self.priority = priority
        self.category = category
        self.status = "todo"
        self.depends_on = depends_on or []  # task_ids this blocks on
        self.tags = tags or []
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at
        self.completed_at: Optional[str] = None
        self.notes: List[str] = []
        self.metadata: Dict = {}

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description[:200],
            "assignee": self.assignee,
            "priority": self.priority,
            "category": self.category,
            "status": self.status,
            "depends_on": self.depends_on,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Task":
        task = cls(
            title=data["title"],
            description=data.get("description", ""),
            assignee=data.get("assignee", ""),
            priority=data.get("priority", 0),
            category=data.get("category", "general"),
            depends_on=data.get("depends_on", []),
            tags=data.get("tags", []),
            task_id=data.get("task_id", ""),
        )
        task.status = data.get("status", "todo")
        task.created_at = data.get("created_at", task.created_at)
        task.updated_at = data.get("updated_at", task.updated_at)
        task.completed_at = data.get("completed_at")
        task.notes = data.get("notes", [])
        task.metadata = data.get("metadata", {})
        return task

    def __repr__(self):
        icon = STATUS_ICONS.get(self.status, "📌")
        priority_str = "!" * (self.priority + 1) if self.priority > 0 else ""
        return f"<{icon} {self.task_id} {self.title} {priority_str}>"


class KanbanBoard:
    """kanban management """

    def __init__(self, data_dir: str = "", board_name: str = "default"):
        self.data_dir = data_dir or os.environ.get(
            "WW_CONFIG", os.path.expanduser("~/.ww")
        )
        self.board_name = board_name
        self._tasks: Dict[str, Task] = {}
        self._statuses = DEFAULT_STATUSES
        self._load()

    # ── CRUD ──

    def add(self, title: str, **kwargs) -> Task:
        """Add task"""
        task = Task(title=title, **kwargs)
        self._tasks[task.task_id] = task
        self._save()
        return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def update(self, task_id: str, **fields) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        for k, v in fields.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = datetime.now(timezone.utc).isoformat()
        if fields.get("status") == "done":
            task.completed_at = task.updated_at
        self._save()
        return task

    def delete(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save()
            return True
        return False

    # ── Status transitions ──

    def assign(self, task_id: str, assignee: str) -> bool:
        return self.update(task_id, assignee=assignee) is not None

    def start(self, task_id: str) -> bool:
        return self.update(task_id, status="in_progress") is not None

    def complete(self, task_id: str) -> bool:
        return self.update(task_id, status="done") is not None

    def block(self, task_id: str, reason: str = "") -> bool:
        task = self.update(task_id, status="blocked")
        if task and reason:
            task.notes.append(f"BLOCKED: {reason}")
            self._save()
        return task is not None

    def unblock(self, task_id: str) -> bool:
        return self.update(task_id, status="in_progress") is not None

    # ── Queries ──

    def list(self, status: str = "", assignee: str = "",
             category: str = "", limit: int = 50) -> List[Dict]:
        """List tasks (sorted by priority)"""
        tasks = list(self._tasks.values())

        if status:
            tasks = [t for t in tasks if t.status == status]
        if assignee:
            tasks = [t for t in tasks if t.assignee == assignee]
        if category:
            tasks = [t for t in tasks if t.category == category]

        # Sort: blocked first, then by priority desc, then by created_at
        def sort_key(t: Task):
            blocked_val = 0 if t.status == "blocked" else 1
            return (blocked_val, t.priority, t.created_at)

        tasks.sort(key=sort_key, reverse=True)
        return [t.to_dict() for t in tasks[:limit]]

    def list_by_status(self) -> Dict[str, List[Dict]]:
        """List tasks grouped by state"""
        result = {}
        for status in self._statuses:
            result[status] = self.list(status=status)
        return result

    def stats(self) -> Dict:
        """Kanban statistics"""
        counts = {s: 0 for s in self._statuses}
        for t in self._tasks.values():
            counts[t.status] = counts.get(t.status, 0) + 1
        total = len(self._tasks)
        return {
            "total": total,
            "by_status": counts,
            "completion_rate": round(
                (counts.get("done", 0) / max(total, 1)) * 100, 1
            ),
        }

    def add_note(self, task_id: str, note: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.notes.append(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] {note}")
        task.updated_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

    # ── Persistence ──

    def _path(self) -> str:
        return os.path.join(self.data_dir, f"kanban_{self.board_name}.json")

    def _save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        data = {
            "board": self.board_name,
            "statuses": self._statuses,
            "tasks": [t.to_dict() for t in self._tasks.values()],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self._path(), "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _load(self):
        path = self._path()
        try:
            with open(path) as f:
                data = json.load(f)
            for t_data in data.get("tasks", []):
                task = Task.from_dict(t_data)
                self._tasks[task.task_id] = task
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def __repr__(self):
        s = self.stats()
        return (
            f"<KanbanBoard '{self.board_name}' "
            f"{s['total']} tasks: "
            f"📋{s['by_status'].get('todo',0)} "
            f"🔄{s['by_status'].get('in_progress',0)} "
            f"✅{s['by_status'].get('done',0)}>"
        )
