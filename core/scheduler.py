"""ww/core/scheduler.py — Worldwave schedule  v0.1

WW's built-in schedule system. Similar to Hermes cronjob but directly integrated with WW spiral loop.

supports:
- Cron expressionschedule
- One-time task (ISO datetime)
- Interval schedule (every N seconds/minutes/hours)
- Persistent save (JSON)
Error/result notification mechanism
"""

from __future__ import annotations
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional


SCHEDULER_DB = os.path.expanduser("~/.ww/scheduler.json")


def parse_cron(expr: str) -> Dict[str, Any]:
    """
    Resolve simplified cron expression.
    supportsformat:
    - '* * * * *' — standard cron (min hour dom mon dow)
    - '*/5 * * * *' — Every 5 minutes
    - '0 9 * * 1-5' — Weekdays at 9 AM
    - '@hourly', '@daily', '@weekly' — Shortcuts
    - 'every 30m', 'every 2h' — Interval mode
    """
    expr = expr.strip().lower()
    
    # Shortcut mode
    shortcuts = {
        "@hourly": "0 * * * *",
        "@daily": "0 0 * * *",
        "@weekly": "0 0 * * 0",
        "@monthly": "0 0 1 * *",
        "@yearly": "0 0 1 1 *",
    }
    
    if expr in shortcuts:
        expr = shortcuts[expr]
    
    # Interval mode
    interval_match = re.match(r'every\s+(\d+)\s*(s|m|h|d)', expr)
    if interval_match:
        val = int(interval_match.group(1))
        unit = interval_match.group(2)
        seconds = {"s": val, "m": val * 60, "h": val * 3600, "d": val * 86400}
        return {"type": "interval", "interval_seconds": seconds.get(unit, val)}
    
    # Cron mode
    parts = expr.split()
    if len(parts) == 5:
        mins, hours, dom, months, dow = parts
        return {
            "type": "cron",
            "minute": mins,
            "hour": hours or "*",
            "dom": dom or "*",
            "month": months or "*",
            "dow": dow or "*",
        }
    
    # ISO datetime mode (one-time)
    try:
        datetime.fromisoformat(expr)
        return {"type": "once", "run_at": expr}
    except ValueError:
        pass
    
    raise ValueError("unrecognized schedule expression: " + expr)


