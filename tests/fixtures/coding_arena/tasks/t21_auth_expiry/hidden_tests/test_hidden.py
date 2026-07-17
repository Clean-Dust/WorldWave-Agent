from pkg.auth import is_expired
from pkg.session import active

def test_before():
    assert is_expired(10, 9) is False

def test_exact():
    assert is_expired(10, 10) is True

def test_after():
    assert is_expired(10, 11) is True

def test_active():
    assert active(10, 10) is False
