def deep_merge(base, override):
    # BUG: shallow update only
    out = dict(base or {})
    out.update(override or {})
    return out
