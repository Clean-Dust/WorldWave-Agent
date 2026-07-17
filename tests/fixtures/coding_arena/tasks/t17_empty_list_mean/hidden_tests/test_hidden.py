import pytest
from pkg.stats import mean
from pkg.report import avg_or_none

def test_mean():
    assert mean([2, 4]) == 3

def test_empty():
    with pytest.raises(ValueError):
        mean([])

def test_report():
    assert avg_or_none([]) is None
    assert avg_or_none([1, 3]) == 2
