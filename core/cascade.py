"""
ww/core/cascade.py — Cross-Module Cascade Signaling v0.1

Biomimetic cascade signaling: amygdala valence → thalamus + basal ganglia.

In the biological brain, the amygdala's emotional valence signals cascade
through multiple brain regions simultaneously:
- High stress → thalamus tightens attention filtering (focus on threats)
- High stress → basal ganglia raises inhibition threshold (more cautious actions)
- High stress → autonomic nervous system accelerates heartbeat

This module provides the software equivalent: a signaling bus that
the amygdala broadcasts on, and other modules subscribe to.

Architecture:
    Amygdala (core/memory/amygdala.py)
        │  compute_urgency() / compute_penalty() / stress level
        ▼
    CascadeBus (this module)
        │  routes signals to all subscribers
        ├──→ BayesianAttentionGate (gateway/attention.py)
        │      └─ set_stress_level() → raises filter threshold
        ├──→ BasalGanglia (core/subconscious/basal_ganglia.py)
        │      └─ set_stress_level() → raises caution lambda
        ├──→ CircadianRhythm (core/circadian.py)
        │      └─ SystemMetrics.stress_signal → accelerates heartbeat
        └──→ GlobalWorkspace (core/global_workspace.py)
               └─ set_filter_intensity() → stricter workspace gating

Pure Python, zero external dependencies.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class CascadeSignal:
    """A signal emitted by one brain module for consumption by others."""
    source: str               # Emitting module (e.g., "amygdala")
    signal_type: str          # Signal type (e.g., "stress", "reward", "alert")
    value: float              # Signal intensity [0, 1]
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class CascadeBus:
    """Cross-module signaling bus for the biomimetic brain architecture.

    Modules publish signals; interested modules subscribe to specific
    signal types. This decouples the modules — amygdala doesn't need to
    know about gateway or basal ganglia.

    Usage:
        bus = CascadeBus()
        bus.subscribe("stress", gateway.set_stress_level)
        bus.subscribe("stress", basal_ganglia.set_stress_level)
        # Amygdala emits:
        bus.emit(CascadeSignal(source="amygdala", signal_type="stress", value=0.8))
    """

    def __init__(self, name: str = "default"):
        self.name = name
        self._subscribers: Dict[str, List[Callable]] = {}
        self._signal_history: List[CascadeSignal] = []
        self._max_history = 50
        self._emit_count: int = 0
        self._error_count: int = 0

    # ── Subscription ──

    def subscribe(self, signal_type: str, callback: Callable):
        """Register a callback for a specific signal type.

        callback receives (CascadeSignal) and should not raise.
        """
        if signal_type not in self._subscribers:
            self._subscribers[signal_type] = []
        if callback not in self._subscribers[signal_type]:
            self._subscribers[signal_type].append(callback)

    def unsubscribe(self, signal_type: str, callback: Callable):
        """Remove a subscription."""
        if signal_type in self._subscribers:
            self._subscribers[signal_type] = [
                cb for cb in self._subscribers[signal_type] if cb != callback
            ]

    # ── Wildcard subscription ──

    def subscribe_all(self, callback: Callable):
        """Subscribe to ALL signal types."""
        self.subscribe("*", callback)

    # ── Emission ──

    def emit(self, signal: CascadeSignal) -> int:
        """Emit a signal to all subscribers.

        Returns number of callbacks invoked.
        Supports both type-specific and wildcard (*) subscribers.
        """
        self._signal_history.append(signal)
        if len(self._signal_history) > self._max_history:
            self._signal_history = self._signal_history[-self._max_history // 2:]

        self._emit_count += 1
        invoked = 0

        # Type-specific subscribers
        for cb in self._subscribers.get(signal.signal_type, []):
            try:
                cb(signal.value)
                invoked += 1
            except Exception:
                self._error_count += 1

        # Wildcard subscribers
        for cb in self._subscribers.get("*", []):
            try:
                cb(signal)
                invoked += 1
            except Exception:
                self._error_count += 1

        return invoked

    def emit_stress(self, level: float, source: str = "amygdala",
                    reason: str = ""):
        """Convenience: emit a stress signal."""
        return self.emit(CascadeSignal(
            source=source,
            signal_type="stress",
            value=max(0.0, min(1.0, level)),
            metadata={"reason": reason},
        ))

    def emit_reward(self, level: float, source: str = "amygdala",
                    reason: str = ""):
        """Convenience: emit a reward signal."""
        return self.emit(CascadeSignal(
            source=source,
            signal_type="reward",
            value=max(0.0, min(1.0, level)),
            metadata={"reason": reason},
        ))

    def emit_alert(self, level: float, source: str = "system",
                   reason: str = ""):
        """Convenience: emit an alert signal."""
        return self.emit(CascadeSignal(
            source=source,
            signal_type="alert",
            value=max(0.0, min(1.0, level)),
            metadata={"reason": reason},
        ))

    # ── Query ──

    def recent_signals(self, signal_type: Optional[str] = None,
                       limit: int = 10) -> List[CascadeSignal]:
        """Get recent signals, optionally filtered by type."""
        signals = self._signal_history
        if signal_type:
            signals = [s for s in signals if s.signal_type == signal_type]
        return signals[-limit:]

    def current_stress_level(self) -> float:
        """Get the most recent stress signal value (or 0.0)."""
        stress_signals = [s for s in self._signal_history
                          if s.signal_type == "stress"]
        if stress_signals:
            # Weighted by recency
            now = time.time()
            total_weight = 0.0
            weighted_sum = 0.0
            for s in stress_signals:
                age = now - s.timestamp
                weight = max(0.1, 1.0 - age / 300)  # Decay over 5 min
                weighted_sum += s.value * weight
                total_weight += weight
            return weighted_sum / max(total_weight, 0.01)
        return 0.0

    # ── Stats ──

    def stats(self) -> Dict:
        return {
            "name": self.name,
            "subscribers": {
                stype: len(cbs) for stype, cbs in self._subscribers.items()
            },
            "total_emitted": self._emit_count,
            "total_errors": self._error_count,
            "history_size": len(self._signal_history),
            "current_stress": round(self.current_stress_level(), 3),
        }

    def clear_history(self):
        self._signal_history = []
        self._emit_count = 0
        self._error_count = 0


# ── Integration helper: wire up the cascade ──

def wire_biomimetic_cascade(
    bus: CascadeBus,
    attention_gate: Any = None,
    basal_ganglia: Any = None,
    circadian_rhythm: Any = None,
    global_workspace: Any = None,
):
    """Wire up all biomimetic modules to the cascade bus.

    This is the single integration point that connects the amygdala's
    output to all downstream modules.

    Args:
        bus: The CascadeBus instance
        attention_gate: BayesianAttentionGate (has set_stress_level)
        basal_ganglia: BasalGanglia (has set_stress_level)
        circadian_rhythm: CircadianRhythm (accepts stress in SystemMetrics)
        global_workspace: GlobalWorkspace (has set_filter_intensity)
    """
    if attention_gate and hasattr(attention_gate, 'set_stress_level'):
        bus.subscribe("stress", attention_gate.set_stress_level)

    if basal_ganglia and hasattr(basal_ganglia, 'set_stress_level'):
        bus.subscribe("stress", basal_ganglia.set_stress_level)

    if global_workspace and hasattr(global_workspace, 'set_filter_intensity'):
        bus.subscribe("stress", global_workspace.set_filter_intensity)

    # Circadian rhythm receives stress via SystemMetrics, not directly
    # But we can store the latest stress value for the rhythm to poll
    if circadian_rhythm:
        def _update_rhythm_stress(value: float):
            # Non-destructive: rhythm polls this via its own metrics collection
            pass  # Handled by collect_system_metrics(stress_signal=...)
        bus.subscribe("stress", lambda v: None)  # Placeholder for monitoring

    return bus


# ── Singleton ──

_default_bus: Optional[CascadeBus] = None


def get_cascade_bus() -> CascadeBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = CascadeBus()
    return _default_bus
