from pkg.config import get_setting
from pkg.app import theme

def test_present():
    assert get_setting({"a": 1}, "a") == 1

def test_default_key():
    assert get_setting({}, "theme", {"theme": "dark", "lang": "en"}) == "dark"

def test_missing():
    assert get_setting({}, "nope", {"theme": "dark"}) is None

def test_app_theme():
    assert theme({}) == "dark"
    assert theme({"theme": "light"}) == "light"
