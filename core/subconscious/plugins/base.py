"""Abstract interfaces for self-hosted LLM backend plugins.

All backends (Transformers, Llama.cpp, vLLM, etc.) implement these interfaces.
The Subconscious class interacts exclusively through these abstractions.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Data Structures ──────────────────────────────────────────────────


@dataclass
class HiddenStateSlice:
    """A snapshot of internal LLM hidden states at one generation step.

    shape_flat: flattened vector length (product of all dims)
    values: list of floats (the flattened state)
    source: which model layer this came from (0 = embedding, N = Nth layer)
    """
    layer_index: int
    values: List[float]
    shape_dims: Tuple[int, ...] = (0,)


@dataclass
class AttentionInfo:
    """Attention matrix summary from one layer.

    sparsity: fraction of attention weights < 0.01 (0.0 = all dense, 1.0 = all sparse)
    entropy: token-level attention entropy averaged across heads
    total_heads: how many attention heads in this layer
    """
    layer_index: int
    sparsity: float = 0.0
    entropy: float = 0.0
    total_heads: int = 0


@dataclass
class LogitInfo:
    """Logit / probability information for one forward pass."""
    token_ids: List[int] = field(default_factory=list)
    logprobs: List[float] = field(default_factory=list)
    token_entropy: float = 0.0
    num_tokens: int = 0


@dataclass
class TokenizerInfo:
    """Information about the tokenizer for control token injection."""
    vocab_size: int = 0
    bos_token_id: Optional[int] = None
    eos_token_id: Optional[int] = None
    special_tokens: Dict[str, int] = field(default_factory=dict)


@dataclass
class PrefixPayload:
    """The payload produced by the latent decoder for prefix injection."""
    embedding_vector: List[float]
    length_tokens: int  # how many virtual tokens this prefix represents
    intent: str = ""  # "latent_context" | "reasoning_trigger" | "steering"


# ── Exceptions ───────────────────────────────────────────────────────


class BackendNotReadyError(RuntimeError):
    """Raised when a backend plugin is not ready (e.g., model not loaded)."""


class BackendUnsupportedError(RuntimeError):
    """Raised when the operation is not supported by this backend."""


class PluginConfigurationError(ValueError):
    """Raised when the plugin config is invalid."""


# ── Abstract Backend Plugin ──────────────────────────────────────────


class BackendPlugin(ABC):
    """Interface that every LLM backend plugin must implement.

    A backend plugin wraps a specific inference engine (transformers,
    llama.cpp, vLLM, etc.) and provides access to hidden states,
    attention matrices, logits, and tokenizer operations.
    """

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._ready = False

    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name (e.g. 'transformers', 'llamacpp')."""

    @abstractmethod
    def validate(self) -> bool:
        """Check whether this backend is properly configured and the model
        is loaded. Returns True if ready for operation."""

    @abstractmethod
    def estimate_model_size_gb(self) -> float:
        """Return approximate model size in GB (for feature extraction)."""

    # ── Hidden State Access ──

    @abstractmethod
    def get_hidden_states(
        self,
        layer_indices: Optional[List[int]] = None,
        pool: str = "last",
    ) -> Dict[int, HiddenStateSlice]:
        """Get hidden states from specified layers.

        Args:
            layer_indices: which layers to read. None = embedding + last layer.
            pool: 'last' (last token's hidden state), 'mean' (mean pool),
                  'first' (first token), or 'all' (return all).

        Returns:
            Dict mapping layer_index -> HiddenStateSlice
        """

    @abstractmethod
    def get_attention_info(self) -> Dict[int, AttentionInfo]:
        """Get attention matrix summaries for all layers.

        Returns: Dict mapping layer_index -> AttentionInfo
        """

    # ── Prefix / Embedding Injection ──

    @abstractmethod
    def inject_prefix_embeddings(self, payload: PrefixPayload) -> None:
        """Inject custom prefix embeddings into the model's embedding layer.

        This modifies the model's forward pass to prepend virtual
        tokens from the subconscious latent decoder.
        """

    @abstractmethod
    def clear_prefix_embeddings(self) -> None:
        """Remove injected prefix embeddings and restore the model's
        default embedding layer."""

    # ── Logit / Probability Access ──

    @abstractmethod
    def get_logits(
        self, return_probs: bool = True
    ) -> LogitInfo:
        """Get logits and probabilities from the current forward pass."""

    # ── Tokenizer ──

    @abstractmethod
    def get_tokenizer_info(self) -> TokenizerInfo:
        """Return the tokenizer's vocabulary and special token info."""

    @abstractmethod
    def add_special_token(self, token_str: str) -> int:
        """Add a new special token (e.g. '<thinking>') to the tokenizer
        and model embedding table. Returns the new token ID."""

    # ── Interrupt Support ──

    @abstractmethod
    def register_interrupt_callback(
        self, callback: Callable[[], bool]
    ) -> None:
        """Register a callback that the inference loop calls before each
        generation step. The callback returns True to stop generation,
        False to continue."""

    @abstractmethod
    def remove_interrupt_callback(self) -> None:
        """Remove the interrupt callback (resume normal generation)."""


# ── Plugin Registry ──────────────────────────────────────────────────


_plugin_registry: Dict[str, type] = {}


def register_backend(name: str, cls: type) -> None:
    """Register a backend plugin class by name."""
    _plugin_registry[name] = cls


def get_backend_class(name: str) -> Optional[type]:
    """Lookup a backend plugin class by name."""
    return _plugin_registry.get(name)


def list_available_backends() -> List[str]:
    """List all registered backend names."""
    return list(_plugin_registry.keys())


# ── Plugin Host ──────────────────────────────────────────────────────


@dataclass
class PluginHost:
    """Context object that the plugin system provides to the subconscious.

    This bridges the subconscious's tools (memory, config, logger)
    to the plugin system without circular imports.
    """
    config: Dict[str, Any] = field(default_factory=dict)
    feature_vector_size: int = 32
    enable_prefix_injection: bool = False
    enable_interrupts: bool = False
    enable_probes: bool = False
    enable_control_gate: bool = False
    enable_thinking_token: bool = False
