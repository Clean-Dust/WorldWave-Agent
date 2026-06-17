"""
ww/core/context.py — Worldwave contextmanagement v0.2

LLM-driven context compression system:
- Exceeds token limit auto-compress history
- Use LLM for semantic summary (not simple truncation)
- Spirals context association
- Token budget trace
"""

from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Dict, List


# ── defaultvalue ──
DEFAULT_MAX_MESSAGES = 30
DEFAULT_MAX_TOKENS = 32000        # Trigger compress token limit
DEFAULT_COMPRESS_TARGET = 12000   # Compress target token count
COMPRESSION_WARN_THRESHOLD = 0.85  # Reach this ratio to warn


# ── Token estimation (~4 chars/token) ──
def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 10


class ContextMessage:
    """A conversation message (with token estimation)

    immutable=True message at compress will be skipped, preserving original content.
    Suitable for Computer Use coordinates, file paths and other precise references.
    """
    def __init__(self, role: str, content: str, metadata: Dict = None,
                 timestamp: str = "", token_estimate: int = 0,
                 immutable: bool = False,
                 immutable_scope: str = ""):
        self.role = role
        self.content = content
        self.metadata = metadata or {}
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        self.tokens = token_estimate or estimate_tokens(content)
        self.immutable = immutable  # True = compress skip
        self.immutable_scope = immutable_scope  # scope marker, for TTL auto-clear

    def to_dict(self) -> Dict:
        return {
            "role": self.role,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "tokens": self.tokens,
        }

    def __repr__(self):
        icon = {"user": "🧑", "assistant": "🤖", "system": "⚙", "tool": "🔧"}
        return f"<{icon.get(self.role, '❓')} {self.content[:40]}... ({self.tokens}t)>"


class CompressedBlock:
    """A compress history (retains semantic summary, not original text)"""
    def __init__(self, summary: str, original_count: int, original_tokens: int,
                 timestamp: str = ""):
        self.summary = summary
        self.original_count = original_count
        self.original_tokens = original_tokens
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()

    def to_context_block(self) -> str:
        return (
            f"〔history summary: original {self.original_count} messages / "
            f"{self.original_tokens} tokens〕\n{self.summary}"
        )


