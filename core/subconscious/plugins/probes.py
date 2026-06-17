"""Metacognitive probe computations for self-hosted LLM introspection.

Computes scalar probe signals from hidden states, attention matrices,
and logits obtained from backend plugins. These fill feature dimensions
19-23 in the subconscious feature vector (previously placeholder zeros).

Pure Python, zero external dependencies. Default-disabled.
"""

from __future__ import annotations
import math
from typing import Dict, List, Optional, Sequence

# ── Probe Signal ──


class ProbeSignal:
    """One metacognitive probe signal with metadata."""

    def __init__(
        self,
        name: str,
        value: float,
        confidence: float = 0.5,
        feature_index: int = -1,
    ):
        self.name = name
        self.value = max(0.0, min(1.0, value))
        self.confidence = max(0.0, min(1.0, confidence))
        self.feature_index = feature_index

    def __repr__(self) -> str:
        return (f"Probe({self.name}={self.value:.4f}, "
                f"conf={self.confidence:.3f}, idx={self.feature_index})")


# ── Probe Computations ──


def entropy_of_probs(probs: Sequence[float]) -> float:
    """Compute Shannon entropy of a probability distribution.

    H = -sum(p * log2(p + eps))
    Returns normalized entropy [0, 1] where 1 = uniform, 0 = certain.
    """
    total = 0.0
    for p in probs:
        if p > 1e-10:
            total += p * math.log2(p)
    # Normalize: max entropy = log2(n)
    n = len(probs)
    if n < 2:
        return 0.0
    h = -total / math.log2(n)
    return max(0.0, min(1.0, h))


def token_level_entropy(
    logprobs: List[float],
    normalize: bool = True,
) -> ProbeSignal:
    """Compute token-level entropy from log probabilities.

    The logprobs are converted to probabilities via p = exp(lp),
    then entropy is computed and optionally normalized to [0, 1].

    High entropy = model is uncertain (many plausible next tokens).
    Low entropy = model is confident (one dominant next token).
    """
    if not logprobs:
        return ProbeSignal("token_entropy", 0.5, confidence=0.0)

    # Convert logprobs to probabilities
    probs = [math.exp(lp) for lp in logprobs]
    total = sum(probs)
    if total <= 0:
        return ProbeSignal("token_entropy", 0.5, confidence=0.0)
    probs = [p / total for p in probs]

    entropy = entropy_of_probs(probs)
    return ProbeSignal("token_entropy", entropy, confidence=0.8)


def attention_sparsity(
    attention_info: Dict[int, "AttentionInfo"],  # type: ignore
) -> ProbeSignal:
    """Compute average attention sparsity across all layers.

    Sparsity = fraction of attention weights with value < 0.01.
    High sparsity = model is focusing on few positions (e.g., pattern
    matching). Low sparsity = model is broadly mixing context.
    """
    # Handle raw dict if AttentionInfo objects aren't available
    if not attention_info:
        return ProbeSignal("attention_sparsity", 0.5, confidence=0.0)

    sparsities: List[float] = []
    for info in attention_info.values():
        if isinstance(info, dict):
            sparsities.append(info.get("sparsity", 0.5))
        else:
            sparsities.append(getattr(info, "sparsity", 0.5))

    if not sparsities:
        return ProbeSignal("attention_sparsity", 0.5, confidence=0.0)

    avg = sum(sparsities) / len(sparsities)
    return ProbeSignal("attention_sparsity", avg, confidence=0.7)


def logit_magnitude(
    logits: Optional[List[float]] = None,
    logprobs: Optional[List[float]] = None,
) -> ProbeSignal:
    """Compute logit vector magnitude as a metacognitive signal.

    High logit magnitude often correlates with confident predictions.
    Uses RMS (root mean square) normalized to [0, 1].

    Args:
        logits: raw logit values
        logprobs: log probabilities (used if logits not available)
    """
    values = logits or logprobs
    if not values:
        return ProbeSignal("logit_magnitude", 0.5, confidence=0.0)

    # RMS norm of logit vector
    if len(values) < 2:
        return ProbeSignal("logit_magnitude", 0.5, confidence=0.0)

    mean_sq = sum(v * v for v in values) / len(values)
    rms = math.sqrt(mean_sq)

    # Normalize: typical logit range is [0, 30], use sigmoid-like mapping
    magnitude = 1.0 - 1.0 / (1.0 + rms * 0.1 + rms * rms * 0.001)
    return ProbeSignal("logit_magnitude", magnitude, confidence=0.6)


