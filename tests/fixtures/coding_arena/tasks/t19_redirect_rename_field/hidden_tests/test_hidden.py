from pkg.model import User
from pkg.views import card

def test_display():
    assert User("  Ann ", "x").display() == "Ann"

def test_email():
    assert User("a", "  Bob@X.COM ").normalized_email() == "bob@x.com"

def test_card():
    assert card(User("  Z ", "A@B.C")) == "Z <a@b.c>"
