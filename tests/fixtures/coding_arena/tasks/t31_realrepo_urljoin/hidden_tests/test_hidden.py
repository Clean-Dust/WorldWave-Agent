from pkg.urls import join_url
from pkg.client import endpoint

def test_basic():
    assert join_url("http://x.com", "a") == "http://x.com/a"

def test_slash_base():
    assert join_url("http://x.com/", "/a") == "http://x.com/a"

def test_empty_path():
    assert join_url("http://x.com", "") == "http://x.com/"

def test_client():
    assert endpoint("http://api/", "v1") == "http://api/v1"