def hidden_state_norm(
    hidden: Optional[List[float]] = None,
    hidden_state: Optional[List[float]] = None,
) -> ProbeSignal:
    """Compute L2 norm of hidden state vector as a probe.

    Hidden state norm correlates with model's internal activation
    levels. Sudden drops/gains can indicate confusion or concept
    boundaries.

    Args:
        hidden: alias for hidden_state
        hidden_state: the actual hidden state vector
    """
    values = hidden or hidden_state
    if not values:
        return ProbeSignal("hidden_state_norm", 0.5, confidence=0.0)

    if len(values) < 2:
        return ProbeSignal("hidden_state_norm", 0.5, confidence=0.0)

    l2 = math.sqrt(sum(v * v for v in values))

    # Normalize: typical L2 norms are model-specific
    # Assume range roughly [0, 50] → map sigmoid-ish
    norm = 1.0 - 1.0 / (1.0 + l2 * 0.05 + l2 * l2 * 0.0002)
    return ProbeSignal("hidden_state_norm", norm, confidence=0.5)


def thinking_tokens_ratio(
    token_ids: List[int],
    thinking_token_ids: Optional[List[int]] = None,
) -> ProbeSignal:
    """Compute the fraction of generation tokens that are 'thinking'
    type tokens (e.g., special reasoning tokens).

    A high ratio suggests the model is in a deep reasoning phase.
    A low ratio suggests the model is producing output directly.

    Args:
        token_ids: sequence of token IDs from the generation
        thinking_token_ids: IDs of tokens considered "thinking" tokens.
            If None, assumes a heuristic: tokens near the end of vocab
            that are typically special/thinking tokens.
    """
    if not token_ids:
        return ProbeSignal("thinking_tokens_ratio", 0.0, confidence=0.0)

    if thinking_token_ids is None:
        # Heuristic: tokens with very high IDs (past typical vocab)
        # or negative IDs (special tokens in some tokenizers)
        # Default empty — user must configure for their model
        thinking_token_ids = []

    if not thinking_token_ids:
        # No thinking tokens configured → ratio is 0
        return ProbeSignal("thinking_tokens_ratio", 0.0, confidence=0.3)

    t_set = set(thinking_token_ids)
    ratio = sum(1 for tid in token_ids if tid in t_set) / len(token_ids)
    return ProbeSignal("thinking_tokens_ratio", ratio, confidence=0.9)


# ── Probe Aggregator ──


class ProbeAggregator:
    """Collects and aggregates multiple probe signals across time steps.

    Maintains a running window of probe values to smooth out noise
    and track trends.
    """

    def __init__(self, max_history: int = 10):
        self.max_history = max_history
        self._history: Dict[str, List[float]] = {}
        self._last: Dict[str, float] = {}

    def record(self, signal: ProbeSignal) -> None:
        """Record one probe signal observation."""
        name = signal.name
        if name not in self._history:
            self._history[name] = []
        self._history[name].append(signal.value)
        self._last[name] = signal.value

        # Trim
        if len(self._history[name]) > self.max_history:
            self._history[name] = self._history[name][-self.max_history:]

    def smoothed(self, name: str) -> float:
        """Get the smoothed (mean) value for a probe."""
        vals = self._history.get(name)
        if not vals:
            return 0.5
        return sum(vals) / len(vals)

    def trend(self, name: str, window: int = 3) -> float:
        """Compute trend: positive = rising, negative = falling.

        Simple: mean of last `window` - mean of previous `window`.
        """
        vals = self._history.get(name)
        if not vals or len(vals) < window * 2:
            return 0.0
        recent = sum(vals[-window:]) / window
        earlier = sum(vals[-(window * 2):-window]) / window
        return recent - earlier

    def latest(self, name: str) -> Optional[float]:
        return self._last.get(name)

    def fill_features(
        self, features: List[float],
        index_map: Dict[str, int],
    ) -> None:
        """Fill metacognitive probe dimensions in a feature vector.

        Args:
            features: mutable feature vector (length 32)
            index_map: mapping from probe name to index
                       e.g. {'token_entropy': 19, ...}
        """
        for name, idx in index_map.items():
            if 0 <= idx < len(features):
                features[idx] = self.smoothed(name)

    def reset(self) -> None:
        self._history.clear()
        self._last.clear()
