def stable_unique(items):
    # BUG: uses set (unordered / wrong order)
    return list(set(items or []))
