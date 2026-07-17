from .textutil import normalize_name

def register(name):
    key = normalize_name(name)
    return {"ok": bool(key), "key": key}
