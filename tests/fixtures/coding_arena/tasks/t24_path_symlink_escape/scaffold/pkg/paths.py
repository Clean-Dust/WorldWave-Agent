import os

def resolve_under(root, *parts):
    # BUG: no checks
    return os.path.abspath(os.path.join(root, *parts))