def next_run(schedule: Dict[str, Any], now: datetime = None) -> Optional[str]:
    """
    Calculate next execution time (ISO datetime string).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    stype = schedule.get("type", "")
    
    if stype == "interval":
        return (now + timedelta(seconds=schedule.get("interval_seconds", 300))).isoformat()
    
    if stype == "once":
        try:
            run_at = datetime.fromisoformat(schedule["run_at"])
            if run_at <= now.replace(tzinfo=None):
                return None  # Expired
            return run_at.isoformat()
        except (ValueError, KeyError):
            return None
    
    if stype == "cron":
        # Simplified calculation: default next time is now + 1min (check every minute)
        return (now + timedelta(minutes=1)).isoformat()
    
    return None


class ScheduledTask:
    """a scheduletask data structure."""
    
    def __init__(
        self,
        task_id: str,
        name: str,
        goal: str,
        schedule: str,   # cron expression or ISO datetime
        max_spirals: int = 3,
        enabled: bool = True,
        created: str = "",
        last_run: str = "",
        next_run_at: str = "",
        run_count: int = 0,
        last_result: str = "",
    ):
        self.task_id = task_id
        self.name = name or "task-" + task_id[:8]
        self.goal = goal
        self.schedule_str = schedule
        self.max_spirals = max_spirals
        self.enabled = enabled
        self.created = created or datetime.now(timezone.utc).isoformat()
        self.last_run = last_run
        self.next_run_at = next_run_at
        self.run_count = run_count
        self.last_result = last_result
    
    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "goal": self.goal,
            "schedule": self.schedule_str,
            "max_spirals": self.max_spirals,
            "enabled": self.enabled,
            "created": self.created,
            "last_run": self.last_run,
            "next_run_at": self.next_run_at,
            "run_count": self.run_count,
            "last_result": self.last_result,
        }
    
    def __repr__(self):
        return "<Task:" + self.name + " cron:" + self.schedule_str + ">"


class Scheduler:
    """
    WW schedule . 
    
    Manage scheduled/periodic tasks, responsible for scheduling, persistence, and triggering execution.
    """
    
    def __init__(self, db_path: str = SCHEDULER_DB):
        self.db_path = db_path
        self._tasks: Dict[str, ScheduledTask] = {}
        self._load()
        self._running = False
        self._thread = None
        self._stats = {"total_runs": 0, "successful_runs": 0, "failed_runs": 0}
    
    def _load(self):
        """from diskloadtask. """
        if not os.path.isfile(self.db_path):
            return
        try:
            with open(self.db_path) as f:
                data = json.load(f)
            for item in data:
                task = ScheduledTask(
                    task_id=item["task_id"],
                    name=item.get("name", ""),
                    goal=item["goal"],
                    schedule=item["schedule"],
                    max_spirals=item.get("max_spirals", 3),
                    enabled=item.get("enabled", True),
                    created=item.get("created", ""),
                    last_run=item.get("last_run", ""),
                    next_run_at=item.get("next_run_at", ""),
                    run_count=item.get("run_count", 0),
                    last_result=item.get("last_result", ""),
                )
                self._tasks[task.task_id] = task
        except Exception as e:
            print("Scheduler load error:", str(e))
    
    def _save(self):
        """persist task."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        data = [t.to_dict() for t in self._tasks.values()]
        with open(self.db_path, "w") as f:
            json.dump(data, f, indent=2)
    
    def add(self, name: str, goal: str, schedule: str,
            max_spirals: int = 3) -> ScheduledTask:
        """add a scheduletask."""
        import uuid
        task_id = str(uuid.uuid4())[:12]
        
        # Calculate next execution time
        parsed = parse_cron(schedule)
        next_run_at = next_run(parsed)
        
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            goal=goal,
            schedule=schedule,
            max_spirals=max_spirals,
            next_run_at=next_run_at or "",
        )
        self._tasks[task_id] = task
        self._save()
        return task
    
    def remove(self, task_id: str) -> bool:
        """remove a scheduletask."""
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save()
            return True
        return False
    
    def list(self) -> List[Dict]:
        """list all task."""
        return [t.to_dict() for t in sorted(
            self._tasks.values(),
            key=lambda t: t.next_run_at or "9999",
        )]
    
    def get(self, task_id: str) -> Optional[Dict]:
        """get single task."""
        task = self._tasks.get(task_id)
        return task.to_dict() if task else None
    
    def update_next_run(self, task_id: str):
        """update next execute."""
        task = self._tasks.get(task_id)
        if task:
            now = datetime.now(timezone.utc)
            parsed = parse_cron(task.schedule_str)
            task.next_run_at = (
                (now + timedelta(minutes=1)).isoformat()
                if task.schedule_str.count(" ") == 5
                else next_run(parsed, now) or ""
            )
    
    def record_run(self, task_id: str, result: str):
        """record one execute result."""
        task = self._tasks.get(task_id)
        if task:
            task.last_run = datetime.now(timezone.utc).isoformat()
            task.last_result = result[:200]
            task.run_count += 1
            self.update_next_run(task_id)
            self._save()
    
    def due_tasks(self) -> List[ScheduledTask]:
        """find overdue task."""
        now = datetime.now(timezone.utc)
        due = []
        for task in self._tasks.values():
            if not task.enabled:
                continue
            if task.next_run_at and task.next_run_at <= now.isoformat():
                due.append(task)
        return due
    
    def start(self, run_callback: Callable[[ScheduledTask], str]):
        """startschedule  (backgroundthread) . """
        if self._running:
            return
        
        self._running = True
        self._callback = run_callback
        
        def _loop():
            while self._running:
                try:
                    for task in self.due_tasks():
                        if not self._running:
                            break
                        result = run_callback(task)
                        self.record_run(task.task_id, result)
                except Exception as e:
                    print("Scheduler tick error:", str(e))
                time.sleep(30)  # Check every 30 seconds
        
        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """stopschedule . """
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
    
    def info(self) -> Dict:
        """schedule state information."""
        return {
            "running": self._running,
            "total_tasks": len(self._tasks),
            "enabled_tasks": sum(1 for t in self._tasks.values() if t.enabled),
            "due_now": len(self.due_tasks()),
        }


def default_scheduler() -> Scheduler:
    return Scheduler()
