"""
ww/core/subconscious/aggregation.py — Neural Network Robust Aggregation

Replaces the legacy tree-based aggregation (v7) with neural-network-compatible
robust aggregation for Gossip Learning.

Core design:
  - All model weights are flattened into a 1D vector for flexible aggregation
  - Robust algorithms (median, trimmed_mean, Krum, Multi-Krum) operate on
    weight vectors instead of leaf-node values
  - Weighted averaging is the primary aggregation operator (FedAvg-style)

Why this change (v8):
  - Trees cannot be averaged (different structures → misaligned leaves)
  - NN weights have a natural vector space: (W1, b1, W2, b2, ...) ∈ ℝⁿ
  - Vector averaging is well-defined and preserves model structure
  - Robust aggregators (median, Krum) naturally apply to weight vectors

Key invariant:
  All peer models must share the SAME architecture (same hidden_dim, n_features).
  This is enforced at the protocol level via architecture fingerprint.
"""

from __future__ import annotations
import copy
import json
import math
import random
from typing import Any, Dict, List, Optional, Tuple

from .predictor import DeepRiskNet


# ════════════════════════════════════════════════════════════════
#  Weight vector ↔ Model conversion
# ════════════════════════════════════════════════════════════════


def flatten_weights(model: DeepRiskNet) -> List[float]:
    """
    Flatten all model parameter tensors into a single 1D vector.

    Order is deterministic: l1_W, l1_b, ln1_g, ln1_b,
                             l2_W, l2_b, ln2_g, ln2_b,
                             l3_W, l3_b, ln3_g, ln3_b,
                             l4_W, l4_b,
                             l5_W, l5_b

    Args:
        model: DeepRiskNet instance

    Returns:
        flattened weight vector
    """
    d = model.to_dict()["params"]
    order = [
        "l1_W", "l1_b", "ln1_g", "ln1_b",
        "l2_W", "l2_b", "ln2_g", "ln2_b",
        "l3_W", "l3_b", "ln3_g", "ln3_b",
        "l4_W", "l4_b",
        "l5_W", "l5_b",
    ]
    result: List[float] = []
    for key in order:
        tensor = d.get(key, [])
        _flatten_into(tensor, result)
    return result


def _flatten_into(t: list, out: List[float]):
    """Recursively flatten a nested list into `out`."""
    if isinstance(t, list):
        if t and isinstance(t[0], list):
            for row in t:
                _flatten_into(row, out)
        else:
            out.extend(float(v) for v in t)
    else:
        out.append(float(t))


def unflatten_weights(model: DeepRiskNet, flat: List[float]) -> DeepRiskNet:
    """
    Load a flattened weight vector back into a new model.

    The model must have the same architecture as the one that produced `flat`.

    Args:
        model: reference model (provides architecture/parameter structure)
        flat: flattened weight vector from flatten_weights()

    Returns:
        new DeepRiskNet with the loaded weights
    """
    d = model.to_dict()
    order = [
        "l1_W", "l1_b", "ln1_g", "ln1_b",
        "l2_W", "l2_b", "ln2_g", "ln2_b",
        "l3_W", "l3_b", "ln3_g", "ln3_b",
        "l4_W", "l4_b",
        "l5_W", "l5_b",
    ]
    idx = [0]
    for key in order:
        if key not in d["params"]:
            continue
        _unflatten_into(d["params"][key], flat, idx)
    return DeepRiskNet.from_dict(d)


def _unflatten_into(t: list, flat: List[float], idx: List[int]):
    """Assign flattened values into nested list structure."""
    if isinstance(t, list):
        if t and isinstance(t[0], list):
            for row in t:
                _unflatten_into(row, flat, idx)
        else:
            for i in range(len(t)):
                t[i] = flat[idx[0]] if idx[0] < len(flat) else t[i]
                idx[0] += 1


def weight_count(model: DeepRiskNet) -> int:
    """Return number of scalar parameters."""
    return len(flatten_weights(model))


# ════════════════════════════════════════════════════════════════
#  Averaging-based aggregation
# ════════════════════════════════════════════════════════════════


