"""
core/memory/tools.py — Self-editing memory tools

Gives the cognitive agent the ability to actively manage its own memory —
the agent transitions from "passive consumer of recall results" to
"active manager of its knowledge".

Two core tools:
- remember(key, value): Store a fact in memory. Agent calls this when it
  learns something new or detects a change.
- forget(key): Mark a fact as no longer valid. Superseded, not deleted.

These are the mechanism that makes entity continuity possible — the agent
can say "I'll remember that" and actually do it.

Integration:
- EntityStateManager: stores working memory (fast, always in context)
- MemorySystem: stores semantic facts (durable, recallable)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

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

    def set_entity(self, entity_id: str):
        """Set the current entity context (called before each interaction)."""
        self._entity_id = entity_id

    # ── remember ─────────────────────────────────────────────────

    def remember(self, key: str, value: str, category: str = "general") -> dict:
        """Store a fact in entity memory. Called by the agent when it learns something.

        Args:
            key: Short label for the fact (e.g., "user_name", "preferred_model")
            value: The fact content (e.g., "Chung", "deepseek-v4-pro")
            category: Optional category tag (general, preference, technical, etc.)

        Returns:
            {"status": "stored", "key": key, "previous": old_value or None}

        This is a SELF-EDITING operation — the agent decides what to remember.
        The old value (if any) is returned so the agent can confirm the update.
        """
        if not key or not value:
            return {"status": "error", "message": "key and value are required"}

        previous = None

        # 1. Store in entity working memory (always in context)
        if self._entity_mgr:
            state = self._entity_mgr.get(self._entity_id)
            previous = state.working_memory.get(key)
            self._entity_mgr.set_working_memory(self._entity_id, key, value)
            log.info("Entity %s: remember '%s' = '%s' (was: %s)",
                     self._entity_id[:12], key, value[:50], previous)

        # 2. Store in semantic memory (durable, recallable)
        if self._memory:
            fact_text = self._format_fact(key, value, category)
            self._memory.store_fact(
                fact=fact_text,
                entities=[key, category],
                context_id=f"remember:{self._entity_id}",
            )

        return {
            "status": "stored",
            "key": key,
            "previous": previous,
            "timestamp": time.time(),
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
            "status": "forgotten",
            "key": key,
            "was": was,
            "timestamp": time.time(),
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

        return {
            "facts": dict(items),
            "total": len(items),
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
                    "Example: remember(key='user_name', value='Chung')"
                ),
                "parameters": {
                    "key": {"type": "string", "description": "Short label for the fact"},
                    "value": {"type": "string", "description": "The fact content"},
                    "category": {"type": "string", "description": "Optional: general, preference, technical, contact, project"},
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
