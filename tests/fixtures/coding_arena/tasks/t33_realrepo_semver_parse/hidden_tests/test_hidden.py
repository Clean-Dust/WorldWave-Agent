import pytest
from pkg.semver import parse
from pkg.compat import is_at_least

def test_basic():
    assert parse("1.2.3") == (1, 2, 3)

def test_prerelease():
    assert parse("2.0.0-rc1") == (2, 0, 0)

def test_bad():
    with pytest.raises(ValueError):
        parse("1.2")

def test_compat():
    assert is_at_least("3.1.0", 3) is True
    assert is_at_least("2.9.9", 3) is False
