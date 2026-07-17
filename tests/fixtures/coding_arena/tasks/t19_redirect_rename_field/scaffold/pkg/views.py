from .model import User

def card(u: User):
    return f"{u.display()} <{u.normalized_email()}>"
