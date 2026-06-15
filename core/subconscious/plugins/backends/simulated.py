"""Simulated backend for testing self-hosted LLM plugins.

Generates synthetic hidden states, attention info, and logits.
Supports configurable "confusion patterns" to test interrupt logic.
No external dependencies — works in any Python environment.
"""

from __future__ import annotations
import math
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..base import (
    BackendPlugin, BackendNotReadyError, BackendUnsupportedError,
    HiddenStateSlice, AttentionInfo, LogitInfo, TokenizerInfo,
    PrefixPayload, PluginHost,
)


class SimulatedBackend(BackendPlugin):
    """Simulated backend for testing without a real model.

    Produces fake but realistic-looking hidden states, attention
    info, and logits. Supports simulation modes:
      - 'normal': stable, confident outputs
      - 'confused': high entropy, unstable signals
      - 'degraded': gradually worsening signals
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config or {})
        self._mode = self._config.get("mode", "normal")
        self._hidden_dim = self._config.get("hidden_dim", 4096)
        self._num_layers = self._config.get("num_layers", 32)
        self._base_entropy = self._config.get("base_entropy", 0.3)
        self._step = 0
        self._interrupt_callback: Optional[Callable[[], bool]] = None
        self._rng = random.Random(42)  # Fixed seed for reproducibility

        # For confused mode
        self._confused_since = 0

    def name(self) -> str:
        return "simulated"

    def validate(self) -> bool:
        self._ready = True
        return True

    def estimate_model_size_gb(self) -> float:
        return 7.0  # Simulated 7B model

    # ── Hidden State Simulation ──

    def get_hidden_states(
        self,
        layer_indices: Optional[List[int]] = None,
        pool: str = "last",
    ) -> Dict[int, HiddenStateSlice]:
        if not self._ready:
            raise BackendNotReadyError("Backend not validated")

        self._step += 1
        layers = layer_indices if layer_indices else [0, self._num_layers - 1]
        result: Dict[int, HiddenStateSlice] = {}

        for li in layers:
            values = self._sample_hidden(li)
            result[li] = HiddenStateSlice(
                layer_index=li,
                values=values,
                shape_dims=(self._hidden_dim,),
            )
        return result

    def _sample_hidden(self, layer: int) -> List[float]:
        """Sample a synthetic hidden state vector."""
        rng = self._rng
        if self._mode == "normal":
            # Stable, moderate values
            return [rng.gauss(0.0, 0.5) * (1.0 + math.sin(layer * 0.1))
                    for _ in range(min(128, self._hidden_dim))]
        elif self._mode == "confused":
            # Noisy, high-variance
            return [rng.gauss(0.0, 2.0) * (1.0 + self._step * 0.01)
                    for _ in range(min(128, self._hidden_dim))]
        elif self._mode == "degraded":
            # Gradually worsening
            degradation = 1.0 + self._step * 0.02
            return [rng.gauss(0.0, 0.5) * degradation
                    for _ in range(min(128, self._hidden_dim))]
        return [0.0] * min(128, self._hidden_dim)

    # ── Attention Simulation ──

    def get_attention_info(self) -> Dict[int, AttentionInfo]:
        if not self._ready:
            raise BackendNotReadyError("Backend not validated")

        result: Dict[int, AttentionInfo] = {}
        for li in [0, self._num_layers // 2, self._num_layers - 1]:
            if self._mode == "normal":
                sparsity = self._rng.gauss(0.6, 0.1)
                entropy = self._base_entropy
            elif self._mode == "confused":
                sparsity = self._rng.gauss(0.3, 0.2)  # More dense
                entropy = min(1.0, self._base_entropy + 0.4)
                # Simulate attention collapse every few steps
                if self._step % 5 == 0:
                    sparsity = 0.95
            else:  # degraded
                sparsity = max(0.0, 0.6 - self._step * 0.01)
                entropy = min(1.0, self._base_entropy + self._step * 0.01)

            result[li] = AttentionInfo(
                layer_index=li,
                sparsity=max(0.0, min(1.0, sparsity)),
                entropy=max(0.0, min(1.0, entropy)),
                total_heads=32,
            )
        return result

    # ── Prefix Injection ──

    def inject_prefix_embeddings(self, payload: PrefixPayload) -> None:
        if not self._ready:
            raise BackendNotReadyError("Backend not validated")
        # Simulated: just log and return
        pass

    def clear_prefix_embeddings(self) -> None:
        pass  # Simulated: no-op

    # ── Logits ──

    def get_logits(self, return_probs: bool = True) -> LogitInfo:
        if not self._ready:
            raise BackendNotReadyError("Backend not validated")

        vocab_size = 32000
        probs = [self._rng.random() for _ in range(vocab_size)]
        total = sum(probs)
        probs = [p / total for p in probs]

        logprobs = [math.log(p + 1e-30) for p in probs]
        entropy = -sum(p * math.log(p + 1e-30) for p in probs) / math.log(vocab_size)
        top_k = sorted(range(vocab_size),
                       key=lambda i: probs[i], reverse=True)[:5]

        return LogitInfo(
            token_ids=top_k,
            logprobs=[logprobs[i] for i in top_k[:3]],
            token_entropy=max(0.0, min(1.0, entropy)),
            num_tokens=vocab_size,
        )

    # ── Tokenizer ──

    def get_tokenizer_info(self) -> TokenizerInfo:
        return TokenizerInfo(
            vocab_size=32000,
            bos_token_id=1,
            eos_token_id=2,
            special_tokens={},
        )

    def add_special_token(self, token_str: str) -> int:
        # Simulated: return a fake ID
        return 31999 + hash(token_str) % 100

    # ── Interrupt ──

    def register_interrupt_callback(
        self, callback: Callable[[], bool]
    ) -> None:
        self._interrupt_callback = callback

    def remove_interrupt_callback(self) -> None:
        self._interrupt_callback = None

    def check_interrupt(self) -> bool:
        """Simulate checking the interrupt callback before each step."""
        if self._interrupt_callback:
            return self._interrupt_callback()
        return False

    # ── Testing Utilities ──

    def set_mode(self, mode: str) -> None:
        """Switch simulation mode."""
        self._mode = mode
        self._step = 0

    def set_entropy(self, entropy: float) -> None:
        self._base_entropy = max(0.0, min(1.0, entropy))

    def simulate_generation_step(self) -> Dict[str, Any]:
        """Simulate one generation step with interrupt check."""
        if self._interrupt_callback and self._interrupt_callback():
            return {"interrupted": True}

        hs = self.get_hidden_states()
        attn = self.get_attention_info()
        logits = self.get_logits()

        return {
            "interrupted": False,
            "hidden_norm": math.sqrt(
                sum(v * v for v in hs.get(self._num_layers - 1,
                    HiddenStateSlice(0, [0.0])).values[:100])
            ),
            "token_entropy": logits.token_entropy,
            "attention_sparsity": list(attn.values())[0].sparsity
                if attn else 0.5,
        }


# Register
from ..base import register_backend
register_backend("simulated", SimulatedBackend)
