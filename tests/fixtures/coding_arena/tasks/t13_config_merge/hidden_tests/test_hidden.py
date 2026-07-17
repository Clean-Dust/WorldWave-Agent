from pkg.merge import deep_merge
from pkg.settings import load_settings

def test_deep():
    r = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}})
    assert r == {"a": {"x": 1, "y": 9}}

def test_settings():
    s = load_settings({"db": {"port": 1}, "debug": True})
    assert s["db"]["host"] == "localhost"
    assert s["db"]["port"] == 1
    assert s["debug"] is True
