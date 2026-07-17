import pytest
from pkg.recur import factorial
from pkg.combo import n_choose_k

def test_fact():
    assert factorial(0) == 1
    assert factorial(1) == 1
    assert factorial(5) == 120

def test_choose():
    assert n_choose_k(5, 2) == 10
    assert n_choose_k(0, 0) == 1

def test_neg():
    with pytest.raises(ValueError):
        factorial(-1)
