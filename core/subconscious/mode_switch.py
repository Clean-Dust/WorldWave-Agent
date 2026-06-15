"""Subconscious Mode Switch (α fusion weight controller).

Implements Gemini's dynamic mode switching:
  "The subconscious outputs a weight α ∈ [0,1] and a control gate,
   guiding the main consciousness's context-prediction fusion ratio:
   e_fusion = α · h_ctx + (1-α) · e_pred

   When α → 1: more context-attached thinking (stability, debugging)
   When α → 0: more prediction-driven (creativity, exploration)

   The α value dynamically controls:
   - Temperature range: high α → low temp (precise), low α → high temp (exploratory)
   - Top-p range: high α → narrow top-p, low α → wide top-p
   - Thinking mode: α < 0.3 → explore, α > 0.7 → debug, else normal

Integrates with PrefixGenerator to inject reasoning-style prefix embeddings.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ModeState:
    """Current mode switch state."""
    alpha: float = 0.5          # context-prediction fusion weight
    mode: str = "normal"         # debug, explore, creative, precise, normal
    temperature: float = 0.7     # LLM sampling temperature
    top_p: float = 0.9           # nucleus sampling threshold
    confidence: float = 0.5      # how confident the subconscious is in this mode
    last_updated: float = 0.0


class ModeSwitch:
    """Dynamic mode switching controller.

    The subconscious updates α based on feature signals:
    - Context pressure → raise α (stick closer to context)
    - Entropy spike → lower α (explore alternatives)
    - Token overthink → raise α (stop overthinking)
    - Success rate → fine-tune α
    """

    MODE_MAP = {
        "debug":   {"alpha_range": (0.7, 1.0), "temp_range": (0.1, 0.4), "top_p": 0.8},
        "normal":  {"alpha_range": (0.4, 0.7), "temp_range": (0.5, 0.8), "top_p": 0.9},
        "explore": {"alpha_range": (0.1, 0.4), "temp_range": (0.7, 1.2), "top_p": 0.95},
        "creative":{"alpha_range": (0.0, 0.3), "temp_range": (1.0, 1.5), "top_p": 1.0},
        "precise": {"alpha_range": (0.8, 1.0), "temp_range": (0.0, 0.3), "top_p": 0.7},
    }

    def __init__(self):
        self.state = ModeState()

    def update(
        self,
        context_pressure: float = 0.0,
        entropy: float = 0.0,
        overthink_ratio: float = 0.0,
        success_rate: float = 0.5,
        iteration: int = 0,
    ) -> ModeState:
        """Update α based on feature signals from the main consciousness.

        Args:
            context_pressure: 0-1, how full the context window is
            entropy: token distribution entropy (higher = more uncertain)
            overthink_ratio: ratio of thinking tokens to output tokens
            success_rate: recent task success rate
            iteration: current spiral iteration

        Returns updated ModeState with new α, temperature, and mode.
        """
        alpha = self.state.alpha

        # Rule-based α adjustment (in practice, this would be learned via RL)
        # Context pressure: raise α to focus on what we have
        alpha += (context_pressure - 0.5) * 0.1

        # High entropy → explore more (lower α)
        alpha -= (entropy - 0.5) * 0.15

        # Overthinking → focus on output (raise α)
        alpha += (overthink_ratio - 0.3) * 0.1 if overthink_ratio > 0.3 else 0.0

        # Success rate biases toward current α
        if success_rate < 0.3:
            alpha += 0.05  # Not working → try different
        elif success_rate > 0.7:
            alpha += 0.02  # Working → reinforce

        # Clamp
        alpha = max(0.0, min(1.0, alpha))

        # Determine mode from α
        mode = self._mode_from_alpha(alpha)

        # Map mode to temperature and top_p
        mode_cfg = self.MODE_MAP[mode]
        temp_min, temp_max = mode_cfg["temp_range"]
        temperature = temp_min + (1.0 - alpha) * (temp_max - temp_min)
        top_p = mode_cfg["top_p"]

        self.state = ModeState(
            alpha=alpha,
            mode=mode,
            temperature=round(temperature, 2),
            top_p=round(top_p, 2),
            confidence=alpha if mode in ("debug", "precise") else 1.0 - alpha,
            last_updated=time.time(),
        )

        return self.state

    def get_prefix_mode(self) -> str:
        """Get the prefix embedding mode for PrefixGenerator."""
        return self.state.mode

    def get_llm_params(self) -> Dict[str, float]:
        """Get LLM sampling parameters dictated by current mode."""
        return {
            "temperature": self.state.temperature,
            "top_p": self.state.top_p,
            "alpha_fusion": self.state.alpha,
            "mode": self.state.mode,
            "confidence": self.state.confidence,
        }

    def _mode_from_alpha(self, alpha: float) -> str:
        if alpha > 0.8:
            return "precise"
        elif alpha > 0.7:
            return "debug"
        elif alpha > 0.4:
            return "normal"
        elif alpha > 0.2:
            return "explore"
        else:
            return "creative"

    def to_dict(self) -> dict:
        return {
            "alpha": self.state.alpha,
            "mode": self.state.mode,
            "temperature": self.state.temperature,
            "top_p": self.state.top_p,
            "confidence": self.state.confidence,
        }
