from pkg.score import clamp

def test_clamp_high():
    assert clamp(150) == 100

def test_clamp_mid():
    assert clamp(50) == 50
