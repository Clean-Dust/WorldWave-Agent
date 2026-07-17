from .merge import deep_merge

DEFAULT = {"db": {"host": "localhost", "port": 5432}, "debug": False}

def load_settings(user):
    return deep_merge(DEFAULT, user or {})
