"""Subconscious Prefix Embedding Generator.

Implements Gemini's prefix embedding mechanism:
  "The subconscious generates a continuous latent vector z ~ N(0, I),
   decodes it through a decoder to a learnable token prefix p = D_ψ(z).
   The prefix p is forcibly prepended to the user prompt token embeddings
   E(q): [p; E(q)]. The prefix sets reasoning style before any token is generated."

Architecture:
  z → Linear(32→64) → ReLU → Linear(64→128) → LayerNorm → p

  - z: latent vector (32-dim, sampled from Gaussian)
  - p: prefix embedding (128-dim, injected before E(q))
  - Pure Python stdlib, zero external dependencies
  - Trained via simple gradient descent (no autograd needed for forward pass)

Usage:
  generator = PrefixGenerator()
  z = generator.sample_latent()        # Sample z ~ N(0, I)
  p = generator.generate(z, mode="debug")   # Generate prefix for debug mode
  # Inject p into LLM prompt embedding space
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("ww.prefix")

PREFIX_MODEL_PATH = os.path.expanduser("~/.worldwave/models/prefix.json")


@dataclass
class PrefixConfig:
    latent_dim: int = 32
    hidden_dim: int = 64
    prefix_dim: int = 128
    num_modes: int = 5  # debug, explore, creative, precise, normal


class PrefixGenerator:
    """Generates subconscious prefix embeddings.

    The prefix is a 128-dim vector that gets prepended to the LLM's
    token embeddings, steering reasoning style without text prompts.
    """

    def __init__(self, config: PrefixConfig = None):
        self.config = config or PrefixConfig()
        self._init_weights()
        self._load()

    def _init_weights(self):
        """Initialize decoder weights (Xavier uniform)."""
        c = self.config
        rng = random.Random(42)

        # W1: latent_dim → hidden_dim
        limit1 = math.sqrt(6.0 / (c.latent_dim + c.hidden_dim))
        self.W1 = [[rng.uniform(-limit1, limit1) for _ in range(c.hidden_dim)]
                    for _ in range(c.latent_dim)]
        self.b1 = [0.0] * c.hidden_dim

        # W2: hidden_dim → prefix_dim
        limit2 = math.sqrt(6.0 / (c.hidden_dim + c.prefix_dim))
        self.W2 = [[rng.uniform(-limit2, limit2) for _ in range(c.prefix_dim)]
                    for _ in range(c.hidden_dim)]
        self.b2 = [0.0] * c.prefix_dim

        # Mode embeddings: one per mode
        limit_m = math.sqrt(6.0 / (c.latent_dim + c.latent_dim))
        self.mode_embeddings = {
            m: [rng.uniform(-limit_m, limit_m) for _ in range(c.latent_dim)]
            for m in ["debug", "explore", "creative", "precise", "normal"]
        }

    def sample_latent(self, seed: int = None) -> List[float]:
        """Sample z ~ N(0, I) from 32-dim standard Gaussian."""
        rng = random.Random(seed or int(time.time() * 1e6))
        return [rng.gauss(0.0, 1.0) for _ in range(self.config.latent_dim)]

    def generate(self, z: List[float], mode: str = "normal") -> List[float]:
        """Generate prefix embedding from latent vector.

        Args:
            z: 32-dim latent vector
            mode: reasoning style ("debug", "explore", "creative", "precise", "normal")

        Returns:
            128-dim prefix embedding vector
        """
        # Bias the latent with mode embedding
        mode_vec = self.mode_embeddings.get(mode, self.mode_embeddings["normal"])
        z_biased = [zv + mv * 0.3 for zv, mv in zip(z, mode_vec)]

        # Layer 1: z → hidden
        hidden = [0.0] * self.config.hidden_dim
        for j in range(self.config.hidden_dim):
            s = self.b1[j]
            for i in range(self.config.latent_dim):
                s += z_biased[i] * self.W1[i][j]
            hidden[j] = max(0.0, s)  # ReLU

        # Layer 2: hidden → prefix
        prefix = [0.0] * self.config.prefix_dim
        for j in range(self.config.prefix_dim):
            s = self.b2[j]
            for i in range(self.config.hidden_dim):
                s += hidden[i] * self.W2[i][j]
            prefix[j] = s  # No activation on output

        # LayerNorm
        mean = sum(prefix) / len(prefix)
        variance = sum((x - mean) ** 2 for x in prefix) / len(prefix)
        eps = 1e-5
        std = math.sqrt(variance + eps)
        prefix = [(x - mean) / std for x in prefix]

        return prefix

    def get_prefix_for_mode(self, mode: str) -> List[float]:
        """Get a prefix for a specific thinking mode."""
        z = self.sample_latent()
        return self.generate(z, mode)

    def to_dict(self) -> dict:
        """Serialize model weights."""
        return {
            "version": 1,
            "config": {
                "latent_dim": self.config.latent_dim,
                "hidden_dim": self.config.hidden_dim,
                "prefix_dim": self.config.prefix_dim,
                "num_modes": self.config.num_modes,
            },
            "W1": self.W1,
            "b1": self.b1,
            "W2": self.W2,
            "b2": self.b2,
            "mode_embeddings": self.mode_embeddings,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PrefixGenerator":
        gen = cls(PrefixConfig(
            latent_dim=data["config"]["latent_dim"],
            hidden_dim=data["config"]["hidden_dim"],
            prefix_dim=data["config"]["prefix_dim"],
            num_modes=data["config"]["num_modes"],
        ))
        gen.W1 = data["W1"]
        gen.b1 = data["b1"]
        gen.W2 = data["W2"]
        gen.b2 = data["b2"]
        gen.mode_embeddings = data["mode_embeddings"]
        return gen

    def _save(self):
        try:
            Path(PREFIX_MODEL_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(PREFIX_MODEL_PATH, "w") as f:
                json.dump(self.to_dict(), f)
        except Exception as e:
            log.debug("Prefix save failed: %s", e)

    def _load(self):
        if not os.path.exists(PREFIX_MODEL_PATH):
            return
        try:
            with open(PREFIX_MODEL_PATH) as f:
                data = json.load(f)
            loaded = self.from_dict(data)
            self.W1 = loaded.W1
            self.b1 = loaded.b1
            self.W2 = loaded.W2
            self.b2 = loaded.b2
            self.mode_embeddings = loaded.mode_embeddings
            log.info("PrefixGenerator loaded from %s", PREFIX_MODEL_PATH)
        except Exception as e:
            log.warning("Prefix load failed: %s", e)

    def model_size_bytes(self) -> int:
        """Model size in bytes (for P2P sync estimation)."""
        d = self.to_dict()
        return len(json.dumps(d))
