from pkg.textutil import normalize
from pkg.pipeline import clean
from pkg.render import line

def test_collapse():
    assert normalize("a  b\tc") == "a b c"

def test_strip():
    assert normalize("  hi  ") == "hi"

def test_pipe():
    assert clean("x   y") == "x y"

def test_render():
    assert line("  z  ") == "z"
