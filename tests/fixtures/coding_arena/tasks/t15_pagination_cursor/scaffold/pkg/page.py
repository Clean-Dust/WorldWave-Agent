def slice_after(items, cursor=None, limit=10):
    items = list(items)
    if cursor is None:
        return items[:limit]
    try:
        idx = items.index(cursor)
    except ValueError:
        return []
    # BUG: inclusive of cursor
    return items[idx : idx + limit]
