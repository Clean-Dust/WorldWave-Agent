"""Tests: KanbanBoard module"""
import sys; sys.path.insert(0, ".")
import os, shutil
from core.kanban import KanbanBoard, Task

os.makedirs("/tmp/test_kanban", exist_ok=True)
kb = KanbanBoard(data_dir="/tmp/test_kanban", board_name="test")

# Add tasks
t1 = kb.add("Task 1", assignee="alice", priority=3, category="dev")
t2 = kb.add("Task 2", assignee="bob", priority=1, category="dev")
t3 = kb.add("Task 3", assignee="alice", priority=5, category="ops")

assert t1.task_id
assert t2.task_id
assert t3.task_id
assert kb.get(t1.task_id).title == "Task 1"

# Status transitions
assert kb.start(t1.task_id)
assert kb.get(t1.task_id).status == "in_progress"

assert kb.block(t2.task_id, "Waiting for dependency")
assert kb.get(t2.task_id).status == "blocked"
assert len(kb.get(t2.task_id).notes) >= 1

assert kb.complete(t3.task_id)
assert kb.get(t3.task_id).status == "done"
assert kb.get(t3.task_id).completed_at is not None

# Query
alice_tasks = kb.list(assignee="alice")
assert len(alice_tasks) == 2

dev_tasks = kb.list(category="dev")
assert len(dev_tasks) == 2

done_tasks = kb.list(status="done")
assert len(done_tasks) >= 1

# Stats
stats = kb.stats()
assert stats["total"] == 3
assert stats["by_status"]["done"] >= 1
assert stats["completion_rate"] > 0

# Add note
kb.add_note(t1.task_id, "Progress update")
assert len(kb.get(t1.task_id).notes) >= 1

# Update
kb.update(t1.task_id, title="Task 1 (updated)")
assert kb.get(t1.task_id).title == "Task 1 (updated)"

# Delete
kb.delete(t2.task_id)
assert kb.get(t2.task_id) is None
assert kb.stats()["total"] == 2

# Task creation from dict
from core.kanban import Task
task_obj = Task(title="Direct task", description="Created directly")
task_dict = task_obj.to_dict()
assert task_dict["title"] == "Direct task"

# Task from dict
restored = Task.from_dict(task_dict)
assert restored.title == "Direct task"
assert restored.status == "todo"

# Cleanup
shutil.rmtree("/tmp/test_kanban", ignore_errors=True)

print("ALL KANBAN TESTS PASSED")
