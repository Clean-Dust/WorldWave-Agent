"""Streaming Interrupt Controller — confidence-driven generation halting.

State machine that monitors tokens as they are generated and decides
when to call for an interrupt. The interrupt callback (registered via
BackendPlugin.register_interrupt_callback) calls should_interrupt()
before each generation step.

When an interrupt fires:
  1. Generation pauses
  2. The subconscious runs a corrective action (e.g., change parameters,
     inject control tokens, trigger re-planning)
  3. Generation resumes

States:
  IDLE        — No interruption monitoring active
  MONITORING  — Monitoring generation in real-time
  STALLED     — Generation paused (interrupt fired)
  RESOLVED    — Issue was resolved, ready to resume

Pure Python, zero external dependencies. Default-disabled.
"""

from __future__ import annotations
import json
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple


class InterruptState:
    """Symbolic states for the interrupt controller."""
    IDLE = "idle"
    MONITORING = "monitoring"
    STALLED = "stalled"
    RESOLVED = "resolved"


class InterruptController:
    """Real-time generation interrupt controller.

    Uses a combination of probe signals and risk scores to decide
    when to interrupt. Configurable thresholds allow tuning.

    Architecture:
      - Before each generation step, the backend calls should_interrupt()
      - If True, the controller enters STALLED state
      - The owner (subconscious/loop) calls resolve() with new params
      - The controller enters RESOLVED, allowing generation to resume
      - If generation completes without interrupt, it's a clean run
    """

    def __init__(
        self,
        interrupt_risk_threshold: float = 0.85,
        entropy_spike_threshold: float = 0.8,
        max_consecutive_interrupts: int = 3,
        cooldown_steps: int = 5,
        confidence_min: float = 0.3,
    ):
        self.interrupt_risk_threshold = interrupt_risk_threshold
        self.entropy_spike_threshold = entropy_spike_threshold
        self.max_consecutive_interrupts = max_consecutive_interrupts
        self.cooldown_steps = cooldown_steps
        self.confidence_min = confidence_min

        # State
        self._state = InterruptState.IDLE
        self._interrupt_count = 0  # in current task
        self._total_interrupts = 0  # lifetime
        self._steps_since_resume = 0
        self._reason: Optional[str] = None
        self._last_risk: float = 0.0
        self._last_entropy: float = 0.5

    # ── Lifecycle ──

    def start_monitoring(self) -> None:
        """Begin monitoring generation."""
        self._state = InterruptState.MONITORING
        self._interrupt_count = 0
        self._steps_since_resume = 0

    def stop_monitoring(self) -> None:
        """Stop monitoring (generation complete)."""
        if self._state != InterruptState.STALLED:
            self._state = InterruptState.IDLE
        # If stalled, don't reset — owner needs to resolve first

    def resolve(self, new_params: Optional[Dict[str, Any]] = None) -> None:
        """Resolve an interrupt and prepare to resume.

        Args:
            new_params: Optional parameter overrides (temperature, etc.)
                for the resumed generation.
        """
        self._state = InterruptState.RESOLVED
        self._steps_since_resume = 0
        # Owner should resume generation after calling this

    # ── Core Decision ──

    def should_interrupt(
        self,
        risk_score: float,
        token_entropy: float = 0.5,
        attention_sparsity: float = 0.5,
        logit_magnitude: float = 0.5,
        hidden_state_norm: float = 0.5,
        gate_confidence: float = 0.5,
    ) -> Tuple[bool, str]:
        """Called before each generation step. Returns (interrupt, reason).

        Combines multiple signals for the decision:
          1. Risk score > threshold → high likelihood of error
          2. Token entropy spike → model is uncertain
          3. Attention sparsity drop → model is scattered
          4. Logit magnitude dip → model lacks confidence
          5. Running steps since last interrupt (cooldown)
          6. Total interrupts in this task (cap)
        """
        self._last_risk = risk_score
        self._last_entropy = token_entropy

        # Check interrupt cap
        if self._interrupt_count >= self.max_consecutive_interrupts:
            return (False, "max_interrupts_reached")

        # Check cooldown
        if self._steps_since_resume < self.cooldown_steps:
            self._steps_since_resume += 1
            return (False, "cooldown")

        # Signal 1: High risk
        risk_flag = risk_score > self.interrupt_risk_threshold

        # Signal 2: Entropy spike (uncertainty)
        entropy_flag = token_entropy > self.entropy_spike_threshold

        # Signal 3: Attention collapse (very sparse or very dense)
        attention_flag = attention_sparsity > 0.9 or attention_sparsity < 0.05

        # Signal 4: Logit magnitude collapse
        logit_flag = logit_magnitude < 0.15

        # Signal 5: Gate confidence is low
        confidence_flag = gate_confidence < self.confidence_min

        # Decision logic: at least 2 signals must fire
        signals = sum([risk_flag, entropy_flag,
                       attention_flag, logit_flag, confidence_flag])

        if signals >= 2:
            self._state = InterruptState.STALLED
            self._interrupt_count += 1
            self._total_interrupts += 1

            # Build reason
            parts: List[str] = []
            if risk_flag:
                parts.append(f"risk={risk_score:.2f}")
            if entropy_flag:
                parts.append(f"entropy={token_entropy:.2f}")
            if attention_flag:
                parts.append(f"attn={attention_sparsity:.2f}")
            if logit_flag:
                parts.append(f"logit={logit_magnitude:.2f}")
            if confidence_flag:
                parts.append(f"conf={gate_confidence:.2f}")
            self._reason = f"interrupt({signals}/5): " + ", ".join(parts)
            return (True, self._reason)

        self._steps_since_resume += 1
        return (False, "ok")

    # ── Feature Contributions ──

    def fill_interrupt_features(
        self, features: List[float],
        base_index: int = 22,
    ) -> None:
        """Fill feature dimensions with interrupt-related signals.

        Uses features[22] = interrupt trigger level (0=idle, 1=stalled)
        and features[23] = interrupt count normalized.
        """
        if base_index >= len(features):
            return

        # feature[22]: interrupt state as a continuous value
        state_map = {
            InterruptState.IDLE: 0.0,
            InterruptState.MONITORING: 0.3,
            InterruptState.RESOLVED: 0.6,
            InterruptState.STALLED: 1.0,
        }
        features[base_index] = state_map.get(self._state, 0.0)

        # feature[23]: normalized interrupt count for this task
        if base_index + 1 < len(features):
            if self.max_consecutive_interrupts > 0:
                features[base_index + 1] = (
                    self._interrupt_count / self.max_consecutive_interrupts
                )

    # ── Properties ──

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_stalled(self) -> bool:
        return self._state == InterruptState.STALLED

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def interrupt_count(self) -> int:
        return self._interrupt_count

    @property
    def total_interrupts(self) -> int:
        return self._total_interrupts

    # ── Save / Load ──

    def save(self, path: str) -> None:
        data = {
            "total_interrupts": self._total_interrupts,
            "thresholds": {
                "interrupt_risk_threshold": self.interrupt_risk_threshold,
                "entropy_spike_threshold": self.entropy_spike_threshold,
                "max_consecutive_interrupts": self.max_consecutive_interrupts,
                "cooldown_steps": self.cooldown_steps,
                "confidence_min": self.confidence_min,
            },
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

    @classmethod
    def load(cls, path: str, **overrides) -> "InterruptController":
        with open(path) as f:
            data = json.load(f)
        th = data.get("thresholds", {})
        return cls(
            interrupt_risk_threshold=overrides.get(
                "interrupt_risk_threshold",
                th.get("interrupt_risk_threshold", 0.85)),
            entropy_spike_threshold=overrides.get(
                "entropy_spike_threshold",
                th.get("entropy_spike_threshold", 0.8)),
            max_consecutive_interrupts=overrides.get(
                "max_consecutive_interrupts",
                th.get("max_consecutive_interrupts", 3)),
            cooldown_steps=overrides.get(
                "cooldown_steps",
                th.get("cooldown_steps", 5)),
            confidence_min=overrides.get(
                "confidence_min",
                th.get("confidence_min", 0.3)),
        )
