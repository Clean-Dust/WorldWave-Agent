"""Control Gate: α weighting, mode switching, and fusion logic.

The control gate decides:
  1. α ∈ [0, 1] — how much prefix influence to blend with the model's
     own computation (0 = pure model, 1 = full prefix override).
  2. Mode switching — when to enter/exit "latent thinking" mode.
  3. Threshold decisions — when probe signals warrant action.

Architecture:
  features_vector (32 dims) → small MLP (32→16→4) →
    [α_split, α_diffuse, mode_logit, confidence]

Pure Python, zero external dependencies. Default-disabled.
"""

from __future__ import annotations
import json
import math
import os
import random
from typing import Dict, List, Optional, Tuple

# ── Modes ──


class GateMode:
    """Symbolic constants for control gate modes."""
    NORMAL = "normal"
    LATENT_THINKING = "latent_thinking"
    CONFIRM = "confirm"
    INTERRUPT = "interrupt"


# ── Math Helpers (standalone) ──


def _randn() -> float:
    """Box-Muller."""
    u1 = random.random()
    u2 = random.random()
    return math.sqrt(-2.0 * math.log(u1 + 1e-30)) * math.cos(2.0 * math.pi * u2)


def _sigmoid(x: float) -> float:
    if x > 20.0:
        return 1.0
    if x < -20.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _matvec(W: List[List[float]], x: List[float]) -> List[float]:
    return [sum(W[r][c] * x[c] for c in range(len(x))) for r in range(len(W))]


# ── Tiny MLP ──


class _GateMLP:
    """2-layer MLP: features (32) → hidden (16) → output (4)."""

    def __init__(self, n_in: int = 32, n_hidden: int = 16, n_out: int = 4):
        std_h = math.sqrt(2.0 / n_in)
        std_o = math.sqrt(2.0 / n_hidden)
        self.W1 = [[_randn() * std_h for _ in range(n_in)] for _ in range(n_hidden)]
        self.b1 = [0.0] * n_hidden
        self.W2 = [[_randn() * std_o for _ in range(n_hidden)] for _ in range(n_out)]
        self.b2 = [0.0] * n_out
        self.n_in = n_in
        self.n_hidden = n_hidden
        self.n_out = n_out

    def forward(self, x: List[float]) -> List[float]:
        h = _matvec(self.W1, x)
        h = [max(0.0, hi + bi) for hi, bi in zip(h, self.b1)]  # ReLU
        h = _matvec(self.W2, h)
        h = [hi + bi for hi, bi in zip(h, self.b2)]
        return h


# ── ControlGate ──


