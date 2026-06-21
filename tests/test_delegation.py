"""Tests: Delegation module"""
import sys; sys.path.insert(0, ".")
from core.delegation import DelegationManager, ChildTask, MAX_CONCURRENT_CHILDREN


def test_max_concurrent_children():
    assert MAX_CONCURRENT_CHILDREN == 3


def test_childtask_creation():
    ct = ChildTask(task_id="test1", goal="Test task", context="Some context", max_spirals=2)
    assert ct.task_id == "test1"
    assert ct.goal == "Test task"
    assert ct.status == "pending"
    assert ct.max_spirals == 2


def test_childtask_to_dict():
    ct = ChildTask(task_id="test1", goal="Test task", context="Some context", max_spirals=2)
    d = ct.to_dict()
    assert d["task_id"] == "test1"
    assert d["status"] == "pending"


def test_childtask_update():
    ct = ChildTask(task_id="test1", goal="Test task", context="Some context", max_spirals=2)
    ct.status = "running"
    ct.spirals_used = 3
    d2 = ct.to_dict()
    assert d2["status"] == "running"
    assert d2["spirals_used"] == 3


def test_delegation_manager():
    dm = DelegationManager(max_concurrent=3)
    assert dm.max_concurrent == 3
    stats = dm.stats()
    assert stats["max_concurrent"] == 3


def test_empty_delegation():
    dm = DelegationManager(max_concurrent=3)
    results = dm.delegate([])
    assert results == []
