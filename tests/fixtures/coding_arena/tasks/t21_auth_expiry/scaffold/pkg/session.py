from .auth import is_expired

def active(token_exp, now):
    return not is_expired(token_exp, now)
