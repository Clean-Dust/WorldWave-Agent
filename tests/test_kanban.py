"""Tests: KanbanBoard module"""
import sys; sys.path.insert(0, ".")
import os
import shutil
from core.kanban import KanbanBoard, Task


def _cleanup():
    shutil.rmtree("/tmp/test_kanban", ignore_errors=True)


def test_add_tasks():
    _cleanup()
    os.makedirs("/tmp/test_kanban", exist_ok=True)
    kb = KanbanBoard(data_dir="/tmp/test_kanban", board_name="test")
    t1 = kb.add("Task 1", assignee="alice", priority=3, category="dev")
    t2 = kb.add("Task 2", assignee="bob", priority=1, category="dev")
    t3 = kb.add("Task 3", assignee="alice", priority=5, category="ops")
    assert t1.task_id
    assert t2.task_id
    assert t3.task_id
    assert kb.get(t1.task_id).title == "Task 1"


def test_status_transitions():
    _cleanup()
    os.makedirs("/tmp/test_kanban", exist_ok=True)
    kb = KanbanBoard(data_dir="/tmp/test_kanban", board_name="test")
    t1 = kb.add("Task 1", assignee="alice", priority=3, category="dev")
    t2 = kb.add("Task 2", assignee="bob", priority=1, category="dev")
    t3 = kb.add("Task 3", assignee="alice", priority=5, category="ops")

    assert kb.start(t1.task_id)
    assert kb.get(t1.task_id).status == "in_progress"

    assert kb.block(t2.task_id, "Waiting for dependency")
    assert kb.get(t2.task_id).status == "blocked"
    assert len(kb.get(t2.task_id).notes) >= 1

    assert kb.complete(t3.task_id)
    assert kb.get(t3.task_id).status == "done"
    assert kb.get(t3.task_id).completed_at is not None


def test_query():
    _cleanup()
    os.makedirs("/tmp/test_kanban", exist_ok=True)
    kb = KanbanBoard(data_dir="/tmp/test_kanban", board_name="test")
    kb.add("Task 1", assignee="alice", priority=3, category="dev")
    kb.add("Task 2", assignee="bob", priority=1, category="dev")
    kb.add("Task 3", assignee="alice", priority=5, category="ops")

    alice_tasks = kb.list(assignee="alice")
    assert len(alice_tasks) == 2

    dev_tasks = kb.list(category="dev")
    assert len(dev_tasks) == 2

    # Complete the first alice task (list returns dicts)
    first_id = alice_tasks[0].get("task_id") or alice_tasks[0].get("id")
    if first_id:
        kb.complete(first_id)
    done_tasks = kb.list(status="done")
    assert len(done_tasks) >= 1


def test_stats():
    _cleanup()
    os.makedirs("/tmp/test_kanban", exist_ok=True)
    kb = KanbanBoard(data_dir="/tmp/test_kanban", board_name="test")
    kb.add("Task 1", assignee="alice", priority=3, category="dev")
    kb.add("Task 2", assignee="bob", priority=1, category="dev")
    kb.add("Task 3", assignee="alice", priority=5, category="ops")

    stats = kb.stats()
    assert stats["total"] == 3
    assert stats["by_status"]["todo"] >= 1
    assert stats["completion_rate"] >= 0


def test_add_note_and_update():
    _cleanup()
    os.makedirs("/tmp/test_kanban", exist_ok=True)
    kb = KanbanBoard(data_dir="/tmp/test_kanban", board_name="test")
    t1 = kb.add("Task 1", assignee="alice", priority=3, category="dev")

    kb.add_note(t1.task_id, "Progress update")
    assert len(kb.get(t1.task_id).notes) >= 1

    kb.update(t1.task_id, title="Task 1 (updated)")
    assert kb.get(t1.task_id).title == "Task 1 (updated)"


def test_delete():
    _cleanup()
    os.makedirs("/tmp/test_kanban", exist_ok=True)
    kb = KanbanBoard(data_dir="/tmp/test_kanban", board_name="test")
    t1 = kb.add("Task 1", assignee="alice", priority=3, category="dev")
    t2 = kb.add("Task 2", assignee="bob", priority=1)

    kb.delete(t2.task_id)
    assert kb.get(t2.task_id) is None
    assert kb.stats()["total"] == 1


def test_task_serialization():
    task_obj = Task(title="Direct task", description="Created directly")
    task_dict = task_obj.to_dict()
    assert task_dict["title"] == "Direct task"

    restored = Task.from_dict(task_dict)
    assert restored.title == "Direct task"
    assert restored.status == "todo"
