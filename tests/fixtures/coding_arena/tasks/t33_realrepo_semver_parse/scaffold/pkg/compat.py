from .semver import parse

def is_at_least(v, major):
    return parse(v)[0] >= major
