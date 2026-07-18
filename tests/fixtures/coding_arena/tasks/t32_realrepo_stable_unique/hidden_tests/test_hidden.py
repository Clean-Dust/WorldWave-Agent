from pkg.unique import stable_unique
from pkg.batch import batch_ids

def test_order():
    assert stable_unique([3, 1, 3, 2, 1]) == [3, 1, 2]

def test_empty():
    assert stable_unique([]) == []

def test_batch():
    assert batch_ids(["a", "b", "a"]) == ["a", "b"]
