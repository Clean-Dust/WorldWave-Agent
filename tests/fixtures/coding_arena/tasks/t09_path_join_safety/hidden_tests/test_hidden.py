import os
import pytest
from pkg.files import safe_join
from pkg.static import resolve_asset

def test_ok(tmp_path):
    p = safe_join(str(tmp_path), "a.txt")
    assert p.startswith(str(tmp_path))

def test_escape(tmp_path):
    with pytest.raises(ValueError):
        safe_join(str(tmp_path), "..", "etc", "passwd")

def test_asset(tmp_path):
    p = resolve_asset(str(tmp_path), "x.css")
    assert "assets" in p
