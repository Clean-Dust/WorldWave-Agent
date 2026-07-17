from pkg.echo import transform
from pkg.pipeline import run

def test_up():
    assert transform("AbC") == "ABC"

def test_run():
    assert run(["a", "B"]) == ["A", "B"]
