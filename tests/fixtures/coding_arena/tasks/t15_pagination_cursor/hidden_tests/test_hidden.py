from pkg.page import slice_after
from pkg.feed import next_page

def test_first():
    assert slice_after([1, 2, 3, 4], None, 2) == [1, 2]

def test_after():
    assert slice_after([1, 2, 3, 4], 2, 2) == [3, 4]

def test_feed():
    assert next_page(["a", "b", "c"], "a", 2) == ["b", "c"]
