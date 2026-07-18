from pkg.jmerge import merge_json
from pkg.loader import load_pair

def test_deep():
    a = {"x": {"y": 1, "z": 2}}
    b = {"x": {"y": 9}}
    r = merge_json(a, b)
    assert r["x"]["y"] == 9
    assert r["x"]["z"] == 2

def test_list_replace():
    assert merge_json({"a": [1, 2]}, {"a": [3]})["a"] == [3]

def test_loader():
    assert load_pair({"k": {"a": 1}}, {"k": {"b": 2}})["k"]["a"] == 1
