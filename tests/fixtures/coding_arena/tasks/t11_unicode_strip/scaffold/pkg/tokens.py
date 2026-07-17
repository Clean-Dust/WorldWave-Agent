from .clean import clean_token

def normalize_tokens(items):
    return [clean_token(x) for x in items if clean_token(x)]
