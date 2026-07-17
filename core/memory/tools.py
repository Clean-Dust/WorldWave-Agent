"""
core/memory/tools.py — Self-editing memory tools (single system)

Gives the cognitive agent the ability to actively manage its own memory —
the agent transitions from "passive consumer of recall results" to
"active manager of its knowledge".

Tools write against the **single** memory system (MemoryVNext labeled facts
+ atom nets). EntityState flat WM is **not** product SoT; dual-write is
emergency-only via WW_ENTITY_WM_DUAL_WRITE=1 (default off).

  - remember(key, value, kind=...): explicit kind labels only (no keyword guess)
  - forget(key): supersede / remove
  - recall_mine(query): list online labeled facts
  - switch_topic(title): park current topic to STM; start independent thread

Product term for kind: 标签; API field stays ``kind``.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from core.entity_state import normalize_wm_kind
from core.memory.entity_scope import (
    peek_request_entity,
    resolve_entity_id,
    set_request_entity,
)

log = logging.getLogger("ww.memory.tools")

# Natural-language remember patterns (Gate 0.1 reliability)
_RE_KEY_VALUE = re.compile(
    r"(?:^|[\s,;])key\s*[=:]\s*['\"]?([A-Za-z0-9_.\-]+)['\"]?"
    r".*?"
    r"(?:^|[\s,;])value\s*[=:]\s*['\"]?(.+?)['\"]?\s*$",
    re.I | re.S,
)
_RE_KEY_VALUE_ALT = re.compile(
    r"key\s*[=:]\s*['\"]?([A-Za-z0-9_.\-]+)['\"]?"
    r"\s+"
    r"value\s*[=:]\s*['\"]?(.+?)['\"]?(?:\s|$)",
    re.I | re.S,
)
_RE_REMEMBER_IS = re.compile(
    r"(?:please\s+)?remember(?:\s+(?:that|this))?\s*[:\-]?\s*"
    r"(?:my\s+)?(.+?)\s+(?:is|are|=|:)\s+(.+?)\s*$",
    re.I | re.S,
)
_RE_REMEMBER_COLON = re.compile(
    r"(?:please\s+)?remember\s*[:\-]\s*(.+)$",
    re.I | re.S,
)
_RE_STORE_THAT = re.compile(
    r"^(?:i\s+)?(?:like|hate|prefer|use|work\s+as|live\s+in)\s+(.+?)(?:\.\s*store.*)?$",
    re.I,
)

# Map natural phrases → stable keys
_NL_KEY_MAP = (
    (re.compile(r"\bhome\s*city\b|\bcity\b|\blive\b", re.I), "home_city"),
    (re.compile(r"\bpet(?:'s)?\s*name\b|\bpet\b", re.I), "pet_name"),
    (re.compile(r"\b(?:current\s+)?job\b|\bwork(?:s|ing)?\b|\brole\b", re.I), "current_job"),
    (re.compile(r"\bpreference\b|\bprefer\b", re.I), "preference"),
    (re.compile(r"\biron\s*rule\b|\brule\b|\bconstraint\b", re.I), "iron_rule"),
    (re.compile(r"\bfavorite\s*color\b|\bcolour\b", re.I), "favorite_color"),
    (re.compile(r"\bname\b", re.I), "user_name"),
)


def _slug_key(label: str, fallback: str = "fact") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    if not s:
        return fallback
    return s[:48]


def extract_remember_kv(utterance: str) -> Optional[Tuple[str, str]]:
    """Best-effort extract (key, value) from a natural remember utterance.

    Handles:
      - key=foo value=bar
      - Remember: my city is Tokyo
      - Please remember: my pet's name is Luna
      - My job is Engineer. Store that.
    Returns None when extraction is unreliable.
    """
    text = (utterance or "").strip()
    if not text:
        return None

    # 1) Explicit key= / value=
    for pat in (_RE_KEY_VALUE, _RE_KEY_VALUE_ALT):
        m = pat.search(text)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip().strip("'\"")
            if k and v:
                return k, v

    # 2) Remember: my X is Y
    m = _RE_REMEMBER_IS.search(text)
    if m:
        label, value = m.group(1).strip(), m.group(2).strip().strip(".'\"")
        # Strip trailing instructions
        value = re.split(r"\.\s*(?:store|using|call|do not)\b", value, maxsplit=1, flags=re.I)[0].strip()
        if label and value and len(value) < 500:
            key = "fact"
            for rx, mapped in _NL_KEY_MAP:
                if rx.search(label):
                    key = mapped
                    break
            else:
                key = _slug_key(label, "fact")
            return key, value

    # 3) Remember: <free text> — store whole clause under a derived key
    m = _RE_REMEMBER_COLON.search(text)
    if m:
        body = m.group(1).strip().strip(".'\"")
        if body and len(body) < 500:
            # Try "my X is Y" inside body
            m2 = re.search(
                r"(?:my\s+)?(.+?)\s+(?:is|are|=)\s+(.+)$", body, re.I
            )
            if m2:
                label, value = m2.group(1).strip(), m2.group(2).strip().strip(".'\"")
                key = "fact"
                for rx, mapped in _NL_KEY_MAP:
                    if rx.search(label):
                        key = mapped
                        break
                else:
                    key = _slug_key(label, "user_fact")
                return key, value
            key = "user_fact"
            for rx, mapped in _NL_KEY_MAP:
                if rx.search(body):
                    key = mapped
                    break
            return key, body

    # 4) "I like X. Store that."
    low = text.lower()
    if "store" in low or "remember" in low:
        m = _RE_STORE_THAT.match(text.strip())
        if m:
            return "preference", m.group(1).strip().strip(".'\"")

    return None

# Product default: dual-write OFF. Emergency only: WW_ENTITY_WM_DUAL_WRITE=1.
# EntityState remains for identity continuity + isolated unit fixtures.
_ENTITY_WM_DUAL_WRITE = False
_ENTITY_WM_DUAL_WRITE_REMOVE_BY = "2026-08-31"  # historical; path is env-gated


def entity_wm_dual_write_enabled() -> bool:
    """Emergency dual-write EntityState.working_memory. Default OFF.

    Set WW_ENTITY_WM_DUAL_WRITE=1 only for recovery. Product path is
    MemoryVNext / LabeledFactStore / AtomNet / LTM only.
    """
    raw = os.environ.get("WW_ENTITY_WM_DUAL_WRITE")
    if raw is None or str(raw).strip() == "":
        return bool(_ENTITY_WM_DUAL_WRITE)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class MemoryTools:
    """Self-editing memory tools callable by the LLM agent.

    Usage in spiral loop:
        tools = MemoryTools(memory_system, entity_state_manager, entity_id)
        tools.remember("user_preferred_model", "deepseek-v4-pro")
        tools.forget("old_api_key")
    """

    def __init__(self, memory_system=None, entity_state_mgr=None, entity_id: str = ""):
        self._memory = memory_system
        self._entity_mgr = entity_state_mgr
        # Instance fallback; request ContextVar wins on every tool call (Gate 0.2)
        self._entity_id = entity_id or "default"
        self._wire_wm_evict()
        self._bind_vnext_entity()

    @property
    def entity_id(self) -> str:
        """Active entity for this tool call (request scope > instance)."""
        return self._active_entity()

    def set_entity(self, entity_id: str):
        """Set the current entity context (called before each interaction).

        Updates instance binding. Only mutates request ContextVar when a
        ``bind_entity`` scope is already active (avoids permanent pollution).
        """
        self._entity_id = entity_id or "default"
        if peek_request_entity() is not None:
            set_request_entity(self._entity_id)
        self._bind_vnext_entity()

    def _active_entity(self) -> str:
        """Request ContextVar when bound; else this tools instance entity."""
        scoped = peek_request_entity()
        if scoped:
            return scoped
        return (self._entity_id or "default").strip() or "default"

    def _vnext(self):
        if self._memory is None:
            return None
        return getattr(self._memory, "vnext", None)

    def _bind_vnext_entity(self) -> None:
        vnext = self._vnext()
        eid = self._active_entity()
        if vnext is not None and hasattr(vnext, "set_entity"):
            try:
                # Pass only instance fallback update; vnext.set_entity respects scope
                vnext.set_entity(eid)
            except Exception as e:
                log.debug("vnext.set_entity failed: %s", e)

    def _wire_wm_evict(self) -> None:
        """Promote important WM evictions into durable atoms (once per mgr).

        When v-next is present, wire LabeledFactStore on_evict as well so
        capacity pressure promotes into the same MemorySystem.
        """
        vnext = self._vnext()
        if vnext is not None and self._memory is not None:
            facts = getattr(vnext, "facts", None)
            if facts is not None and not getattr(facts, "_wm_evict_wired", False):

                def _on_fact_evict(
                    entity_id: str,
                    key: str,
                    value: str,
                    meta: Optional[Dict[str, Any]] = None,
                ) -> None:
                    self._promote_evicted(entity_id, key, value, meta)

                facts.set_on_evict(_on_fact_evict)
                facts._wm_evict_wired = True

        if not self._entity_mgr or not self._memory:
            return
        if getattr(self._entity_mgr, "_wm_evict_wired", False):
            return

        def _on_wm_evict(
            entity_id: str,
            key: str,
            value: str,
            meta: Optional[Dict[str, Any]] = None,
        ) -> None:
            self._promote_evicted(entity_id, key, value, meta)

        self._entity_mgr.set_on_wm_evict(_on_wm_evict)
        self._entity_mgr._wm_evict_wired = True

    def _promote_evicted(
        self,
        entity_id: str,
        key: str,
        value: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        fact = f"{key}: {value}"
        kind = "outcome"
        if isinstance(meta, dict):
            kind = normalize_wm_kind(meta.get("kind"))
        kind_tag = f"wm_kind:{kind}"
        try:
            if hasattr(self._memory, "store_fact"):
                self._memory.store_fact(
                    fact=fact,
                    entities=[key, "wm_evict", kind_tag],
                    context_id=f"wm_evict:{entity_id}:{kind}",
                )
            elif hasattr(self._memory, "store_text"):
                self._memory.store_text(
                    fact,
                    source="wm_evict",
                    entities=[key, "wm_evict", kind_tag],
                )
            log.info(
                "WM promote→durable entity=%s key=%s kind=%s",
                entity_id[:12],
                key,
                kind,
            )
        except Exception as e:
            log.warning("WM promote store failed for %s: %s", key, e)

    # ── remember ─────────────────────────────────────────────────

    def remember(
        self,
        key: str,
        value: str,
        category: str = "general",
        is_core: bool = False,
        kind: str = "",
    ) -> dict:
        """Store a fact in the single memory system for the active entity.

        Args:
            key: Short label for the fact (e.g., "user_name", "preferred_model")
            value: The fact content
            category: Optional grouping only (does NOT affect eviction)
            is_core: If True, never auto-evicted under capacity pressure
            kind: Explicit WM label id — constraint | commitment | outcome | rationale.
                  Empty/illegal → outcome. Never inferred from keywords.

        Returns:
            On success: {"success": True, "status": "stored", "key": ..., ...}
            On missing args: {"success": False, "status": "error", ...} — never silent OK
        """
        key = (key or "").strip() if isinstance(key, str) else str(key or "").strip()
        value = (value or "").strip() if isinstance(value, str) else str(value or "").strip()
        if not key or not value:
            return {
                "success": False,
                "status": "error",
                "error": "remember tool requires both key and value",
                "message": "key and value are required",
                "output": "remember failed: key and value are required",
            }

        # Always re-bind v-next to current (request-scoped) entity before write
        eid = self._active_entity()
        self._bind_vnext_entity()

        kind_arg: Optional[str] = kind if (kind is not None and str(kind).strip()) else None
        resolved_kind = normalize_wm_kind(kind_arg)
        previous = None
        vnext = self._vnext()
        stored = False

        # ── Primary path: MemoryVNext labeled facts (single SoT) ──
        if vnext is not None:
            try:
                result = vnext.remember(
                    key,
                    value,
                    kind=resolved_kind,
                    is_core=bool(is_core),
                    logical_net="world",
                    category=category,
                    entity_id=eid,
                )
                previous = result.get("previous")
                resolved_kind = normalize_wm_kind(result.get("kind") or resolved_kind)
                stored = True
                log.info(
                    "Entity %s: remember(vnext) '%s' kind=%s is_core=%s",
                    eid[:12],
                    key,
                    resolved_kind,
                    is_core,
                )
            except Exception as e:
                log.warning("vnext.remember failed: %s", e)
                vnext = None  # fall through to legacy paths

        # ── Durable hippocampus/fact_store when no vnext or as sleep backend ──
        if self._memory is not None and vnext is None:
            fact_text = self._format_fact(key, value, category)
            kind_tag = f"wm_kind:{resolved_kind}"
            try:
                if is_core and hasattr(self._memory, "_do_store"):
                    self._memory._do_store(
                        content=fact_text,
                        source="inference",
                        atom_type="semantic",
                        tags=[key, category, kind_tag, eid],
                        context_id=f"remember:{eid}:{resolved_kind}",
                        is_core=True,
                    )
                    stored = True
                elif hasattr(self._memory, "store_fact"):
                    self._memory.store_fact(
                        fact=fact_text,
                        entities=[key, category, kind_tag, eid],
                        context_id=f"remember:{eid}:{resolved_kind}",
                    )
                    stored = True
            except Exception as e:
                log.warning("memory store_fact failed: %s", e)

        # ── EntityState path ──
        # Product SoT is v-next. Dual-write only if WW_ENTITY_WM_DUAL_WRITE=1.
        # When no v-next (unit fixtures / emergency kill): entity is write target.
        if self._entity_mgr and (
            (entity_wm_dual_write_enabled() and self._vnext() is not None)
            or (self._vnext() is None)
        ):
            try:
                state = self._entity_mgr.get(eid)
                if previous is None:
                    previous = state.working_memory.get(key)
                self._entity_mgr.set_working_memory(
                    eid,
                    key,
                    value,
                    kind=kind_arg,
                    is_core=bool(is_core),
                )
                meta = state.working_memory_meta.get(key) or {}
                resolved_kind = normalize_wm_kind(meta.get("kind") or resolved_kind)
                stored = True
            except Exception as e:
                log.debug("entity WM write skipped: %s", e)

        if not stored:
            return {
                "success": False,
                "status": "error",
                "error": "remember failed: no store backend accepted the write",
                "message": "remember failed: no store backend accepted the write",
                "output": "remember failed: storage unavailable",
                "entity_id": eid,
            }

        return {
            "success": True,
            "status": "stored",
            "key": key,
            "value": value,
            "previous": previous,
            "is_core": bool(is_core),
            "kind": resolved_kind,
            "entity_id": eid,
            "timestamp": time.time(),
            "output": f"Remembered {key}: {value}",
            "store": "vnext" if self._vnext() is not None else "legacy_shim",
        }

    # ── forget ───────────────────────────────────────────────────

    def forget(self, key: str) -> dict:
        """Mark a fact as no longer valid (single system, current entity only)."""
        key = (key or "").strip() if isinstance(key, str) else str(key or "").strip()
        if not key:
            return {
                "success": False,
                "status": "error",
                "error": "key is required",
                "message": "key is required",
                "output": "forget failed: key is required",
            }

        eid = self._active_entity()
        self._bind_vnext_entity()
        was = None
        vnext = self._vnext()
        if vnext is not None:
            try:
                result = vnext.forget(key, entity_id=eid)
                was = result.get("was")
            except Exception as e:
                log.warning("vnext.forget failed: %s", e)

        if self._memory is not None and vnext is None:
            try:
                self._memory.store_fact(
                    fact=f"[SUPERSEDED] {key}",
                    entities=[key, "superseded"],
                    context_id=f"forget:{eid}",
                )
            except Exception as e:
                log.debug("forget store_fact: %s", e)

        if entity_wm_dual_write_enabled() and self._entity_mgr and vnext is not None:
            try:
                state = self._entity_mgr.get(eid)
                if was is None:
                    was = state.working_memory.get(key)
                self._entity_mgr.delete_working_memory(eid, key)
            except Exception as e:
                log.debug("entity forget dual-write: %s", e)

        # Unit-fixture path: EntityState only when no product store
        if vnext is None and self._entity_mgr:
            try:
                state = self._entity_mgr.get(eid)
                if was is None:
                    was = state.working_memory.get(key)
                self._entity_mgr.delete_working_memory(eid, key)
            except Exception as e:
                log.debug("entity forget fixture path: %s", e)

        log.info(
            "Entity %s: forget '%s' (was: %s)",
            eid[:12],
            key,
            was,
        )

        return {
            "success": True,
            "status": "forgotten",
            "key": key,
            "was": was,
            "entity_id": eid,
            "timestamp": time.time(),
            "output": f"Forgot {key}" + (f" (was: {was})" if was else ""),
        }

    # ── recall_mine ──────────────────────────────────────────────

    def recall_mine(self, query: str = "", limit: int = 10) -> dict:
        """Query labeled facts for the current entity only (no cross-entity leak)."""
        eid = self._active_entity()
        self._bind_vnext_entity()
        facts: Dict[str, str] = {}
        vnext = self._vnext()

        if vnext is not None:
            try:
                listed = vnext.list_facts(
                    query, entity_id=eid, limit=limit
                )
                raw = listed.get("facts") or {}
                # Flatten value dicts → str for tool output compat
                for k, v in raw.items():
                    if isinstance(v, dict):
                        facts[k] = str(v.get("value", ""))
                    else:
                        facts[k] = str(v)
            except Exception as e:
                log.warning("vnext.list_facts failed: %s", e)

        # Prefer v-next; only fall back to entity if v-next empty / missing
        if not facts and self._entity_mgr:
            state = self._entity_mgr.get(eid)
            facts = dict(state.working_memory)
            if query:
                query_lower = query.lower()
                facts = {
                    k: v
                    for k, v in facts.items()
                    if query_lower in k.lower() or query_lower in v.lower()
                }

        items = list(facts.items())[:limit]
        facts_out = dict(items)
        if facts_out:
            output = "\n".join(f"{k}: {v}" for k, v in facts_out.items())
        else:
            output = ""

        return {
            "success": True,
            "facts": facts_out,
            "total": len(items),
            "entity_id": eid,
            "output": output,
            "store": "vnext" if vnext is not None else "entity_shim",
        }

    # ── switch_topic ─────────────────────────────────────────────

    def switch_topic(self, title: str = "") -> dict:
        """Park current topic into STM and start an independent topic body."""
        vnext = self._vnext()
        if vnext is None:
            return {
                "success": False,
                "status": "error",
                "message": "switch_topic requires MemoryVNext",
                "output": "Memory v-next unavailable for topic switch",
            }
        try:
            result = vnext.switch_topic(title=title or "")
            log.info(
                "Entity %s: switch_topic → %s",
                self._active_entity()[:12],
                result.get("title") or result.get("active_id"),
            )
            return {
                "success": True,
                "status": "switched",
                "output": (
                    f"Switched topic"
                    + (f" to: {result.get('title')}" if result.get("title") else "")
                    + f" (stm={result.get('stm_count', 0)})"
                ),
                **result,
            }
        except Exception as e:
            log.warning("switch_topic failed: %s", e)
            return {
                "success": False,
                "status": "error",
                "message": str(e),
                "output": f"switch_topic failed: {e}",
            }

    # ── Internal ─────────────────────────────────────────────────

    @staticmethod
    def _format_fact(key: str, value: str, category: str) -> str:
        return f"[{category}] {key}: {value}"

    # ── Tool definitions for registry ────────────────────────────

    @staticmethod
    def get_tool_defs() -> list:
        """Return tool definitions for registration in ToolRegistry."""
        return [
            {
                "name": "remember",
                "description": (
                    "Store a fact for the CURRENT entity. REQUIRED: key and value. "
                    "Never call with empty arguments. "
                    "Natural language: user says 'Remember: my city is Tokyo' → "
                    "remember(key='home_city', value='Tokyo'). "
                    "kind optional: constraint|commitment|outcome|rationale."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "REQUIRED short label (e.g. home_city, pet_name)",
                        },
                        "value": {
                            "type": "string",
                            "description": "REQUIRED fact content to store",
                        },
                        "category": {
                            "type": "string",
                            "description": (
                                "Optional grouping only (general, preference, technical, "
                                "contact, project). Does not affect eviction; not a WM label."
                            ),
                            "default": "general",
                        },
                        "is_core": {
                            "type": "boolean",
                            "description": "Optional: mark as core (never auto-evicted)",
                            "default": False,
                        },
                        "kind": {
                            "type": "string",
                            "description": (
                                "Optional WM label id: constraint|commitment|outcome|rationale. "
                                "Empty → outcome. Explicit only."
                            ),
                            "default": "",
                        },
                    },
                    "required": ["key", "value"],
                },
                "category": "memory",
            },
            {
                "name": "forget",
                "description": (
                    "Mark a stored fact as no longer valid. Use this when you detect "
                    "that previously stored information is outdated or incorrect. "
                    "The old fact is not deleted — it is superseded for historical reference. "
                    "Example: forget(key='old_api_key')"
                ),
                "parameters": {
                    "key": {"type": "string", "description": "The fact key to supersede"},
                },
                "category": "memory",
            },
            {
                "name": "recall_mine",
                "description": (
                    "Query what you currently know about the user and current context. "
                    "Use this before responding to check what facts you have stored. "
                    "Example: recall_mine(query='preference') or recall_mine() for all."
                ),
                "parameters": {
                    "query": {"type": "string", "description": "Optional filter keyword"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "category": "memory",
            },
            {
                "name": "switch_topic",
                "description": (
                    "Park the current conversation topic into short-term memory and "
                    "start an independent topic body. Use on clear subject change. "
                    "Example: switch_topic(title='Weekend hiking')"
                ),
                "parameters": {
                    "title": {
                        "type": "string",
                        "description": "Short title for the new independent topic",
                    },
                },
                "category": "memory",
            },
        ]
