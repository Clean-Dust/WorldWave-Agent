def is_expired(exp, now):
    # BUG: token still valid at exact exp
    return now > exp