class ContextWindow:
    """
    contextwindow v0.2 — LLM-driven compress

    management multi-turn conversation messagelist:
    - auto-detect token threshold triggers compress
    - use LLM for semantic summary instead of truncation
    - supports multi-layer compress (multiple summary stacking)
    """

    def __init__(
        self,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        compress_target: int = DEFAULT_COMPRESS_TARGET,
        window_id: str = "",
        llm=None,  # LLMClient for compression
    ):
        self.messages: List[ContextMessage] = []
        self.compressed: List[CompressedBlock] = []  # compress history blocks
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self.compress_target = compress_target
        self.window_id = window_id or f"win_{int(time.time())}"
        self.llm = llm  # optional LLM client (for semantic compress)
        self.compress_count = 0
        self.total_tokens_in = 0  # cumulative input token

    def add(self, role: str, content: str, immutable: bool = False,
            immutable_scope: str = "", **kwargs) -> ContextMessage:
        """Add a new message, auto trigger compress check.

        If immutable_scope is provided, auto clear old immutable markers outside the same scope,
        to prevent Context Window from being bloated by invalid coordinate/path information.
        """
        if immutable and immutable_scope:
            # Clear old messages of the same scope, so they can be compressed
            self._clear_immutable_scope(immutable_scope)
        msg = ContextMessage(role=role, content=content, immutable=immutable,
                             immutable_scope=immutable_scope, **kwargs)
        self.messages.append(msg)
        self.total_tokens_in += msg.tokens
        self._check_compress()
        return msg

    def _check_compress(self):
        """check if compress is needed, if so execute"""
        total = self.total_tokens()
        over_msg = len(self.messages) > self.max_messages
        over_tok = total > self.max_tokens

        if over_msg or over_tok:
            self._compress()

    def _clear_immutable_scope(self, current_scope: str):
        """Clear immutable markers that do not belong to current_scope.

        When a new micro-task starts, a task coordinate/path
        becomes invalid. Clear their immutable markers so compress can reclaim.
        """
        for m in self.messages:
            if m.immutable and m.immutable_scope and m.immutable_scope != current_scope:
                m.immutable = False

    def _compress(self):
        """executecompress: summary of the oldest compressible messages (skip immutable)"""
        if len(self.messages) < 4:
            return  # too few messages, no compress

        compressable = [m for m in self.messages if not m.immutable]
        if len(compressable) < 3:
            return  # no enough compressible messages

        compress_count = max(3, len(compressable) // 2)
        old_msgs = compressable[:compress_count]
        old_tokens = sum(m.tokens for m in old_msgs)

        # Try LLM summarization
        summary = self._llm_summarize(old_msgs) if self.llm else self._simple_summary(old_msgs)

        block = CompressedBlock(
            summary=summary,
            original_count=len(old_msgs),
            original_tokens=old_tokens,
        )
        self.compressed.append(block)
        # Only delete compressed messages (keep immutable)
        old_ids = set(id(m) for m in old_msgs)
        self.messages = [m for m in self.messages if id(m) not in old_ids]
        self.compress_count += 1

    def _simple_summary(self, messages: List[ContextMessage]) -> str:
        """No LLM, simple summary (maintain backward compatibility)"""
        parts = []
        for m in messages:
            content = m.content[:150]
            parts.append(f"[{m.role}] {content}")
        return "(simple compress)\n" + "\n".join(parts)

    def _llm_summarize(self, messages: List[ContextMessage]) -> str:
        """Use LLM for semantic summary"""
        if not self.llm:
            return self._simple_summary(messages)

        # Build conversation text
        lines = []
        for m in messages:
            icon = {"user": "User", "assistant": "Assistant", "tool": "Tool", "system": "System"}
            label = icon.get(m.role, m.role)
            lines.append(f"{label}: {m.content[:500]}")
        text = "\n\n".join(lines)

        try:
            summary = self.llm.chat(
                messages=[{"role": "user", "content": (
                    f"Please use traditional Chinese to summarize the conversation. Keep all key decisions, tool call results, and important discoveries."
                    f"Concise but complete:\n\n{text}"
                )}],
                phase="",
                json_mode=False,
                temperature=0.3,
                max_tokens=500,
            )
            return summary[:1000]
        except Exception:
            return self._simple_summary(messages)

    def total_tokens(self) -> int:
        """When total tokens in window (messages + compressed summary)"""
        msg_tokens = sum(m.tokens for m in self.messages)
        comp_tokens = sum(
            estimate_tokens(b.to_context_block()) for b in self.compressed
        )
        return msg_tokens + comp_tokens

    def to_llm_messages(self, system_prompt: str = "") -> List[Dict]:
        """Convert to message list for LLM API"""
        result = []

        # System = prompt + all compressed history
        sys_parts = [system_prompt] if system_prompt else []
        for block in self.compressed:
            sys_parts.append(block.to_context_block())

        if sys_parts:
            result.append({"role": "system", "content": "\n\n".join(sys_parts)})

        # Current messages
        for msg in self.messages:
            result.append({"role": msg.role, "content": msg.content})

        return result

    def clear(self):
        """Clear context"""
        self.messages = []
        self.compressed = []
        self.compress_count = 0

    def stats(self) -> Dict:
        """Statistics information"""
        return {
            "window_id": self.window_id,
            "messages": len(self.messages),
            "compressed_blocks": len(self.compressed),
            "total_tokens": self.total_tokens(),
            "max_tokens": self.max_tokens,
            "compress_count": self.compress_count,
            "total_tokens_in": self.total_tokens_in,
            "compression_ratio": round(
                (1 - self.total_tokens() / max(self.total_tokens_in, 1)) * 100, 1
            ),
        }


class ConversationManager:
    """Multi-window conversation management v0.2"""

    def __init__(self, llm=None, default_max_messages: int = DEFAULT_MAX_MESSAGES,
                 default_max_tokens: int = DEFAULT_MAX_TOKENS):
        self._windows: Dict[str, ContextWindow] = {}
        self.llm = llm
        self.default_max_messages = default_max_messages
        self.default_max_tokens = default_max_tokens

    def get_or_create(self, window_id: str = "") -> ContextWindow:
        if not window_id:
            window_id = "default"
        if window_id not in self._windows:
            self._windows[window_id] = ContextWindow(
                max_messages=self.default_max_messages,
                max_tokens=self.default_max_tokens,
                window_id=window_id,
                llm=self.llm,
            )
        return self._windows[window_id]

    def add_message(self, role: str, content: str, window_id: str = "",
                    **kwargs) -> ContextMessage:
        win = self.get_or_create(window_id)
        return win.add(role, content, **kwargs)

    def to_llm_messages(self, window_id: str = "",
                        system_prompt: str = "") -> List[Dict]:
        win = self.get_or_create(window_id)
        return win.to_llm_messages(system_prompt)

    def clear_window(self, window_id: str = ""):
        win = self.get_or_create(window_id)
        win.clear()

    def delete_window(self, window_id: str):
        self._windows.pop(window_id, None)

    def list_windows(self) -> List[Dict]:
        return [w.stats() for w in self._windows.values()]

    def all_stats(self) -> Dict:
        windows = self.list_windows()
        return {
            "window_count": len(windows),
            "total_messages": sum(w["messages"] for w in windows),
            "total_tokens": sum(w["total_tokens"] for w in windows),
            "windows": windows,
        }


class SpiralContextSummarizer:
    """
    Spiral context summary 

    At long-running WW session, accumulated a large number of spiral results,
    Auto-summarize old spiral results, only keep semantic compress version.
    """

    def __init__(self, llm, max_spirals_before_compress: int = 20):
        self.llm = llm
        self.max_spirals = max_spirals_before_compress
        self.summaries: List[Dict] = []

    def should_compress(self, spiral_count: int) -> bool:
        return spiral_count > self.max_spirals

    def summarize_spirals(self, spirals: List[Dict]) -> str:
        """Batch summarize multiple spiral results"""
        if not spirals:
            return ""

        texts = []
        for s in spirals:
            num = s.get("spiral_number", "?")
            goal = str(s.get("perception", {}).get("environment_summary", ""))[:200]
            result = str(s.get("evaluation", {}).get("reason", ""))[:200]
            success = s.get("evaluation", {}).get("success", False)
            mark = "✅" if success else "❌"
            texts.append(f"Spiral#{num} {mark} Goal: {goal} | Result: {result}")

        text = "\n".join(texts)

        try:
            summary = self.llm.chat(
                messages=[{"role": "user", "content": (
                    f"Summarize the results of {len(spirals)} spiral loops."
                    f"Retrieve key progress, failed modes, and overall direction:\n\n{text}"
                )}],
                phase="", json_mode=False, temperature=0.3, max_tokens=500,
            )
        except Exception:
            summary = f"({len(spirals)} spirals compress: {len(texts)} records)"

        self.summaries.append({
            "spiral_range": f"{spirals[0].get('spiral_number', '?')}-{spirals[-1].get('spiral_number', '?')}",
            "count": len(spirals),
            "summary": summary,
        })
        return summary


def default_context_manager(llm=None) -> ConversationManager:
    return ConversationManager(llm=llm)
