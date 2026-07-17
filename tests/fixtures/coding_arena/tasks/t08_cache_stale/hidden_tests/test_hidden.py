from pkg.cache import Store
from pkg.repo import UserRepo

def test_set_get():
    s = Store()
    s.set("a", 1)
    assert s.get("a") == 1
    s.set("a", 2)
    assert s.get("a") == 2

def test_repo():
    r = UserRepo()
    r.put("u1", "alice")
    assert r.get("u1") == "alice"
    r.put("u1", "bob")
    assert r.get("u1") == "bob"
