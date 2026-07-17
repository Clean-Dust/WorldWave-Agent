class RateLimiter:
    def __init__(self, limit):
        self.limit = limit
        self.count = 0

    def allow(self):
        # BUG: allows one extra request
        if self.count > self.limit:
            return False
        self.count += 1
        return True

    def reset(self):
        self.count = 0
