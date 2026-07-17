from .page import slice_after

def next_page(items, cursor, limit=2):
    return slice_after(items, cursor, limit)
