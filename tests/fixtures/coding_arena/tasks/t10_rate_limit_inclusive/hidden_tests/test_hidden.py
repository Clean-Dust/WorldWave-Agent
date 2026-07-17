from pkg.ratelimit import RateLimiter
from pkg.gateway import make_gateway

def test_limit():
    rl = RateLimiter(3)
    assert rl.allow() and rl.allow() and rl.allow()
    assert rl.allow() is False

def test_gateway():
    g = make_gateway(1)
    assert g.allow() is True
    assert g.allow() is False
