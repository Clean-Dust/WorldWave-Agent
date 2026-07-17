def parse_user(payload):
    if not isinstance(payload, dict):
        raise TypeError("payload must be dict")
    return {
        "id": payload.get("id"),
        "username": payload.get("user_name") or "",  # BUG typo key
    }
