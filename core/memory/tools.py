"""
core/memory/tools.py — Self-editing memory tools (single system)

Gives the cognitive agent the ability to actively manage its own memory —
the agent transitions from "passive consumer of recall results" to
"active manager of its knowledge".

Tools write against the **single** memory system (MemoryVNext labeled facts
+ atom nets). EntityState flat WM is an optional compatibility dual-write
shim only — product inject does not use it as a second brain.

  - remember(key, value, kind=...): explicit kind labels only (no keyword guess)
  - forget(key): supersede / remove
  - recall_mine(query): list online labeled facts

Product term for kind: 标签; API field stays ``kind``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from core.entity_state import normalize_wm_kind

log = logging.getLogger("ww.memory.tools")

# REMOVAL DEADLINE: EntityState dual-write shim — remove after 2026-08-31
# once migration prove stays green and no external readers of entity WM remain.
_ENTITY_WM_DUAL_WRITE = True
_ENTITY_WM_DUAL_WRITE_REMOVE_BY = "2026-08-31"


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
        self._entity_id = entity_id or "default"
        self._wire_wm_evict()
        self._bind_vnext_entity()

    def set_entity(self, entity_id: str):
        """Set the current entity context (called before each interaction)."""
        self._entity_id = entity_id or "default"
        self._bind_vnext_entity()

    def _vnext(self):
        if self._memory is None:
            return None
        return getattr(self._memory, "vnext", None)

    def _bind_vnext_entity(self) -> None:
        vnext = self._vnext()
        if vnext is not None and hasattr(vnext, "set_entity"):
            try:
                vnext.set_entity(self._entity_id)
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
        """Store a fact in the single memory system.

        Args:
            key: Short label for the fact (e.g., "user_name", "preferred_model")
            value: The fact content
            category: Optional grouping only (does NOT affect eviction)
            is_core: If True, never auto-evicted under capacity pressure
            kind: Explicit WM label id — constraint | commitment | outcome | rationale.
                  Empty/illegal → outcome. Never inferred from keywords.

        Returns:
            {"status": "stored", "key": key, "previous": old_value or None, ...}
        """
        if not key or not value:
            return {"status": "error", "message": "key and value are required"}

        kind_arg: Optional[str] = kind if (kind is not None and str(kind).strip()) else None
        resolved_kind = normalize_wm_kind(kind_arg)
        previous = None
        vnext = self._vnext()

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
                    entity_id=self._entity_id,
                )
                previous = result.get("previous")
                resolved_kind = normalize_wm_kind(result.get("kind") or resolved_kind)
                log.info(
                    "Entity %s: remember(vnext) '%s' kind=%s is_core=%s",
                    self._entity_id[:12],
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
                        tags=[key, category, kind_tag],
                        context_id=f"remember:{self._entity_id}:{resolved_kind}",
                        is_core=True,
                    )
                elif hasattr(self._memory, "store_fact"):
                    self._memory.store_fact(
                        fact=fact_text,
                        entities=[key, category, kind_tag],
                        context_id=f"remember:{self._entity_id}:{resolved_kind}",
                    )
            except Exception as e:
                log.warning("memory store_fact failed: %s", e)

        # ── EntityState path ──
        # When v-next is active: dual-write shim only (not product inject source).
        # REMOVE BY: _ENTITY_WM_DUAL_WRITE_REMOVE_BY.
        # When no MemorySystem (unit tests): entity is the write target.
        if self._entity_mgr and (
            (_ENTITY_WM_DUAL_WRITE and self._vnext() is not None)
            or (self._vnext() is None)
        ):
            try:
                state = self._entity_mgr.get(self._entity_id)
                if previous is None:
                    previous = state.working_memory.get(key)
                self._entity_mgr.set_working_memory(
                    self._entity_id,
                    key,
                    value,
                    kind=kind_arg,
                    is_core=bool(is_core),
                )
                meta = state.working_memory_meta.get(key) or {}
                resolved_kind = normalize_wm_kind(meta.get("kind") or resolved_kind)
            except Exception as e:
                log.debug("entity WM write skipped: %s", e)

        return {
            "success": True,
            "status": "stored",
            "key": key,
            "previous": previous,
            "is_core": bool(is_core),
            "kind": resolved_kind,
            "timestamp": time.time(),
            "output": f"Remembered {key}: {value}",
            "store": "vnext" if self._vnext() is not None else "legacy_shim",
        }

    # ── forget ───────────────────────────────────────────────────

    def forget(self, key: str) -> dict:
        """Mark a fact as no longer valid (single system)."""
        if not key:
            return {"status": "error", "message": "key is required"}

        was = None
        vnext = self._vnext()
        if vnext is not None:
            try:
                result = vnext.forget(key, entity_id=self._entity_id)
                was = result.get("was")
            except Exception as e:
                log.warning("vnext.forget failed: %s", e)

        if self._memory is not None and vnext is None:
            try:
                self._memory.store_fact(
                    fact=f"[SUPERSEDED] {key}",
                    entities=[key, "superseded"],
                    context_id=f"forget:{self._entity_id}",
                )
            except Exception as e:
                log.debug("forget store_fact: %s", e)

        if _ENTITY_WM_DUAL_WRITE and self._entity_mgr:
            try:
                state = self._entity_mgr.get(self._entity_id)
                if was is None:
                    was = state.working_memory.get(key)
                self._entity_mgr.delete_working_memory(self._entity_id, key)
            except Exception as e:
                log.debug("entity forget dual-write: %s", e)

        if vnext is None and self._memory is None and self._entity_mgr:
            state = self._entity_mgr.get(self._entity_id)
            was = state.working_memory.get(key)
            self._entity_mgr.delete_working_memory(self._entity_id, key)

        log.info(
            "Entity %s: forget '%s' (was: %s)",
            self._entity_id[:12],
            key,
            was,
        )

        return {
            "success": True,
            "status": "forgotten",
            "key": key,
            "was": was,
            "timestamp": time.time(),
            "output": f"Forgot {key}" + (f" (was: {was})" if was else ""),
        }

    # ── recall_mine ──────────────────────────────────────────────

    def recall_mine(self, query: str = "", limit: int = 10) -> dict:
        """Query labeled facts from the single memory system."""
        facts: Dict[str, str] = {}
        vnext = self._vnext()

        if vnext is not None:
            try:
                listed = vnext.list_facts(
                    query, entity_id=self._entity_id, limit=limit
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
            state = self._entity_mgr.get(self._entity_id)
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
            "output": output,
            "store": "vnext" if vnext is not None else "entity_shim",
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
                    "Store a fact in your memory. Use this when you learn something "
                    "new about the user or the current task. The fact will persist "
                    "across all conversations and platforms. "
                    "Set kind (label id) explicitly (never guess from keywords): "
                    "constraint=约束 (iron rule), commitment=承诺 (plan/next step), "
                    "outcome=结果 (fact/result; default), rationale=理由 (why). "
                    "category is optional grouping only — does not affect eviction. "
                    "Prefer is_core=True for must-keep facts. "
                    "Example: remember(key='no_netplan', value='never change netplan', "
                    "kind='constraint')"
                ),
                "parameters": {
                    "key": {"type": "string", "description": "Short label for the fact"},
                    "value": {"type": "string", "description": "The fact content"},
                    "category": {
                        "type": "string",
                        "description": (
                            "Optional grouping only (general, preference, technical, "
                            "contact, project). Does not affect eviction; not a WM label."
                        ),
                    },
                    "is_core": {
                        "type": "boolean",
                        "description": "Optional: mark as core (never auto-evicted)",
                    },
                    "kind": {
                        "type": "string",
                        "description": (
                            "Optional WM label id for eviction weight: "
                            "constraint (约束, iron rule, weight 4), "
                            "commitment (承诺, plan/next step, weight 3), "
                            "outcome (结果, fact/result, weight 2; default), "
                            "rationale (理由, why/process, weight 1). "
                            "Empty or unknown → outcome. Explicit only; no keyword inference. "
                            "category ≠ kind/label."
                        ),
                    },
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
        ]
