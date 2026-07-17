def normalize_name(name):
    # BUG: no None/empty guard
    return name.strip().lower()
