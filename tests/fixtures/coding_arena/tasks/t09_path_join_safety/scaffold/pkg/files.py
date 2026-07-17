import os

def safe_join(root, *parts):
    # BUG: no traversal check
    return os.path.abspath(os.path.join(root, *parts))
