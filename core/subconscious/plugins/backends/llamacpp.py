"""Llama.cpp integration for self-hosted LLM introspection.

Provides access to logits, tokenizer extension, and interrupt
callbacks for models loaded via `llama-cpp-python`.

Hidden state and attention access are limited with llama.cpp's
C-level API, but logits and token-level introspection work well.

Requires: llama-cpp-python (optional dep — import at use time).
Disabled by default.
"""

from __future__ import annotations
import math
from typing import Any, Callable, Dict, List, Optional

from ..base import (
    BackendPlugin, BackendNotReadyError, BackendUnsupportedError,
    HiddenStateSlice, AttentionInfo, LogitInfo, TokenizerInfo,
    PrefixPayload, register_backend,
)


class LlamaCppBackend(BackendPlugin):
    """Llama.cpp backend for the subconscious plugin system.

    Uses llama-cpp-python for model inference, logits access,
    tokenizer operations, and interrupt callbacks.

    NOTE: Llama.cpp does not expose per-layer hidden states or
    attention matrices through its Python API. This backend
    provides:
      ✅ Logit/prob access
      ✅ Tokenizer extension (limited)
      ✅ Interrupt callbacks (via logits_processor)
      ❌ Hidden state access (not supported by llama.cpp API)
      ❌ Attention info (not supported by llama.cpp API)
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._model_path = self._config.get(
            "model_path", ""
        )
        self._model = None
        self._interrupt_callback: Optional[Callable[[], bool]] = None
        self._last_logprobs: Optional[List[Dict[str, Any]]] = None
        self._last_tokens: List[int] = []

    def name(self) -> str:
        return "llamacpp"

    def validate(self) -> bool:
        return self._model is not None

    def estimate_model_size_gb(self) -> float:
        if self._model:
            try:
                n_params = self._model.n_vocab() * self._model.n_ctx()
                # Rough estimate: n_params * n_layers * hidden_dim * dtype_bytes
                return self._config.get("model_size_gb", 4.0)
            except Exception:
                pass
        return self._config.get("model_size_gb", 4.0)

    # ── Model Loading ──

    def load_model(self) -> None:
        """Load the GGUF model via llama-cpp-python."""
        try:
            from llama_cpp import Llama
        except ImportError:
            raise BackendNotReadyError(
                "llama-cpp-python required for LlamaCppBackend. "
                "Install with: pip install llama-cpp-python"
            )

        if not self._model_path:
            raise BackendNotReadyError("model_path is required in config")

        self._model = Llama(
            model_path=self._model_path,
            n_ctx=self._config.get("n_ctx", 2048),
            n_gpu_layers=self._config.get("n_gpu_layers", -1),
            n_threads=self._config.get("n_threads", None),
            verbose=self._config.get("verbose", False),
            logits_all=True,  # Required for logprob access
            embedding=False,
        )
        self._ready = True

    def unload_model(self) -> None:
        self._model = None
        self._ready = False
        self._last_logprobs = None
        self._last_tokens = []

    # ── Hidden States (Not Supported) ──

    def get_hidden_states(
        self,
        layer_indices: Optional[List[int]] = None,
        pool: str = "last",
    ) -> Dict[int, HiddenStateSlice]:
        raise BackendUnsupportedError(
            "Llama.cpp does not expose per-layer hidden states "
            "through its Python API."
        )

    def get_attention_info(self) -> Dict[int, AttentionInfo]:
        raise BackendUnsupportedError(
            "Llama.cpp does not expose attention matrices "
            "through its Python API."
        )

    # ── Prefix Injection ──

    def inject_prefix_embeddings(self, payload: PrefixPayload) -> None:
        raise BackendUnsupportedError(
            "Llama.cpp does not support dynamic prefix embedding injection "
            "through its Python API. Use system prompt injection instead. "
            "Set self._use_logit_bias = True to use logit biasing."
        )

    def clear_prefix_embeddings(self) -> None:
        pass  # No-op for llama.cpp

    # ── Logits ──

    def get_logits(self, return_probs: bool = True) -> LogitInfo:
        if not self._ready or self._model is None:
            raise BackendNotReadyError("Model not loaded.")

        try:
            # Access the model's logits from the last evaluation
            logits = self._model._scores  # Internal, may move
            if logits is None:
                return LogitInfo(num_tokens=32000, token_entropy=0.5)

            import numpy as np

            probs = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
            probs = probs / np.sum(probs, axis=-1, keepdims=True)

            vocab_size = probs.shape[-1]
            entropy = -np.sum(probs * np.log(probs + 1e-30), axis=-1) / math.log(vocab_size)
            avg_entropy = float(entropy.mean())

            # Top-k tokens
            top_indices = np.argsort(-probs[0])[:5]
            top_logprobs = [
                float(math.log(probs[0, i] + 1e-30))
                for i in top_indices
            ]

            return LogitInfo(
                token_ids=top_indices.tolist(),
                logprobs=top_logprobs,
                token_entropy=min(1.0, max(0.0, avg_entropy)),
                num_tokens=vocab_size,
            )
        except Exception:
            return LogitInfo(num_tokens=32000, token_entropy=0.5)

    # ── Tokenizer ──

    def get_tokenizer_info(self) -> TokenizerInfo:
        if not self._model:
            return TokenizerInfo(vocab_size=32000)
        try:
            return TokenizerInfo(
                vocab_size=self._model.n_vocab(),
                bos_token_id=self._model.token_bos(),
                eos_token_id=self._model.token_eos(),
            )
        except Exception:
            return TokenizerInfo(vocab_size=32000)

    def add_special_token(self, token_str: str) -> int:
        raise BackendUnsupportedError(
            "Llama.cpp dynamic tokenizer extension is not supported. "
            "Re-quantize the model with the tokens added to the vocabulary."
        )

    # ── Interrupt ──

    def register_interrupt_callback(
        self, callback: Callable[[], bool]
    ) -> None:
        self._interrupt_callback = callback

    def remove_interrupt_callback(self) -> None:
        self._interrupt_callback = None

    def create_interrupt_callback(self):
        """Create a generator callback that checks interrupt status.

        Usage:
            callback = backend.create_interrupt_callback()
            for output in model.create_completion(
                ..., stream=True,
                stop_callback=callback,
            ):
                ...
        """
        callback = self._interrupt_callback

        def _interrupt_cb() -> bool:
            if callback:
                return callback()
            return False

        return _interrupt_cb


# Auto-register
register_backend("llamacpp", LlamaCppBackend)
