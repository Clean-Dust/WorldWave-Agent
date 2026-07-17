"""Hub module — high fan-out entry points."""

from .core import leaf, mid, other_helper


class HubService:
    """Service that fans out to core helpers."""

    def run(self, x: int) -> int:
        a = mid(x)
        b = leaf(a)
        return b

    def label(self, s: str) -> str:
        return other_helper(s)


def hub_entry(x: int) -> int:
    """Top-level hub that calls HubService and mid."""
    svc = HubService()
    return svc.run(mid(x))


def downstream_one(x: int) -> int:
    return hub_entry(x) + 10


def downstream_two(x: int) -> str:
    return HubService().label(str(x))
