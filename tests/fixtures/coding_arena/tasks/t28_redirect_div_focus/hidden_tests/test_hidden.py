import pytest
from pkg.ops import sub, div
from pkg.calc import diff, quot

def test_sub():
    assert sub(5, 2) == 3

def test_div():
    assert div(7, 2) == 3

def test_div_zero():
    with pytest.raises(ZeroDivisionError):
        div(1, 0)

def test_wrappers():
    assert diff(9, 4) == 5
    assert quot(8, 3) == 2
