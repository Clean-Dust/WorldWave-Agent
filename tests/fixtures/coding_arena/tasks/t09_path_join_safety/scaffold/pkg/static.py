from .files import safe_join

def resolve_asset(root, name):
    return safe_join(root, "assets", name)
