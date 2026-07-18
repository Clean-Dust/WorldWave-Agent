from pkg.bag import add_item
from pkg.store import once

def test_isolated():
    a = add_item("x")
    b = add_item("y")
    assert a == ["x"]
    assert b == ["y"]

def test_explicit():
    bag = []
    add_item("a", bag)
    add_item("b", bag)
    assert bag == ["a", "b"]

def test_store():
    assert once(1) == [1]
    assert once(2) == [2]
