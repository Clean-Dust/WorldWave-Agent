def merge_json(base, override):
    # BUG: shallow only
    out = dict(base or {})
    out.update(override or {})
    return out
