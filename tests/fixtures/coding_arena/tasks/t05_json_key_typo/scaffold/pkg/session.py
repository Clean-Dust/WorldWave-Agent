from .api_client import parse_user

def login_payload(payload):
    u = parse_user(payload)
    return u["username"]
