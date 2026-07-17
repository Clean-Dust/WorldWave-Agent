def factorial(n):
    if n < 0:
        raise ValueError("n must be >= 0")
    if n in (0, 1):
        return n  # BUG: 0! should be 1
    return n * factorial(n - 1)
