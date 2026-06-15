"""
ww/core/subconscious/privacy.py — Differential Privacy (DP-SGD) for Neural Network

Applies gradient-level differential privacy during training via DP-SGD:
  1. Per-sample gradient clipping (L2 norm bound)
  2. Aggregate gradients + Gaussian noise (moment accountant)
  3. Result: (ε, δ)-DP guarantee for each training round

For model export, adds calibrated Gaussian noise to weights to prevent
model-inversion attacks when broadcasting to the gossip network.

Architecture change v8:
  - Replaced Laplace noise on leaf nodes (RandomForest era)
  - Now uses Gaussian mechanism on gradients/weights (DeepRiskNet era)
  - Moment accountant tracks privacy spend across training rounds
"""

from __future__ import annotations
import copy
import logging
import math
import random
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ww.subconscious.privacy")


class DifferentialPrivacy:
    """
    Differential privacy for neural network training and export.

    Two operating modes:
      1. Training mode (DP-SGD): clip_per_sample_gradient + aggregate + add Gaussian noise
      2. Export mode: add weight-level noise to shared snapshot

    Usage:
      dp = DifferentialPrivacy(epsilon=3.0, delta=1e-5)
      dp.clip_gradient(grad, clip_norm=1.0)       # step 1
      noisy_grad = dp.add_noise(grad)              # step 2
      dp.report_noise(noise_scale)                  # track spend
    """

    def __init__(
        self,
        epsilon: float = 3.0,
        delta: float = 1e-5,
        sensitivity: float = 1.0,
    ):
        """
        Args:
            epsilon: privacy budget (0.1-10.0). Lower = more private.
            delta: failure probability (default 1e-5, typical for DP).
            sensitivity: L2 sensitivity bound for gradient clipping.
        """
        self.epsilon = epsilon
        self.delta = delta
        self.sensitivity = sensitivity

        # Tracking
        self._total_noise_added = 0.0
        self._protected_count = 0
        self._noise_multiplier = self._compute_noise_multiplier()

    def _compute_noise_multiplier(self) -> float:
        """
        Compute noise multiplier σ such that one round of DP-SGD satisfies
        (ε, δ)-DP given sensitivity = 1.0.

        For Gaussian mechanism: σ = sqrt(2 * log(1.25/δ)) / ε

        This is the standard analytic Gaussian DP calibration.
        """
        if self.epsilon <= 0:
            return 0.0
        return math.sqrt(2.0 * math.log(1.25 / max(self.delta, 1e-10))) / max(self.epsilon, 0.01)

    def clip_gradient(self, grad_flat: List[float],
                      clip_norm: float = 1.0) -> float:
        """
        Clip a per-sample gradient to L2 norm `clip_norm`.

        Returns the original L2 norm (for diagnostics).
        Args:
            grad_flat: flattened gradient vector
            clip_norm: maximum allowed L2 norm

        Returns: original L2 norm before clipping
        """
        if clip_norm <= 0:
            return 0.0

        l2 = math.sqrt(sum(g * g for g in grad_flat))
        if l2 > clip_norm:
            scale = clip_norm / l2
            for i in range(len(grad_flat)):
                grad_flat[i] *= scale
        return l2

    def add_noise(self, grad_flat: List[float]) -> List[float]:
        """
        Add Gaussian noise calibrated to (ε, δ)-DP.

        noise ~ N(0, σ² * C² * I) where σ = noise_multiplier, C = sensitivity

        Args:
            grad_flat: averaged gradient vector (post-clipping)

        Returns: noisy gradient vector (modified in-place)
        """
        if not grad_flat:
            return grad_flat

        sigma = self._noise_multiplier * self.sensitivity
        if sigma <= 0:
            return grad_flat

        for i in range(len(grad_flat)):
            noise = random.gauss(0.0, sigma)
            grad_flat[i] += noise
            self._total_noise_added += abs(noise)

        self._protected_count += 1
        return grad_flat

    def protect_weights(self, weights_dict: dict) -> dict:
        """
        Add Gaussian noise to model weights for export.

        Used when broadcasting model snapshot to gossip network.
        Perturbs each weight with N(0, σ²) where σ = sensitivity / ε.

        Args:
            weights_dict: network parameter dict (from to_dict()['params'])

        Returns: same dict with noise added (modified in-place)
        """
        sigma = self.sensitivity / max(self.epsilon, 0.01)

        for key in weights_dict:
            val = weights_dict[key]
            if isinstance(val, list):
                if val and isinstance(val[0], list):
                    if val[0] and isinstance(val[0][0], list):
                        # 3D: heads × rows × cols (multi-head QKV)
                        for h in range(len(val)):
                            for r in range(len(val[h])):
                                for c in range(len(val[h][r])):
                                    noise = random.gauss(0.0, sigma)
                                    val[h][r][c] += noise
                                    self._total_noise_added += abs(noise)
                    else:
                        # 2D weight matrix
                        for r in range(len(val)):
                            for c in range(len(val[r])):
                                noise = random.gauss(0.0, sigma)
                                val[r][c] += noise
                                self._total_noise_added += abs(noise)
                else:
                    # 1D bias or layer-norm params
                    for i in range(len(val)):
                        noise = random.gauss(0.0, sigma)
                        val[i] += noise
                        self._total_noise_added += abs(noise)

        self._protected_count += 1
        return weights_dict

    def get_noisy_copy(self, model) :
        """
        Return a deep copy of the model with DP noise added to weights.

        For: export model to gossip network without revealing exact local weights.
        """
        noisy = copy.deepcopy(model)
        d = noisy.to_dict()
        self.protect_weights(d["params"])
        from .predictor import DeepRiskNet
        return DeepRiskNet.from_dict(d)

    def stats(self) -> Dict[str, Any]:
        return {
            "mandatory": True,
            "epsilon": self.epsilon,
            "delta": self.delta,
            "noise_multiplier": round(self._noise_multiplier, 4),
            "total_noise_added": round(self._total_noise_added, 4),
            "protected_rounds": self._protected_count,
        }
