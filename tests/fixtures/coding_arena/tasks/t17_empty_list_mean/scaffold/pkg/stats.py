def mean(xs):
    xs = list(xs)
    if not xs:
        return 0  # BUG: should raise
    return sum(xs) / len(xs)
