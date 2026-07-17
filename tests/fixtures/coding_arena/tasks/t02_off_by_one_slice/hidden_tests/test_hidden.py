from pkg.window import take_first_n
from pkg.api import preview

def test_take_n():
    assert take_first_n([1, 2, 3, 4], 3) == [1, 2, 3]
    assert take_first_n([1], 1) == [1]
    assert take_first_n([], 5) == []

def test_preview():
    assert preview(list(range(10)), 2) == [0, 1]
