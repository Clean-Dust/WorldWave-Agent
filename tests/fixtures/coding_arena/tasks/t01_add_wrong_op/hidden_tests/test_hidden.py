from pkg.math_ops import add, mul
from pkg.service import compute

def test_add_basic():
    assert add(2, 3) == 5
    assert add(0, 0) == 0
    assert add(-1, 1) == 0

def test_service_add():
    assert compute(10, 5, "add") == 15

def test_mul_untouched():
    assert mul(3, 4) == 12
