"""
core/autonomous_scheduler.py — Autonomous Scheduling & Heartbeat Loop

Gives WW the ability to self-trigger tasks without external input.
Supports:
- Natural language cron scheduling ("every hour", "daily at 9am")
- Heartbeat-driven background task execution
- Idle detection → proactive task generation
- Task queue with priority, retry, and persistence

Config:
  WW_AUTONOMOUS_ENABLED = "true" (default: true)
  WW_HEARTBEAT_INTERVAL = 300 (seconds, default 5 min)
  WW_IDLE_TASK_THRESHOLD = 600 (seconds idle before proactive tasks)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("ww.autonomous_scheduler")

SCHED_DIR = os.path.expanduser("~/.ww/scheduler")
SCHED_DB = os.path.join(SCHED_DIR, "tasks.json")
HEARTBEAT_DB = os.path.join(SCHED_DIR, "heartbeat.json")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduleType(str, Enum):
    CRON = "cron"          # Recurring: "0 9 * * *"
    INTERVAL = "interval"   # Every N seconds: 3600
    HEARTBEAT = "heartbeat" # Run on each heartbeat tick
    IDLE = "idle"           # Run when system is idle
    ONE_SHOT = "one_shot"   # Run once at specific time


@dataclass
class AutonomousTask:
    """A scheduled task definition."""
    task_id: str
    name: str
    description: str = ""
    schedule_type: ScheduleType = ScheduleType.INTERVAL
    schedule_value: str = ""  # Cron expression or interval seconds
    goal: str = ""            # The goal string for the spiral loop
    enabled: bool = True
    priority: int = 5         # 0=highest, 10=lowest
    max_retries: int = 2
    retry_count: int = 0
    last_run_at: float = 0.0
    last_status: str = ""
    created_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "schedule_type": self.schedule_type.value,
            "schedule_value": self.schedule_value,
            "goal": self.goal,
            "enabled": self.enabled,
            "priority": self.priority,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "created_at": self.created_at,
            "tags": self.tags,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutonomousTask":
        return cls(
            task_id=d.get("task_id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            schedule_type=ScheduleType(d.get("schedule_type", "interval")),
            schedule_value=d.get("schedule_value", ""),
            goal=d.get("goal", ""),
            enabled=d.get("enabled", True),
            priority=d.get("priority", 5),
            max_retries=d.get("max_retries", 2),
            retry_count=d.get("retry_count", 0),
            last_run_at=d.get("last_run_at", 0.0),
            last_status=d.get("last_status", ""),
            created_at=d.get("created_at", time.time()),
            tags=d.get("tags", []),
            context=d.get("context", {}),
        )


class AutonomousScheduler:
    """Background heartbeat loop + task scheduling engine.

    Runs in a daemon thread. On each tick:
    1. Checks which scheduled tasks are due
    2. Executes them via callback (ww.run)
    3. Persists task state

    Also supports NL-based task creation: "run every 30 minutes" → schedule.
    """

    def __init__(
        self,
        run_callback: Optional[Callable] = None,
        heartbeat_interval: int = 300,
        enabled: bool = True,
        idle_threshold: int = 600,
    ):
        self._run_callback = run_callback
        self.heartbeat_interval = heartbeat_interval
        self.enabled = enabled
        self.idle_threshold = idle_threshold
        self._tasks: Dict[str, AutonomousTask] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_interaction_time = time.time()
        self._heartbeat_count = 0
        self._loaded = False

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self):
        """Start the heartbeat daemon thread."""
        if not self.enabled:
            log.info("Autonomous scheduler disabled")
            return
        if self._running:
            return
        self.ensure_loaded()
        self._running = True
        self._thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="ww-autonomous-scheduler"
        )
        self._thread.start()
        log.info(f"Autonomous scheduler started (heartbeat={self.heartbeat_interval}s)")

    def stop(self):
        """Stop the heartbeat loop."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        log.info("Autonomous scheduler stopped")

    def touch(self):
        """Signal user interaction — resets idle timer."""
        self._last_interaction_time = time.time()

    # ── Persistence ───────────────────────────────────────────

    def ensure_loaded(self):
        if self._loaded:
            return
        os.makedirs(SCHED_DIR, exist_ok=True)
        if os.path.exists(SCHED_DB):
            try:
                with open(SCHED_DB, "r") as f:
                    data = json.load(f)
                for tdata in data.get("tasks", []):
                    task = AutonomousTask.from_dict(tdata)
                    self._tasks[task.task_id] = task
            except Exception as e:
                log.warning(f"Scheduler DB load failed: {e}")
        self._loaded = True
        self._save()

    def _save(self):
        os.makedirs(SCHED_DIR, exist_ok=True)
        data = {
            "tasks": [t.to_dict() for t in self._tasks.values()],
            "last_saved": datetime.now(timezone.utc).isoformat(),
        }
        with open(SCHED_DB, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ── Heartbeat Loop ────────────────────────────────────────

    def _heartbeat_loop(self):
        """Main daemon loop — check and execute due tasks."""
        while self._running:
            try:
                self._heartbeat_count += 1
                self._tick()
            except Exception as e:
                log.error(f"Heartbeat error: {e}")
            time.sleep(self.heartbeat_interval)

    def _tick(self):
        """Single heartbeat tick."""
        now = time.time()
        due_tasks = []

        with self._lock:
            for task in self._tasks.values():
                if not task.enabled:
                    continue
                if self._is_due(task, now):
                    due_tasks.append(task)

        # Sort by priority
        due_tasks.sort(key=lambda t: t.priority)

        # Execute up to 3 per tick
        for task in due_tasks[:3]:
            self._execute_task(task)

        # Check idle → proactive task generation
        idle_for = now - self._last_interaction_time
        if idle_for > self.idle_threshold and self._heartbeat_count % 12 == 0:  # ~hourly
            self._generate_idle_task()

        self._save()

    def _is_due(self, task: AutonomousTask, now: float) -> bool:
        """Check if a task should run now."""
        st = task.schedule_type

        if st == ScheduleType.ONE_SHOT:
            # schedule_value is ISO timestamp
            try:
                due_at = datetime.fromisoformat(task.schedule_value).timestamp()
                return now >= due_at and task.last_status != "completed"
            except Exception:
                return False

        if st == ScheduleType.HEARTBEAT:
            return True  # Every tick

        if st == ScheduleType.INTERVAL:
            try:
                interval = int(task.schedule_value)
            except (ValueError, TypeError):
                interval = self.heartbeat_interval
            return (now - task.last_run_at) >= interval

        if st == ScheduleType.CRON:
            return self._cron_matches(task.schedule_value, now)

        return False

    def _cron_matches(self, expr: str, now: float) -> bool:
        """Simple cron expression matching (minute hour day month weekday)."""
        try:
            dt = datetime.fromtimestamp(now)
            parts = expr.strip().split()
            if len(parts) != 5:
                return False

            minute, hour, day, month, weekday = parts
            current = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
            patterns = [minute, hour, day, month, weekday]

            for pat, cur in zip(patterns, current):
                if pat == "*":
                    continue
                if "," in pat:
                    values = [int(x) for x in pat.split(",")]
                    if cur not in values:
                        return False
                elif "/" in pat:
                    base, _, step = pat.partition("/")
                    step = int(step)
                    if base == "*":
                        base_val = 0
                    else:
                        base_val = int(base)
                    if (cur - base_val) % step != 0:
                        return False
                else:
                    if int(pat) != cur:
                        return False
            return True
        except Exception:
            return False

    # ── Task Execution ────────────────────────────────────────

    def _execute_task(self, task: AutonomousTask):
        """Execute a scheduled task via the spiral loop callback."""
        task.last_run_at = time.time()
        task.last_status = "running"

        if not self._run_callback:
            log.warning(f"No run callback set — skipping: {task.name}")
            task.last_status = "failed"
            return

        try:
            result = self._run_callback(task.goal)
            success = result.get("status") == "completed" if isinstance(result, dict) else False

            if success:
                task.last_status = "completed"
                task.retry_count = 0
            else:
                task.last_status = "failed"
                task.retry_count += 1
                if task.retry_count >= task.max_retries:
                    log.warning(f"Task {task.name} exceeded max retries, disabling")
                    task.enabled = False
        except Exception as e:
            task.last_status = "failed"
            task.retry_count += 1
            log.error(f"Task {task.name} execution error: {e}")

    def _generate_idle_task(self):
        """During idle periods, generate a proactive maintenance task."""
        # Don't spam — check if there's already a pending idle task
        existing = [t for t in self._tasks.values()
                    if t.schedule_type == ScheduleType.IDLE and t.last_status != "completed"]
        if existing:
            return

        task = AutonomousTask(
            task_id="idle-" + uuid.uuid4().hex[:6],
            name="Proactive Maintenance",
            description="Auto-generated during idle period",
            schedule_type=ScheduleType.IDLE,
            goal="Perform system maintenance: check for stale data, "
                 "consolidate memories, optimize indices, report status",
            priority=8,
        )
        self._tasks[task.task_id] = task
        log.info("🌙 Generated idle maintenance task")

    # ── Public API ────────────────────────────────────────────

    def add_task(self, name: str, goal: str, schedule: str = "1h",
                 priority: int = 5, tags: List[str] = None) -> str:
        """Add a scheduled task with natural-language schedule.

        Args:
            name: Human-readable name
            goal: The goal for the spiral loop to execute
            schedule: NL schedule like "30m", "every 2h", "daily at 9am",
                      or cron "0 9 * * *", or "heartbeat"
            priority: 0-10, lower = higher priority
            tags: Optional categorization tags

        Returns:
            task_id
        """
        self.ensure_loaded()

        schedule_type, schedule_value = self._parse_schedule(schedule)

        task = AutonomousTask(
            task_id=uuid.uuid4().hex[:8],
            name=name,
            goal=goal,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            priority=priority,
            tags=tags or [],
        )
        with self._lock:
            self._tasks[task.task_id] = task
        self._save()
        log.info(f"📅 Scheduled: {name} ({schedule})")
        return task.task_id

    def remove_task(self, task_id: str) -> bool:
        """Remove a scheduled task."""
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                self._save()
                return True
        return False

    def toggle_task(self, task_id: str, enabled: bool = None) -> bool:
        """Enable/disable a task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.enabled = enabled if enabled is not None else not task.enabled
            self._save()
            return True

    def list_tasks(self) -> List[Dict]:
        """List all scheduled tasks."""
        self.ensure_loaded()
        return [
            {
                "task_id": t.task_id,
                "name": t.name,
                "goal": t.goal[:80],
                "schedule": f"{t.schedule_type.value}:{t.schedule_value}",
                "enabled": t.enabled,
                "priority": t.priority,
                "last_status": t.last_status,
                "last_run": datetime.fromtimestamp(t.last_run_at).isoformat() if t.last_run_at else "never",
                "tags": t.tags,
            }
            for t in sorted(self._tasks.values(), key=lambda t: t.priority)
        ]

    def run_now(self, task_id: str) -> bool:
        """Force immediate execution of a task."""
        self.ensure_loaded()
        task = self._tasks.get(task_id)
        if not task:
            return False
        self._execute_task(task)
        self._save()
        return True

    def _parse_schedule(self, schedule: str) -> tuple:
        """Parse natural-language schedule into (ScheduleType, value)."""
        s = schedule.strip().lower()

        # Heartbeat
        if s in ("heartbeat", "every tick", "each tick"):
            return ScheduleType.HEARTBEAT, "0"

        # Cron expression
        if re.match(r'^[\d\*,/]+\s+[\d\*,/]+\s+[\d\*,/]+\s+[\d\*,/]+\s+[\d\*,/]+$', s):
            return ScheduleType.CRON, s

        # One-shot timestamp
        if re.match(r'^\d{4}-\d{2}-\d{2}', s):
            return ScheduleType.ONE_SHOT, s

        # Interval: "30m", "every 2h", "every 30 minutes"
        interval_match = re.match(r'(?:every\s+)?(\d+)\s*(s|sec|second|m|min|minute|h|hour|d|day)s?\.?', s)
        if interval_match:
            val = int(interval_match.group(1))
            unit = interval_match.group(2)[0]
            if unit == 's':
                seconds = val
            elif unit == 'm':
                seconds = val * 60
            elif unit == 'h':
                seconds = val * 3600
            elif unit == 'd':
                seconds = val * 86400
            else:
                seconds = val * 60
            return ScheduleType.INTERVAL, str(seconds)

        # "daily at 9am" → cron "0 9 * * *"
        daily_match = re.match(r'daily\s+(?:at\s+)?(\d{1,2})\s*(am|pm)?', s)
        if daily_match:
            hour = int(daily_match.group(1))
            if daily_match.group(2) == "pm" and hour != 12:
                hour += 12
            return ScheduleType.CRON, f"0 {hour} * * *"

        # "hourly" → every hour
        if s == "hourly":
            return ScheduleType.INTERVAL, "3600"

        # Default: every 5 minutes
        return ScheduleType.INTERVAL, "300"

    def stats(self) -> Dict:
        """Scheduler statistics."""
        self.ensure_loaded()
        tasks = list(self._tasks.values())
        return {
            "total_tasks": len(tasks),
            "enabled_tasks": sum(1 for t in tasks if t.enabled),
            "active_by_type": {
                st.value: sum(1 for t in tasks if t.schedule_type == st)
                for st in ScheduleType
            },
            "heartbeat_count": self._heartbeat_count,
            "idle_seconds": time.time() - self._last_interaction_time,
            "last_interaction": datetime.fromtimestamp(self._last_interaction_time).isoformat(),
        }
