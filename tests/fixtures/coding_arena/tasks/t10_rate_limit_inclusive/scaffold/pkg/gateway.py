from .ratelimit import RateLimiter

def make_gateway(limit=3):
    return RateLimiter(limit)