def weighted_average(
    models: List[DeepRiskNet],
    weights: Optional[List[float]] = None,
) -> DeepRiskNet:
    """
    Weighted average of multiple DeepRiskNet models (FedAvg).

    All models must have the same architecture.

    Args:
        models: list of peer models
        weights: per-model weight (None = equal weight)

    Returns:
        weight-averaged model (new instance)
    """
    if not models:
        raise ValueError("No models to average")

    n = len(models)
    if weights is None:
        weights = [1.0 / n] * n

    # Normalise weights
    total = sum(weights)
    if total <= 0:
        weights = [1.0 / n] * n
        total = 1.0
    weights = [w / total for w in weights]

    # Flatten all models
    vectors = [flatten_weights(m) for m in models]
    min_len = min(len(v) for v in vectors)

    # Weighted average
    avg = [0.0] * min_len
    for i in range(n):
        w = weights[i]
        vec = vectors[i]
        for j in range(min_len):
            avg[j] += w * vec[j]

    # Load back into model
    return unflatten_weights(models[0], avg)


# ════════════════════════════════════════════════════════════════
#  Robust aggregation algorithms
# ════════════════════════════════════════════════════════════════


def _euclidean_distance(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def trimmed_mean(
    models: List[DeepRiskNet],
    trim_ratio: float = 0.2,
    min_models: int = 3,
) -> Optional[DeepRiskNet]:
    """
    Coordinate-wise Trimmed Mean aggregation.

    For each weight position, sort all peer values, trim the top/bottom
    trim_ratio, and average the remainder.

    Args:
        models: list of peer models
        trim_ratio: fraction to remove from each end (default 0.2)
        min_models: minimum models required

    Returns:
        aggregated model, or None if insufficient models
    """
    if len(models) < min_models:
        return None

    vectors = [flatten_weights(m) for m in models]
    min_len = min(len(v) for v in vectors)
    vectors = [v[:min_len] for v in vectors]

    n_params = min_len
    n_models = len(vectors)
    trim_count = max(1, int(n_models * trim_ratio))

    aggregated = []
    for pos in range(n_params):
        values = sorted(v[pos] for v in vectors)
        trimmed = values[trim_count: n_models - trim_count]
        if not trimmed:
            trimmed = values
        aggregated.append(sum(trimmed) / len(trimmed))

    return unflatten_weights(models[0], aggregated)


def median_aggregation(
    models: List[DeepRiskNet],
    min_models: int = 3,
) -> Optional[DeepRiskNet]:
    """
    Coordinate-wise median aggregation.

    More robust than averaging — even with ~50% malicious nodes,
    the median remains stable.

    Args:
        models: list of peer models
        min_models: minimum models required

    Returns:
        aggregated model, or None
    """
    if len(models) < min_models:
        return None

    vectors = [flatten_weights(m) for m in models]
    min_len = min(len(v) for v in vectors)
    vectors = [v[:min_len] for v in vectors]

    n_params = min_len
    n_models = len(vectors)

    aggregated = []
    for pos in range(n_params):
        values = sorted(v[pos] for v in vectors)
        mid = n_models // 2
        if n_models % 2 == 0:
            val = (values[mid - 1] + values[mid]) / 2.0
        else:
            val = values[mid]
        aggregated.append(val)

    return unflatten_weights(models[0], aggregated)


def krum_aggregation(
    models: List[DeepRiskNet],
    f: Optional[int] = None,
    min_models: int = 5,
) -> Optional[DeepRiskNet]:
    """
    Krum aggregation: select the single model closest to all others.

    For each model, compute sum of distances to its (n-f-2) nearest neighbours.
    Return the model with the smallest total distance.

    Guarantee: if f < (n-2)/2, the returned model is not malicious.

    Args:
        models: list of peer models
        f: tolerated malicious count (default = max(1, n//2 - 1))
        min_models: minimum models required

    Returns:
        best model (selected from input list), or None
    """
    n = len(models)
    if n < min_models:
        return None

    if f is None:
        f = max(1, n // 2 - 1)

    # Ensure Krum condition: n > 2f + 2
    if n < 2 * f + 2:
        f = max(1, n // 2 - 1)
    if n < 2 * f + 2:
        return models[0]  # Degenerate: not enough peers

    m_neighbors = n - f - 2  # number of nearest neighbours

    vectors = [flatten_weights(m) for m in models]
    min_len = min(len(v) for v in vectors)
    vectors = [v[:min_len] for v in vectors]

    # Pairwise distance matrix
    distances = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _euclidean_distance(vectors[i], vectors[j])
            distances[i][j] = d
            distances[j][i] = d

    best_idx = 0
    best_score = float("inf")

    for i in range(n):
        sorted_dists = sorted(
            [(distances[i][j], j) for j in range(n) if j != i],
            key=lambda x: x[0],
        )
        top_m = sorted_dists[:m_neighbors]
        score = sum(d for d, _ in top_m)
        if score < best_score:
            best_score = score
            best_idx = i

    return models[best_idx]


def multi_krum_aggregation(
    models: List[DeepRiskNet],
    f: Optional[int] = None,
    m: Optional[int] = None,
    min_models: int = 5,
) -> Optional[DeepRiskNet]:
    """
    Multi-Krum aggregation (recommended robust aggregator).

    Process:
      1. Compute Krum score for each model
      2. Select top-m most agreeable models
      3. Average the weights of selected models

    Better than single Krum: preserves consensus, smooths noise.

    Args:
        models: list of peer models
        f: tolerated malicious count
        m: number of top models to average
        min_models: minimum models required

    Returns:
        aggregated model (averaged from top-m), or None
    """
    n = len(models)
    if n < min_models:
        return None

    if f is None:
        f = max(1, n // 2 - 1)

    if n < 2 * f + 2:
        f = max(1, n // 2 - 1)
    if n < 2 * f + 2:
        return median_aggregation(models, min_models=3)

    if m is None:
        m = max(2, n - f - 2)
    m = min(m, n - f - 2)
    m = max(2, m)

    m_neighbors = n - f - 2

    vectors = [flatten_weights(m) for m in models]
    min_len = min(len(v) for v in vectors)
    vectors = [v[:min_len] for v in vectors]

    distances = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _euclidean_distance(vectors[i], vectors[j])
            distances[i][j] = d
            distances[j][i] = d

    krum_scores = []
    for i in range(n):
        sorted_dists = sorted(
            [(distances[i][j], j) for j in range(n) if j != i],
            key=lambda x: x[0],
        )
        top = sorted_dists[:m_neighbors]
        score = sum(d for d, _ in top)
        krum_scores.append((score, i))

    krum_scores.sort(key=lambda x: x[0])
    top_indices = [idx for _, idx in krum_scores[:m]]

    # Average the selected models
    selected = [models[idx] for idx in top_indices]
    return weighted_average(selected)


# ════════════════════════════════════════════════════════════════
#  Legacy compatibility (aggregate_forest)
# ════════════════════════════════════════════════════════════════


def aggregate_forest(
    models: List[DeepRiskNet],
    method: str = "median",
    weights: Optional[List[float]] = None,
) -> DeepRiskNet:
    """
    Aggregate multiple DeepRiskNet models.

    Args:
        models: list of peer models (all same architecture)
        method: "weighted_average", "trimmed_mean", "median", "krum", "multi_krum"
        weights: per-model weight (only used by weighted_average)

    Returns:
        aggregated model
    """
    if not models:
        return DeepRiskNet()

    if method == "weighted_average":
        return weighted_average(models, weights)
    elif method == "trimmed_mean":
        result = trimmed_mean(models)
        return result or models[0]
    elif method == "krum":
        result = krum_aggregation(models)
        return result or models[0]
    elif method == "multi_krum":
        result = multi_krum_aggregation(models)
        return result or models[0]
    else:  # median (default)
        result = median_aggregation(models)
        return result or models[0]


# ════════════════════════════════════════════════════════════════
#  Validation set evaluation
# ════════════════════════════════════════════════════════════════


def evaluate_model(
    model: DeepRiskNet,
    validation_set: List[Tuple[List[float], float]],
) -> float:
    """
    Evaluate model on a validation set.

    Args:
        model: DeepRiskNet instance
        validation_set: [(state_vector, ground_truth), ...]

    Returns:
        accuracy (0.0 ~ 1.0)
    """
    if not validation_set:
        return 0.0
    correct = 0
    for vec, outcome in validation_set:
        pred = model.predict(vec)
        risk = getattr(pred, 'crash_risk', pred)
        correct += 1.0 - abs(risk - outcome)
    return correct / len(validation_set)


def balance_gradient_defense(
    peer_model: DeepRiskNet,
    local_model: DeepRiskNet,
    validation_set: List[Tuple[List[float], float]],
    threshold: float = -0.1,
) -> Tuple[bool, float]:
    """
    Gradient-direction BALANCE defense (Gemini Layer 5).

    Computes the average loss gradient for both models on the local validation
    set, then measures cosine similarity between the two gradient vectors.

    If the peer's gradient direction strongly opposes the local gradient
    (cos_sim < threshold), the peer may be adversarially pushing in the
    opposite direction → reject.

    Args:
        peer_model: incoming peer model (deep-copied internally)
        local_model: current local model (not modified)
        validation_set: local validation data
        threshold: min cosine similarity (-1..1). Default -0.1 (reject only
                   when direction is strongly opposite).

    Returns:
        (accepted: bool, cosine_similarity: float)
    """
    if not validation_set:
        return True, 0.0

    # Deep-copy peer so we don't pollute the original
    peer_copy = copy.deepcopy(peer_model)

    local_grad = _avg_gradient(local_model, validation_set)
    peer_grad = _avg_gradient(peer_copy, validation_set)

    if not local_grad or not peer_grad:
        return True, 0.0

    cos_sim = _cosine_similarity_vec(local_grad, peer_grad)
    return cos_sim >= threshold, cos_sim


def _avg_gradient(model: DeepRiskNet,
                  validation_set: List[Tuple[List[float], float]]) -> List[float]:
    """Compute average gradient of model over validation set.

    Each sample: forward(x) → backward(target) → collect gradients.
    Returns a 1D list of averaged gradient values.

    Args:
        model: DeepRiskNet instance
        validation_set: [(x_vector, target), ...]

    Returns:
        averaged gradient vector
    """
    n = len(validation_set)
    if n == 0:
        return []

    accumulated = None
    for x, y in validation_set:
        model.forward(x)
        model.backward(y)
        # Collect gradient from each parameter
        flat = []
        for _, _, g in model._params.params:
            flat.extend(g)
        if accumulated is None:
            accumulated = flat[:]
        else:
            for i in range(len(flat)):
                accumulated[i] += flat[i]

    if accumulated is None:
        return []
    inv_n = 1.0 / n
    return [v * inv_n for v in accumulated]


def _cosine_similarity_vec(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors.

    Args:
        a: first vector
        b: second vector (must be same length)

    Returns:
        similarity in [-1, 1]
    """
    min_len = min(len(a), len(b))
    if min_len < 2:
        return 0.0
    aa = a[:min_len]
    bb = b[:min_len]
    dot = sum(x * y for x, y in zip(aa, bb))
    na = math.sqrt(sum(x * x for x in aa))
    nb = math.sqrt(sum(y * y for y in bb))
    if na * nb == 0:
        return 0.0
    return dot / (na * nb)


def balancer_protection(
    peer_model: DeepRiskNet,
    local_model: DeepRiskNet,
    validation_set: List[Tuple[List[float], float]],
    threshold: float = 0.02,
) -> Tuple[bool, float]:
    """
    BALANCE defense: compare peer model against local model on a local
    validation set.

    The peer model is accepted only if its accuracy on the local validation
    set is within `threshold` of the local model's accuracy.

    This prevents malicious updates that work on the attacker's data but
    fail on the target node's data distribution.

    Args:
        peer_model: incoming peer model
        local_model: current local model
        validation_set: local validation data
        threshold: maximum allowed accuracy gap

    Returns:
        (accepted, local_accuracy)
    """
    if not validation_set:
        return True, 0.0

    local_acc = evaluate_model(local_model, validation_set)
    peer_acc = evaluate_model(peer_model, validation_set)

    if peer_acc >= local_acc or (local_acc - peer_acc) <= threshold:
        return True, local_acc
    return False, local_acc


def local_validation_check(
    peer_model: DeepRiskNet,
    validation_set: List[Tuple[List[float], float]],
    min_accuracy: float = 0.4,
) -> bool:
    """
    LPC (Local Prediction Check): ensure peer model meets a baseline accuracy
    on local data before accepting.

    This prevents models that systematically produce garbage predictions.

    Args:
        peer_model: incoming peer model
        validation_set: local validation data
        min_accuracy: minimum accuracy required

    Returns:
        True if peer model passes the check
    """
    if not validation_set:
        return True
    acc = evaluate_model(peer_model, validation_set)
    return acc >= min_accuracy


# ════════════════════════════════════════════════════════════════
#  Delta Sum Compression — efficient P2P weight sync
# ════════════════════════════════════════════════════════════════


class DeltaEncoder:
    """Compress model weight sync to delta-only for P2P efficiency.

    Usage:
      encoder = DeltaEncoder(threshold=0.001)
      # First sync: store baseline
      encoder.set_baseline(full_vector)
      # Second sync: encode delta
      delta = encoder.encode_delta(new_vector)  # smaller than full
      # Receiver: reconstruct from baseline
      reconstructed = encoder.apply_delta(baseline, delta)
    """

    def __init__(self, threshold: float = 0.001):
        self.threshold = threshold
        self._baseline: Optional[List[float]] = None
        self._total_sent = 0
        self._total_saved = 0

    def set_baseline(self, flat_weights: List[float]) -> None:
        """Store reference weights for delta computation."""
        self._baseline = list(flat_weights)

    def clear_baseline(self) -> None:
        self._baseline = None

    def encode_delta(
        self, new_weights: List[float],
    ) -> Dict[str, Any]:
        """Compute sparse delta from baseline.

        Returns:
            {"indices": [...], "values": [...], "full_length": N}
            Only indices where |delta| >= threshold are included.
            If no baseline is set, returns full weights with all indices.
        """
        if self._baseline is None or len(self._baseline) != len(new_weights):
            # First sync: send full vector
            full_len = len(new_weights)
            return {
                "indices": list(range(full_len)),
                "values": list(new_weights),
                "full_length": full_len,
                "is_full": True,
            }

        indices: List[int] = []
        values: List[float] = []
        for i, (old_v, new_v) in enumerate(zip(self._baseline, new_weights)):
            diff = new_v - old_v
            if abs(diff) >= self.threshold:
                indices.append(i)
                values.append(diff)

        full_len = len(new_weights)
        saved = full_len - len(values)

        # Track stats
        self._total_sent += len(values)
        self._total_saved += saved

        return {
            "indices": indices,
            "values": values,
            "full_length": full_len,
            "is_full": False,
        }

    def apply_delta(
        self, baseline: List[float], delta: Dict[str, Any]
    ) -> List[float]:
        """Reconstruct weights from baseline + delta.

        Args:
            baseline: receiver's current weights
            delta: encoded delta from encode_delta()

        Returns:
            reconstructed weight vector
        """
        result = list(baseline)
        if delta.get("is_full", False):
            return list(delta["values"])

        for idx, val in zip(delta["indices"], delta["values"]):
            if idx < len(result):
                result[idx] += val
        return result

    def compression_ratio(self) -> float:
        """How much smaller deltas are vs full sync on average.

        Ratio > 1.0 means delta is smaller (good).
        """
        total_full = self._total_sent + self._total_saved
        if total_full == 0:
            return 1.0
        return total_full / max(1, self._total_sent)

    def stats(self) -> Dict[str, Any]:
        return {
            "total_sent": self._total_sent,
            "total_saved": self._total_saved,
            "compression_ratio": round(self.compression_ratio(), 2),
        }
