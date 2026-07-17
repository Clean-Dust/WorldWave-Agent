from pkg.textutil import normalize_name
from pkg.users import register

def test_normal():
    assert normalize_name("  Alice ") == "alice"

def test_none():
    assert normalize_name(None) == ""

def test_empty():
    assert normalize_name("   ") == ""

def test_register_none():
    r = register(None)
    assert r["ok"] is False
    assert r["key"] == ""
