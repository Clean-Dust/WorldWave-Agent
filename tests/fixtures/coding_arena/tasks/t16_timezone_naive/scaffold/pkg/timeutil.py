def to_epoch(dt):
    # BUG: uses naive timestamp without forcing UTC
    return int(dt.timestamp()) if dt.tzinfo else int(dt.replace(tzinfo=None).timestamp())
