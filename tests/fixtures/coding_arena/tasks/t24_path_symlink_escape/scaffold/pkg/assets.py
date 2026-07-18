from .paths import resolve_under

def asset_path(root, name):
    return resolve_under(root, "static", name)
