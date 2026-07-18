from pkg.score import clamp
from pkg.report import fmt

def test_low():
    assert clamp(-5) == 0

def test_none():
    assert clamp(None) == 0

def test_high():
    assert clamp(200) == 100

def test_fmt():
    assert "score=0" in fmt(-1)
