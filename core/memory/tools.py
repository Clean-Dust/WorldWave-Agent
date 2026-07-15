"""
core/memory/tools.py — Self-editing memory tools

Gives the cognitive agent the ability to actively manage its own memory —
the agent transitions from "passive consumer of recall results" to
"active manager of its knowledge".

Two core tools:
- remember(key, value, kind=...): Store a fact in memory. Agent calls this
  when it learns something new or detects a change. kind is an explicit
  WM label id (constraint / commitment / outcome / rationale) — never
  inferred from keywords. Product term: 标签; API field stays ``kind``.
- forget(key): Mark a fact as no longer valid. Superseded, not deleted.

These are the mechanism that makes entity continuity possible — the agent
can say "I'll remember that" and actually do it.

Integration:
- EntityStateManager: bounded working memory (RAM, capacity-evicted by
  label weight)
- MemorySystem: durable atoms; also receives promote-on-evict from WM
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from core.entity_state import normalize_wm_kind

log = logging.getLogger("ww.memory.tools")


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

    def set_entity(self, entity_id: str):
        """Set the current entity context (called before each interaction)."""
        self._entity_id = entity_id

    def _wire_wm_evict(self) -> None:
        """Promote important WM evictions into MemorySystem atoms (once per mgr)."""
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
                    "WM promote→LTM entity=%s key=%s kind=%s",
                    entity_id[:12],
                    key,
                    kind,
                )
            except Exception as e:
                log.warning("WM promote store failed for %s: %s", key, e)

        self._entity_mgr.set_on_wm_evict(_on_wm_evict)
        self._entity_mgr._wm_evict_wired = True

    # ── remember ─────────────────────────────────────────────────

    def remember(
        self,
        key: str,
        value: str,
        category: str = "general",
        is_core: bool = False,
        kind: str = "",
    ) -> dict:
        """Store a fact in entity memory. Called by the agent when it learns something.

        Args:
            key: Short label for the fact (e.g., "user_name", "preferred_model")
            value: The fact content (e.g., "Chung", "deepseek-v4-pro")
            category: Optional grouping only (general, preference, technical, etc.).
                      Does NOT affect eviction. Never call category a 标签/label
                      for ranking — only ``kind`` is the WM label id.
            is_core: If True, mark as core memory (never auto-evicted / GC'd).
                     Iron rule: prefer is_core=True for must-keep facts.
                     Always dual-writes to MemorySystem atoms; WM key is protected.
            kind: Explicit WM label id for eviction weight (not keyword-inferred):
                  - constraint (约束): iron rule e.g. never change netplan — soft
                    weight 4; if is_core omitted, still high protect but soft only
                  - commitment (承诺): next step / plan choice — weight 3
                  - outcome (结果): fact / code / result — weight 2 (default)
                  - rationale (理由): why / process note — weight 1, easiest squeeze
                  Empty/illegal → outcome (default). constraint does NOT replace is_core.

        Returns:
            {"status": "stored", "key": key, "previous": old_value or None}

        This is a SELF-EDITING operation — the agent decides what to remember.
        The old value (if any) is returned so the agent can confirm the update.
        """
        if not key or not value:
            return {"status": "error", "message": "key and value are required"}

        previous = None
        # Empty kind → None so set_working_memory preserves existing / defaults outcome
        kind_arg: Optional[str] = kind if (kind is not None and str(kind).strip()) else None
        resolved_kind = normalize_wm_kind(kind_arg)

        # 1. Entity working memory (bounded RAM; is_core keys are not auto-evicted)
        if self._entity_mgr:
            state = self._entity_mgr.get(self._entity_id)
            previous = state.working_memory.get(key)
            self._entity_mgr.set_working_memory(
                self._entity_id,
                key,
                value,
                kind=kind_arg,
                is_core=bool(is_core),
            )
            meta = state.working_memory_meta.get(key) or {}
            resolved_kind = normalize_wm_kind(meta.get("kind"))
            log.info(
                "Entity %s: remember '%s' = '%s' (was: %s, is_core=%s, kind=%s)",
                self._entity_id[:12],
                key,
                value[:50],
                previous,
                is_core,
                resolved_kind,
            )

        # 2. Semantic / atom memory (durable). is_core always goes to MemorySystem
        #    so core facts are not only in the volatile WM buffer.
        if self._memory:
            fact_text = self._format_fact(key, value, category)
            kind_tag = f"wm_kind:{resolved_kind}"
            if is_core and hasattr(self._memory, "_do_store"):
                self._memory._do_store(
                    content=fact_text,
                    source="inference",
                    atom_type="semantic",
                    tags=[key, category, kind_tag],
                    context_id=f"remember:{self._entity_id}:{resolved_kind}",
                    is_core=True,
                )
            else:
                self._memory.store_fact(
                    fact=fact_text,
                    entities=[key, category, kind_tag],
                    context_id=f"remember:{self._entity_id}:{resolved_kind}",
                )

        return {
            "success": True,
            "status": "stored",
            "key": key,
            "previous": previous,
            "is_core": bool(is_core),
            "kind": resolved_kind,
            "timestamp": time.time(),
            "output": f"Remembered {key}: {value}",
        }

    # ── forget ───────────────────────────────────────────────────

    def forget(self, key: str) -> dict:
        """Mark a fact as no longer valid. Called by the agent when it detects
        that stored information is outdated or incorrect.

        Args:
            key: The fact key to supersede

        Returns:
            {"status": "forgotten", "key": key, "was": old_value or None}

        The old fact is NOT deleted — it is superseded. This preserves the
        temporal history (what was believed when) while preventing stale
        facts from influencing future decisions.
        """
        if not key:
            return {"status": "error", "message": "key is required"}

        was = None

        # 1. Remove from entity working memory
        if self._entity_mgr:
            state = self._entity_mgr.get(self._entity_id)
            was = state.working_memory.get(key)
            self._entity_mgr.delete_working_memory(self._entity_id, key)

        # 2. In semantic memory, mark related facts as superseded
        if self._memory:
            self._memory.store_fact(
                fact=f"[SUPERSEDED] {key}",
                entities=[key, "superseded"],
                context_id=f"forget:{self._entity_id}",
            )

        log.info("Entity %s: forget '%s' (was: %s)",
                 self._entity_id[:12], key, was)

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
        """Query what the agent currently knows about itself and its user.

        Args:
            query: Optional filter (empty = return all working memory)
            limit: Max results

        Returns:
            {"facts": {...}, "total": N}
        """
        facts = {}
        if self._entity_mgr:
            state = self._entity_mgr.get(self._entity_id)
            facts = dict(state.working_memory)

        if query:
            query_lower = query.lower()
            facts = {k: v for k, v in facts.items()
                     if query_lower in k.lower() or query_lower in v.lower()}

        # Limit
        items = list(facts.items())[:limit]
        facts_out = dict(items)
        # Human-readable output so extract_user_response / chat surfaces
        # can show facts without a second LLM turn (E4 continuity).
        if facts_out:
            output = "\n".join(f"{k}: {v}" for k, v in facts_out.items())
        else:
            output = ""

        return {
            "success": True,
            "facts": facts_out,
            "total": len(items),
            "output": output,
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
