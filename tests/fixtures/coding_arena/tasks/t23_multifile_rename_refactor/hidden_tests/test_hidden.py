from pkg.pricing import apply_discount
from pkg.cart import line_total
from pkg.checkout import total

def test_half():
    assert abs(apply_discount(100, 50) - 50.0) < 1e-9

def test_ten():
    assert abs(apply_discount(200, 10) - 180.0) < 1e-9

def test_cart():
    assert abs(line_total(80, 25) - 60.0) < 1e-9

def test_checkout():
    assert abs(total(40, 0) - 40.0) < 1e-9
