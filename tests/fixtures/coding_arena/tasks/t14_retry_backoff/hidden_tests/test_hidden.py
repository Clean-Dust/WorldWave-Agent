from pkg.retry import next_delay
from pkg.client import delays

def test_exp():
    assert next_delay(0, 0.1, 10) == 0.1
    assert next_delay(1, 0.1, 10) == 0.2
    assert next_delay(2, 0.1, 10) == 0.4

def test_cap():
    assert next_delay(10, 0.5, 1.0) == 1.0

def test_list():
    assert delays(3, 1.0, 100) == [1.0, 2.0, 4.0]
