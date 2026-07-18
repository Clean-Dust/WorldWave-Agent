import os
import pytest
from pkg.paths import resolve_under
from pkg.assets import asset_path

def test_ok(tmp_path):
    p = resolve_under(str(tmp_path), "a", "b.txt")
    assert p.startswith(str(tmp_path))

def test_dotdot(tmp_path):
    with pytest.raises(ValueError):
        resolve_under(str(tmp_path), "..", "etc")

def test_abs(tmp_path):
    with pytest.raises(ValueError):
        resolve_under(str(tmp_path), "/etc/passwd")

def test_asset(tmp_path):
    p = asset_path(str(tmp_path), "x.css")
    assert "static" in p
