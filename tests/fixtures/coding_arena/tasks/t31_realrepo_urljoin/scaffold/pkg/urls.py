def join_url(base, path):
    # BUG: naive concat double slash
    return base + path
