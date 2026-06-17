"""
ww/core/global_workspace.py — Global Workspace v0.1

Biomimetic Global Workspace (GWT-inspired):
- LLM context is treated as a scarce, precious resource
- Strict capacity limit on high-level abstract items (default 7)
- All information must pass through attention scoring before reaching cortex
- Priority-based eviction when capacity exceeded

Inspired by Baars' Global Workspace Theory and the Gemini biomimetic blueprint:
the prefrontal cortex (LLM) should only receive pre-filtered, high-value
information, not raw sensory flood.
"""

from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Defaults ──
DEFAULT_CAPACITY = 7          # "7 high-level abstract items" from blueprint
DEFAULT_MIN_PRIORITY = 0.15   # Below this, items are rejected entirely
PRIORITY_DECAY_HALF_LIFE = 300  # 5 minutes — stale items decay


@dataclass
class WorkspaceItem:
    """A single item competing for space in the global workspace.

    Each item has:
    - content: the actual text/content
    - source: where it came from (e.g., 'gateway', 'memory', 'tool_output')
    - priority: computed attention score [0, 1]
    - timestamp: when it entered the workspace
    - token_estimate: approximate token count
    - immutable: if True, cannot be evicted (use sparingly)
    """
    content: str
    source: str = "unknown"
    priority: float = 0.5
    timestamp: float = field(default_factory=time.time)
    token_estimate: int = 0
    immutable: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.token_estimate == 0:
            self.token_estimate = max(1, len(self.content) // 4)


class GlobalWorkspace:
    """Biomimetic global workspace with strict capacity limits.

    Models the prefrontal cortex's working memory:
    - Limited capacity (default 7 items)
    - Competitive access: only highest-priority items enter
    - Automatic decay: stale items lose priority over time
    - Eviction: lowest-priority items removed when capacity exceeded
    - Attention gating: items below min_priority are rejected

    Usage:
        ws = GlobalWorkspace(capacity=7)
        ws.submit("Critical error in database", source="gateway", priority=0.9)
        ws.submit("Health check OK", source="gateway", priority=0.1)
        context = ws.to_context_string()
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        min_priority: float = DEFAULT_MIN_PRIORITY,
        decay_halflife: float = PRIORITY_DECAY_HALF_LIFE,
        workspace_id: str = "",
    ):
        self.capacity = capacity
        self.min_priority = min_priority
        self.decay_halflife = decay_halflife
        self.workspace_id = workspace_id or f"ws_{int(time.time())}"
        self._items: List[WorkspaceItem] = []
        self._evicted_count: int = 0
        self._rejected_count: int = 0
        self._total_submitted: int = 0

        # Cross-module signals (written by amygdala, read by loop)
        self.cascade_signals: Dict[str, Any] = {}
        self.filter_intensity: float = 0.5  # 0=loose, 1=strict (amygdala can raise this)
        self._last_cleanup: float = time.time()

    # ── Priority computation ──

    def compute_priority(
        self,
        content: str,
        source: str = "unknown",
        relevance_to_goal: float = 0.5,
        urgency: float = 0.0,
        novelty: float = 0.0,
    ) -> float:
        """Compute attention priority score for a potential workspace item.

        Formula:
            priority = relevance * 0.4 + urgency * 0.3 + novelty * 0.2 + source_weight * 0.1

        source_weight depends on source type:
            error/tool_failure = 0.9
            user_command = 0.8
            memory_recall = 0.6
            health_check = 0.2
            unknown = 0.4
        """
        source_weights = {
            "error": 0.9, "tool_failure": 0.9, "crash": 1.0,
            "user_command": 0.8, "user_message": 0.75,
            "memory_recall": 0.6, "subconscious_alert": 0.7,
            "tool_output": 0.5, "gateway": 0.5,
            "health_check": 0.2, "heartbeat": 0.15,
            "system": 0.3, "unknown": 0.4,
        }
        sw = source_weights.get(source, 0.4)

        priority = (
            relevance_to_goal * 0.4
            + urgency * 0.3
            + novelty * 0.2
            + sw * 0.1
        )
        return max(0.0, min(1.0, priority))

    # ── Core operations ──

    def submit(
        self,
        content: str,
        source: str = "unknown",
        priority: float = 0.5,
        immutable: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        relevance: float = 0.5,
        urgency: float = 0.0,
        novelty: float = 0.0,
    ) -> Optional[WorkspaceItem]:
        """Submit an item to the global workspace.

        Returns the item if accepted, None if rejected (below min_priority).
        May trigger eviction of lower-priority items if capacity exceeded.
        """
        self._total_submitted += 1

        # Auto-compute priority if not explicitly provided
        if priority == 0.5 and (relevance != 0.5 or urgency != 0.0 or novelty != 0.0):
            priority = self.compute_priority(content, source, relevance, urgency, novelty)

        # Apply amygdala-driven filter intensity
        effective_min = self.min_priority + (self.filter_intensity - 0.5) * 0.3
        effective_min = max(0.05, min(0.5, effective_min))

        # Gate: reject items below threshold (unless immutable)
        if not immutable and priority < effective_min:
            self._rejected_count += 1
            return None

        item = WorkspaceItem(
            content=content,
            source=source,
            priority=priority,
            immutable=immutable,
            metadata=metadata or {},
        )

        self._items.append(item)
        self._enforce_capacity()
        return item

    def _enforce_capacity(self):
        """Evict lowest-priority non-immutable items if over capacity."""
        if len(self._items) <= self.capacity:
            return

        # Separate immutable from evictable
        evictable = [i for i in self._items if not i.immutable]
        immutable = [i for i in self._items if i.immutable]

        if len(immutable) >= self.capacity:
            # All slots taken by immutable items — force-evict oldest immutable
            immutable.sort(key=lambda x: x.timestamp)
            overflow = len(self._items) - self.capacity + 1
            evicted = immutable[:overflow]
            self._items = immutable[overflow:] + evictable
        else:
            # Normal eviction: sort evictable by (priority, recency)
            evictable.sort(key=lambda x: (x.priority, -x.timestamp))
            slots_for_evictable = self.capacity - len(immutable)
            keep = evictable[-slots_for_evictable:] if slots_for_evictable > 0 else []
            self._items = immutable + keep

        self._evicted_count += 1

    def apply_decay(self, now: Optional[float] = None):
        """Decay priorities of all items based on time since entry.

        Called periodically (e.g., each spiral cycle).
        Stale items become easier to evict.
        """
        now = now or time.time()
        lam = math.log(2) / self.decay_halflife
        for item in self._items:
            if item.immutable:
                continue
            age = now - item.timestamp
            item.priority *= math.exp(-lam * age)

        # After decay, re-enforce capacity
        self._enforce_capacity()
        self._last_cleanup = now

    # ── Query ──

    def get_top_items(self, n: Optional[int] = None) -> List[WorkspaceItem]:
        """Return highest-priority items (up to n, or all)."""
        sorted_items = sorted(self._items, key=lambda x: -x.priority)
        if n:
            return sorted_items[:n]
        return sorted_items

    def to_context_string(self, max_items: Optional[int] = None) -> str:
        """Convert workspace to a context string for LLM system prompt.

        Formats items as a structured attention report.
        """
        items = self.get_top_items(max_items or self.capacity)
        if not items:
            return ""

        lines = ["[Global Workspace — Active Context Items]"]
        for i, item in enumerate(items):
            priority_bar = "█" * int(item.priority * 10) + "░" * (10 - int(item.priority * 10))
            lines.append(
                f"  [{i+1}] [{priority_bar}] ({item.source}) {item.content[:200]}"
            )
        return "\n".join(lines)

    def to_list(self) -> List[Dict]:
        """Serializable representation for API/debugging."""
        return [
            {
                "content": item.content[:200],
                "source": item.source,
                "priority": round(item.priority, 3),
                "tokens": item.token_estimate,
                "age_seconds": round(time.time() - item.timestamp, 1),
                "immutable": item.immutable,
            }
            for item in self.get_top_items()
        ]

    # ── Cross-module interface ──

    def set_filter_intensity(self, intensity: float):
        """Called by amygdala to raise/lower attention gate threshold.

        High stress → high intensity → stricter filtering.
        Low stress → low intensity → more permissive.
        """
        self.filter_intensity = max(0.0, min(1.0, intensity))

    def receive_cascade(self, signal_type: str, payload: Any):
        """Receive cascade signal from other brain modules (amygdala, etc.)."""
        self.cascade_signals[signal_type] = {
            "value": payload,
            "timestamp": time.time(),
        }

    # ── Stats ──

    def stats(self) -> Dict:
        return {
            "workspace_id": self.workspace_id,
            "capacity": self.capacity,
            "current_items": len(self._items),
            "utilization": round(len(self._items) / self.capacity, 2),
            "total_submitted": self._total_submitted,
            "rejected": self._rejected_count,
            "evicted": self._evicted_count,
            "filter_intensity": round(self.filter_intensity, 3),
            "top_priorities": [round(i.priority, 2) for i in self.get_top_items(3)],
            "cascade_signals": list(self.cascade_signals.keys()),
        }

    def clear(self):
        """Reset workspace (e.g., new session)."""
        self._items = []
        self.cascade_signals = {}
        self.filter_intensity = 0.5


# ── Factory ──

def create_workspace(capacity: int = DEFAULT_CAPACITY, **kwargs) -> GlobalWorkspace:
    return GlobalWorkspace(capacity=capacity, **kwargs)
