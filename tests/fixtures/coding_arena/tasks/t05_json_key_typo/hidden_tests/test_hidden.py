from pkg.api_client import parse_user
from pkg.session import login_payload

def test_parse():
    u = parse_user({"id": 7, "username": "bob"})
    assert u["username"] == "bob"
    assert u["id"] == 7

def test_login():
    assert login_payload({"id": 1, "username": "alice"}) == "alice"
