def take_first_n(items, n):
    if n <= 0:
        return []
    return list(items)[: n - 1]  # BUG: off-by-one
