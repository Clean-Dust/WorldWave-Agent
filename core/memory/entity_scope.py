"""Request-scoped entity binding for memory isolation (Gate 0.2).

Process-global ``MemoryVNext.entity_id`` / shared MemoryTools can be rebound
by interleaved ``/ww/run`` calls. Every memory read/write must prefer the
**request-scoped** entity from a ContextVar so concurrent or sequential
rebinds cannot clobber another request mid-flight.

Usage:
    with bind_entity("entity_A"):
        # all memory ops resolve entity_A unless entity_id= is passed explicitly
        mv.remember("k", "v")
        ms.search("query")
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, Optional

# None = no request scope active (fall back to instance / explicit args)
_request_entity_id: ContextVar[Optional[str]] = ContextVar(
    "ww_memory_request_entity_id", default=None
)


def get_request_entity(fallback: str = "default") -> str:
    """Resolve active request entity, else fallback (never empty)."""
    cur = _request_entity_id.get()
    if cur is not None and str(cur).strip():
        return str(cur).strip()
    fb = (fallback or "").strip()
    return fb if fb else "default"


def set_request_entity(entity_id: str) -> Token:
    """Set request entity for the current context (return token for reset)."""
    eid = (entity_id or "").strip() or "default"
    return _request_entity_id.set(eid)


def reset_request_entity(token: Token) -> None:
    """Restore previous request entity from bind/set token."""
    _request_entity_id.reset(token)


def peek_request_entity() -> Optional[str]:
    """Return bound entity or None if no request scope is active."""
    cur = _request_entity_id.get()
    if cur is None:
        return None
    s = str(cur).strip()
    return s or None


@contextmanager
def bind_entity(entity_id: str) -> Iterator[str]:
    """Bind entity_id for the entire ``with`` block (request/run scope).

    Always resets on exit so later work cannot inherit a stale entity.
    """
    token = set_request_entity(entity_id)
    try:
        yield get_request_entity()
    finally:
        reset_request_entity(token)


def atom_belongs_to_entity(atom: Any, entity_id: str) -> bool:
    """Hard filter: never surface another cognitive entity's remember atoms.

    Rules:
    - meta.entity_id wins when present
    - else entity_id listed in atom.entities
    - untagged atoms: visible only to entity ``default`` (legacy)
    """
    entity = (entity_id or "").strip() or "default"
    meta = getattr(atom, "meta", None)
    if not isinstance(meta, dict) and isinstance(atom, dict):
        meta = atom.get("meta")
    if isinstance(meta, dict):
        me = str(meta.get("entity_id") or "").strip()
        if me:
            return me == entity

    ents: list = []
    raw_ents = getattr(atom, "entities", None)
    if raw_ents is None and isinstance(atom, dict):
        raw_ents = atom.get("entities")
    if raw_ents:
        ents = [str(e) for e in raw_ents]
    if entity in ents:
        return True

    # Untagged legacy: only default may see (prevents cross-entity leak)
    if entity == "default":
        return True
    return False


def resolve_entity_id(
    explicit: str = "",
    *,
    instance_fallback: str = "default",
) -> str:
    """Resolve entity: explicit arg > request ContextVar > instance fallback."""
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    return get_request_entity(instance_fallback or "default")
