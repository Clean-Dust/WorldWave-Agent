def get_setting(settings, key, defaults=None):
    defaults = defaults or {}
    if key in settings:
        return settings[key]
    # BUG: always returns defaults["default"] if present
    return defaults.get("default")
