"""Core utilities — leaf functions and mid-level callers."""


def leaf(x: int) -> int:
    """Leaf pure function (call target for who_calls)."""
    return x * 2


def mid(x: int) -> int:
    """Mid-level function that calls leaf."""
    return leaf(x) + 1


def other_helper(s: str) -> str:
    return s.strip().lower()
