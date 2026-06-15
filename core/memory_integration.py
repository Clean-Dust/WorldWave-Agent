"""
ww/core/memory_integration.py — Worldwave memory system integration v0.2

Integration layer (not external service):
1. MemorySystem (local hippocampus + amygdala + sleep) 
2. ContextWindow (short-term working memory, LLM summary compress)
3. SpiralContextSummarizer (spiral loop memory summary)

Replace old HTTP bridge to memory v2 (port 9200).
Memory system is now a first-class Python module in WW framework.
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from core.context import ContextWindow, SpiralContextSummarizer, estimate_tokens
from .memory import MemorySystem

logger = logging.getLogger("ww.memory")


class MemoryIntegrator:
    """
    Memory integration: unified access to ContextWindow + MemorySystem.

    Spiral loop usage:
        mem = MemoryIntegrator(llm=llm)
        mem.record_spiral(spiral_data)
        mem.record_action(action_data)
        context = mem.get_context(goal)
    """

    def __init__(
        self,
        llm=None,
        max_context_tokens: int = 16000,
        max_window_messages: int = 30,
        hippocampus_cap: int = 100,
        memory_data_dir: str = "",
        schedule_sleep_hour: int = 3,  # Daily 3:00 AM auto consolidation (-1=disabled)
    ):
        self.llm = llm
        self.max_context_tokens = max_context_tokens

        # ── MemorySystem (local, replaces HTTP v2) ──
        self.memory_system = MemorySystem(
            hippocampus_cap=hippocampus_cap,
            data_dir=memory_data_dir,
            schedule_sleep_hour=schedule_sleep_hour,
        )

        # Short-term context window (working memory)
        self.context_window = ContextWindow(
            max_messages=max_window_messages,
            max_tokens=max_context_tokens,
            compress_target=max_context_tokens // 2,
            llm=llm,
        )

        # Spiral summarizer (compresses long sessions)
        self.spiral_summarizer = SpiralContextSummarizer(
            llm=llm,
            max_spirals_before_compress=15,
        )

        # Session tracking
        self.session_id = f"session_{int(time.time())}"
        self.spiral_count = 0
        self._pending_spirals: List[Dict] = []

    # ── Context & conflict metrics (for subconscious feature vector) ──

    def get_context_pressure(self) -> float:
        """Context window usage ratio 0.0-1.0."""
        total = self.context_window.total_tokens()
        max_tok = self.context_window.max_tokens
        if max_tok <= 0:
            return 0.0
        return min(1.0, total / max_tok)

    def get_memory_conflict_rate(self) -> float:
        """Memory conflict rate based on archived/synthesized atoms ratio 0.0-1.0."""
        try:
            atoms = self.memory_system.hippocampus.all()
            total = len(atoms)
            if total == 0:
                return 0.0
            archived = sum(1 for a in atoms if getattr(a, 'is_archived', False))
            return min(1.0, archived / total)
        except Exception:
            return 0.0

    # ── Record methods ──

    def record_spiral(self, spiral_data: Dict) -> None:
        """Record a spiral loop result"""
        self.spiral_count += 1
        self._pending_spirals.append(spiral_data)

        # Compress if needed
        if self.spiral_summarizer.should_compress(len(self._pending_spirals)):
            self._compress_pending()

        # Store to context window
        summary = self._summarize_spiral(spiral_data)
        self.context_window.add("assistant", summary, metadata={
            "type": "spiral", "number": spiral_data.get("spiral_number", self.spiral_count)
        })

        # Store to local MemorySystem
        self.memory_system.store(
            content=summary,
            source="ww_loop",
            context_id=f"spiral_{self.spiral_count}",
            tags=["spiral", "ww"],
        )

    def record_action(self, action_data: Dict) -> None:
        """Record a linear action result"""
        tool = action_data.get("tool", "?")
        success = action_data.get("success", False)
        status = "✓" if success else "✗"
        summary = f"[Linear action] {status} tool={tool} params={action_data.get('params', {})}"
        self.context_window.add("tool", summary, metadata={
            "type": "action", "tool": tool, "success": success,
        })

        # success/failedrecordto  MemorySystem
        if success:
            self.memory_system.store_success(summary)
        else:
            self.memory_system.store(
                content=summary,
                source="tool",
                urgency=0.5,
            )

    def record_user_message(self, content: str) -> None:
        """recordusermessage"""
        self.context_window.add("user", content)

    def record_assistant_response(self, content: str) -> None:
        """Record assistant reply"""
        self.context_window.add("assistant", content[:200])

    # ── Recall methods ──

    def get_context(self, goal: str = "") -> str:
        """getcomplete contexttext (for  LLM context) """
        parts = []

        # Current goal
        if goal:
            parts.append(f"when  goal: {goal}")

        # Context window -> LLM messages
        messages = self.context_window.to_llm_messages(system_prompt=goal)

        # Add MemorySystem recall (local, no HTTP)
        if goal:
            memories = self.memory_system.recall(goal, top_k=5)
            if memories and memories.get("results"):
                lines = []
                for r in memories["results"][:5]:
                    atom = r.get("atom", {})
                    content = atom.get("content", "")[:100]
                    salience = r.get("salience", 0)
                    lines.append(f"  • [{salience}] {content}")
                if lines:
                    parts.append("Related memories:\n" + "\n".join(lines))

        # Current conversation
        for msg in messages:
            role_map = {"user": "user", "assistant": "Assistant", "tool": "tool", "system": "system"}
            label = role_map.get(msg["role"], msg["role"])
            parts.append(f"[{label}]\n{msg['content'][:300]}")

        return "\n\n".join(parts)

    def recall_similar(self, query: str, limit: int = 5) -> List[Dict]:
        """Recall similar memories from memory system (local, no HTTP)."""
        result = self.memory_system.recall(query, top_k=limit)
        return result.get("results", [])

    def get_stats(self) -> Dict:
        """Memory system statistics"""
        mem_status = self.memory_system.overall_status()
        return {
            "session_id": self.session_id,
            "spirals": self.spiral_count,
            "context_tokens": self.context_window.total_tokens(),
            "context_messages": len(self.context_window.messages),
            "compressed_blocks": len(self.context_window.compressed),
            "pending_spirals": len(self._pending_spirals),
            "memory_system": mem_status,
        }

    # ── Internal ──

    def _summarize_spiral(self, spiral: Dict) -> str:
        """Single spiral summary"""
        num = spiral.get("spiral_number", "?")
        plan = spiral.get("plan", {})
        eval_ = spiral.get("evaluation", {})

        steps = [s.get("description", "?")[:50] for s in plan.get("steps", [])[:3]]
        success = eval_.get("success", False)
        reason = str(eval_.get("reason", ""))[:100]

        return (
            f"Spiral#{num} {'✅' if success else '❌'} "
            f"steps=[{'; '.join(steps)}] "
            f"result={reason}"
        )

    def _compress_pending(self):
        """Compress pending spiral summaries"""
        if not self._pending_spirals:
            return

        summary = self.spiral_summarizer.summarize_spirals(self._pending_spirals)
        self.context_window.add("system", summary, metadata={"type": "spiral_summary"})
        self._pending_spirals.clear()