class ControlGate:
    """Decides blending weights and mode transitions.

    The gate reads the current feature vector (32 dims) and produces:
      - α_split: how much to spread prefix influence across layers [0,1]
      - α_diffuse: how much prefix replaces default computation [0,1]
      - mode: which operating mode to use
      - confidence: how certain the gate is about its decision
    """

    def __init__(
        self,
        risk_threshold: float = 0.7,
        uncertainty_threshold: float = 0.6,
        stealth_mode: bool = False,
        learning_rate: float = 1e-3,
    ):
        self.mlp = _GateMLP(n_in=32, n_hidden=16, n_out=4)
        self.risk_threshold = risk_threshold
        self.uncertainty_threshold = uncertainty_threshold
        self.stealth_mode = stealth_mode
        self.lr = learning_rate

        # State
        self._mode = GateMode.NORMAL
        self._current_alpha = 0.0
        self._consecutive_high_risk = 0
        self._consecutive_low_risk = 0
        self._gate_switch_count = 0

        # Adam optimizer state
        self._adam_t = 0
        self._adam_m = [0.0] * (self.mlp.n_hidden * self.mlp.n_in
                                + self.mlp.n_hidden
                                + self.mlp.n_hidden * self.mlp.n_out
                                + self.mlp.n_out)
        self._adam_v = [0.0] * len(self._adam_m)

    # ── Core Decision ──

    def evaluate(self, features: List[float],
                 risk_score: float) -> Dict:
        """Evaluate the current state and produce control signals.

        Args:
            features: 32-dim feature vector
            risk_score: current prediction from DeepRiskNet [0,1]

        Returns:
            dict with keys:
              alpha: blended activation weight [0, 1]
              mode: current gate mode string
              confidence: gate's confidence [0, 1]
              should_interrupt: bool (true when mode == INTERRUPT)
              alpha_split: prefix spread factor
              alpha_diffuse: prefix replacement factor
              reason: short string describing why
        """
        # MLP forward
        out = self.mlp.forward(features)

        # Extract signals
        alpha_split = _sigmoid(out[0])
        alpha_diffuse = _sigmoid(out[1])
        mode_logit = out[2]
        confidence = _sigmoid(out[3])

        # Compute blended alpha
        alpha = (alpha_split + alpha_diffuse) / 2.0

        # Mode switching
        old_mode = self._mode

        if risk_score > self.risk_threshold:
            self._consecutive_high_risk += 1
            self._consecutive_low_risk = 0
        else:
            self._consecutive_low_risk += 1
            self._consecutive_high_risk = 0

        # High risk → enter LATENT_THINKING
        if (self._consecutive_high_risk >= 2
                and self._mode == GateMode.NORMAL):
            self._mode = GateMode.LATENT_THINKING
            alpha = min(1.0, alpha + 0.3)
            self._gate_switch_count += 1

        # Very high risk + probes suggest confusion → INTERRUPT
        if (risk_score > 0.9 and self._consecutive_high_risk >= 3
                and self._mode == GateMode.LATENT_THINKING):
            self._mode = GateMode.INTERRUPT
            alpha = 1.0
            self._gate_switch_count += 1

        # Sustained low risk → return to NORMAL
        if (self._consecutive_low_risk >= 3
                and self._mode != GateMode.NORMAL):
            self._mode = GateMode.NORMAL
            alpha = max(0.0, alpha - 0.3)
            self._gate_switch_count += 1

        # In CONFIRM mode, force moderate alpha
        if self._mode == GateMode.CONFIRM:
            alpha = 0.4

        self._current_alpha = max(0.0, min(1.0, alpha))

        reason = self._get_reason(old_mode)
        should_interrupt = (self._mode == GateMode.INTERRUPT)

        return {
            "alpha": self._current_alpha,
            "mode": self._mode,
            "confidence": confidence,
            "should_interrupt": should_interrupt,
            "alpha_split": alpha_split,
            "alpha_diffuse": alpha_diffuse,
            "reason": reason,
        }

    def _get_reason(self, old_mode: str) -> str:
        if self._mode != old_mode:
            reasons = {
                (GateMode.NORMAL, GateMode.LATENT_THINKING):
                    "high-risk: entering latent thinking",
                (GateMode.LATENT_THINKING, GateMode.INTERRUPT):
                    "critical risk: interrupt triggered",
                (GateMode.INTERRUPT, GateMode.NORMAL):
                    "risk resolved: returning to normal",
                (GateMode.LATENT_THINKING, GateMode.NORMAL):
                    "risk normalized: exiting latent thinking",
            }
            return reasons.get((old_mode, self._mode), f"mode: {old_mode}→{self._mode}")
        if self._current_alpha > 0.6:
            return "high prefix blend"
        return "steady state"

    # ── Feedback / Learning ──

    def feedback(self, reward: float) -> None:
        """Update gate weights based on outcome reward.

        Simple heuristic: if gate correctly avoided a failure (reward > 0),
        strengthen the current weight pattern. If it caused issues (reward < 0),
        weaken it.
        """
        # Flatten all MLP params for gradient update
        params = self._flatten_params()
        dim = len(params)
        lr = self.lr

        # Heuristic: reward nudges all params proportionally
        grads = [(-reward * 0.001 * p) for p in params]

        # Adam step
        self._adam_t += 1
        beta1, beta2, eps = 0.9, 0.999, 1e-8
        lr_t = lr * math.sqrt(1.0 - beta2 ** self._adam_t) / (1.0 - beta1 ** self._adam_t)

        for i in range(dim):
            self._adam_m[i] = (beta1 * self._adam_m[i]
                               + (1.0 - beta1) * grads[i])
            self._adam_v[i] = (beta2 * self._adam_v[i]
                               + (1.0 - beta2) * grads[i] * grads[i])
            params[i] -= lr_t * self._adam_m[i] / (math.sqrt(self._adam_v[i]) + eps)

        self._unflatten_params(params)

    def _flatten_params(self) -> List[float]:
        flat = []
        for row in self.mlp.W1:
            flat.extend(row)
        flat.extend(self.mlp.b1)
        for row in self.mlp.W2:
            flat.extend(row)
        flat.extend(self.mlp.b2)
        return flat

    def _unflatten_params(self, flat: List[float]) -> None:
        idx = 0
        for r in range(self.mlp.n_hidden):
            for c in range(self.mlp.n_in):
                self.mlp.W1[r][c] = flat[idx]
                idx += 1
        for i in range(self.mlp.n_hidden):
            self.mlp.b1[i] = flat[idx]
            idx += 1
        for r in range(self.mlp.n_out):
            for c in range(self.mlp.n_hidden):
                self.mlp.W2[r][c] = flat[idx]
                idx += 1
        for i in range(self.mlp.n_out):
            self.mlp.b2[i] = flat[idx]
            idx += 1

    # ── State Management ──

    def reset_mode(self) -> None:
        """Reset gate to NORMAL mode (e.g., after a task completes)."""
        self._mode = GateMode.NORMAL
        self._current_alpha = 0.0
        self._consecutive_high_risk = 0
        self._consecutive_low_risk = 0

    @property
    def current_mode(self) -> str:
        return self._mode

    @property
    def current_alpha(self) -> float:
        return self._current_alpha

    # ── Save / Load ──

    def save(self, path: str) -> None:
        data = {
            "W1": self.mlp.W1,
            "b1": self.mlp.b1,
            "W2": self.mlp.W2,
            "b2": self.mlp.b2,
            "thresholds": {
                "risk_threshold": self.risk_threshold,
                "uncertainty_threshold": self.uncertainty_threshold,
            },
            "switches": self._gate_switch_count,
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

    @classmethod
    def load(cls, path: str, **overrides) -> "ControlGate":
        with open(path) as f:
            data = json.load(f)
        th = data.get("thresholds", {})
        gate = cls(
            risk_threshold=overrides.get("risk_threshold",
                                         th.get("risk_threshold", 0.7)),
            uncertainty_threshold=overrides.get("uncertainty_threshold",
                                                th.get("uncertainty_threshold", 0.6)),
        )
        gate.mlp.W1 = data["W1"]
        gate.mlp.b1 = data["b1"]
        gate.mlp.W2 = data["W2"]
        gate.mlp.b2 = data["b2"]
        gate._gate_switch_count = data.get("switches", 0)
        return gate
