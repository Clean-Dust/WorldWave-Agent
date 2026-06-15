"""Tests: Delegation module"""
import sys; sys.path.insert(0, ".")
from core.delegation import DelegationManager, ChildTask, MAX_CONCURRENT_CHILDREN

assert MAX_CONCURRENT_CHILDREN == 3

# ChildTask creation
ct = ChildTask(task_id="test1", goal="Test task", context="Some context", max_spirals=2)
assert ct.task_id == "test1"
assert ct.goal == "Test task"
assert ct.status == "pending"
assert ct.max_spirals == 2

# ChildTask to dict
d = ct.to_dict()
assert d["task_id"] == "test1"
assert d["status"] == "pending"

# ChildTask update
ct.status = "running"
ct.spirals_used = 3
d2 = ct.to_dict()
assert d2["status"] == "running"
assert d2["spirals_used"] == 3

# DelegationManager construction
dm = DelegationManager(max_concurrent=3)
assert dm.max_concurrent == 3

# Stats
stats = dm.stats()
assert stats["max_concurrent"] == 3

# Empty delegation
results = dm.delegate([])
assert results == []

print("ALL DELEGATION TESTS PASSED")
