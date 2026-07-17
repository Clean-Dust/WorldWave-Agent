from .config import get_setting

DEFAULTS = {"theme": "dark", "lang": "en"}

def theme(settings):
    return get_setting(settings, "theme", DEFAULTS)
