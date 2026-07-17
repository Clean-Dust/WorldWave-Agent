from pkg.geo import near
from pkg.nav import same_point

def test_near():
    assert near(1.0, 1.0 + 1e-9) is True
    assert near(1.0, 2.0) is False

def test_same_point():
    assert same_point((0.0, 0.0), (1e-9, -1e-9)) is True
