def clean_token(s):
    if s is None:
        return ""
    # BUG: only strips regular space, not unicode ws
    t = str(s)
    while t.startswith(" "):
        t = t[1:]
    while t.endswith(" "):
        t = t[:-1]
    return t
