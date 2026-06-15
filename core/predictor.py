"""
ww/core/subconscious/predictor.py — Deep Risk Network (Tabular MLP)

Replaces the legacy RandomForest with a modern neural network for subconscious
failure-risk prediction.  Architecture:

  Input (32-dim numerical feature vector)
    → Linear(32→64) + LayerNorm + ReLU + Dropout(p=0.1)
    → Linear(64→64) + LayerNorm + ReLU + Dropout(p=0.1)
    → Linear(64→32) + LayerNorm + ReLU
    → Linear(32→16) + ReLU
    → Linear(16→1) + Sigmoid
    → Risk score [0.0, 1.0]

Key properties:
  - Pure Python, zero external dependencies (no numpy, no torch)
  - ~9,200 trainable parameters → ~36 KB serialized (float32)
  - Supports weighted-averaging aggregation for Gossip Learning
  - L2 weight decay + gradient clipping + dropout for robustness
  - Adam optimizer (momentum + adaptive LR)

Why this replaces RandomForest:
  - Neural network weights are continuous → natural for FedAvg / Gossip aggregation
  - Tree ensemble structure cannot be averaged across peers (different split topology)
  - NN latent space acts as universal "collective subconscious" representation
"""

from __future__ import annotations
import json
import logging
import math
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("ww.subconscious.predictor")

# ════════════════════════════════════════════════════════════════
#  Math utilities
# ════════════════════════════════════════════════════════════════

def _randn() -> float:
    """Box-Muller transform — standard normal N(0, 1)."""
    u1 = random.random()
    u2 = random.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _randn_tensor(shape: List[int]) -> List:
    """Recursively build a tensor (list-of-lists) of standard normal values."""
    if len(shape) == 1:
        return [_randn() for _ in range(shape[0])]
    return [_randn_tensor(shape[1:]) for _ in range(shape[0])]


def _zeros(shape: List[int]) -> List:
    if len(shape) == 1:
        return [0.0] * shape[0]
    return [_zeros(shape[1:]) for _ in range(shape[0])]


def _ones(shape: List[int]) -> List:
    if len(shape) == 1:
        return [1.0] * shape[0]
    return [_ones(shape[1:]) for _ in range(shape[0])]


def _matvec(W: List[List[float]], x: List[float]) -> List[float]:
    """Matrix (rows × cols) × vector (cols)."""
    return [sum(W[r][c] * x[c] for c in range(len(W[0]))) for r in range(len(W))]


def _add(a: List[float], b: List[float]) -> List[float]:
    return [a[i] + b[i] for i in range(len(a))]


def _sub(a: List[float], b: List[float]) -> List[float]:
    return [a[i] - b[i] for i in range(len(a))]


def _scale(v: List[float], s: float) -> List[float]:
    return [x * s for x in v]


def _dot(a: List[float], b: List[float]) -> float:
    return sum(a[i] * b[i] for i in range(len(a)))


def _l2_norm_sq(v: List[float]) -> float:
    return sum(x * x for x in v)


def _l2_norm(v: List[float]) -> float:
    return math.sqrt(_l2_norm_sq(v))


def _add_vec_to_mat_rows(W: List[List[float]], v: List[float]):
    """Add vector v to each row of matrix W (in-place for efficiency)."""
    for r in range(len(W)):
        for c in range(len(W[0])):
            W[r][c] += v[c] if isinstance(v, list) and len(v) > 1 else (v if isinstance(v, (int, float)) else v[c])  # noqa: inline for speed


def _outer(a: List[float], b: List[float]) -> List[List[float]]:
    """Outer product: len(a) × len(b) matrix."""
    return [[a[i] * b[j] for j in range(len(b))] for i in range(len(a))]


# ════════════════════════════════════════════════════════════════
#  Parameter container (manages a list of (value, grad) pairs)
# ════════════════════════════════════════════════════════════════

ParamRef = Tuple[str, List, List]  # (name, value_tensor, grad_tensor)


class ParameterGroup:
    """Collects named parameters from all layers for optimisation."""

    def __init__(self):
        self.params: List[ParamRef] = []

    def add(self, name: str, value: list, grad: list):
        self.params.append((name, value, grad))

    def zero_grad(self):
        for _, _, g in self.params:
            _zero_inplace(g)

    def __len__(self):
        return len(self.params)

    def param_count(self) -> int:
        """Total number of scalar parameters."""
        total = 0
        for _, v, _ in self.params:
            total += _tensor_size(v)
        return total

    def l2_reg(self) -> float:
        """Sum of squared L2 norms across all parameters."""
        return sum(_l2_norm_sq(_tensor_flatten(v)) for _, v, _ in self.params)


def _tensor_size(t: list) -> int:
    """Number of scalar elements in a nested list."""
    if not isinstance(t, list):
        return 1
    if t and not isinstance(t[0], list):
        return len(t)
    return sum(_tensor_size(sub) for sub in t)


def _tensor_flatten(t: list) -> List[float]:
    """Flatten arbitrarily nested list of floats."""
    result = []
    stack = [t]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        else:
            result.append(float(item))
    return result


def _unflatten_inplace(t: list, flat: List[float]) -> None:
    """Fill nested list t from flat list in-place, consuming elements."""
    idx = [0]

    def _fill(x):
        if isinstance(x, list):
            if x and not isinstance(x[0], list):
                n = len(x)
                for i in range(n):
                    x[i] = flat[idx[0] + i]
                idx[0] += n
            else:
                for sub in x:
                    _fill(sub)

    _fill(t)


def _zero_inplace(t: list):
    """Set all elements to 0.0 in-place."""
    if isinstance(t, list):
        if t and not isinstance(t[0], list):
            for i in range(len(t)):
                t[i] = 0.0
        else:
            for sub in t:
                _zero_inplace(sub)


# ════════════════════════════════════════════════════════════════
#  Layer definitions
# ════════════════════════════════════════════════════════════════

class Linear:
    """Fully-connected layer: output = input @ W^T + bias."""

    def __init__(self, in_features: int, out_features: int,
                 use_bias: bool = True, gain: float = 1.0):
        # Kaiming init
        std = gain * math.sqrt(2.0 / in_features)
        self.W = [[_randn() * std for _ in range(in_features)] for _ in range(out_features)]
        self.b = [0.0] * out_features if use_bias else None
        self._in = None  # cached input for backward

        self.in_features = in_features
        self.out_features = out_features
        self.use_bias = use_bias

    def forward(self, x: List[float]) -> List[float]:
        self._in = x
        out = _matvec(self.W, x)
        if self.b is not None:
            out = _add(out, self.b)
        return out

    def backward(self, grad_out: List[float], pg: ParameterGroup,
                 prefix: str = "") -> List[float]:
        """Backprop: compute gradients for W, b and input.
        Returns gradient w.r.t. input.
        """
        x = self._in  # cached

        # dW = outer(grad_out, x)
        dW = _outer(grad_out, x)
        pg.add(prefix + "W", self.W, dW)

        if self.b is not None:
            pg.add(prefix + "b", self.b, grad_out[:])

        # dx = grad_out @ W
        dx = [0.0] * self.in_features
        for c in range(self.in_features):
            s = 0.0
            for r in range(self.out_features):
                s += grad_out[r] * self.W[r][c]
            dx[c] = s
        return dx

    def param_count(self) -> int:
        n = self.in_features * self.out_features
        if self.b is not None:
            n += self.out_features
        return n


# ════════════════════════════════════════════════════════════════
#  Intra-Feature Attention (Tabular Self-Attention)
# ════════════════════════════════════════════════════════════════


class IntraFeatureAttention:
    """
    Multi-head intra-feature self-attention for tabular data.

    Architecture per sample:
      x (n_features,)
      → embed each feature to d_model via learned embedding → tokens (n_features, d_model)
      → for each head h (d_k = d_model // n_heads):
          Q_h = T @ W_q_h, K_h = T @ W_k_h, V_h = T @ W_v_h
          head_h = softmax(Q_h @ K_h^T / sqrt(d_k)) @ V_h
      → concat all heads → (n_features, d_model)
      → output projection @ W_o
      → residual (attended + tokens)
      → LayerNorm
      → mean pool over features → (d_model,)
      → project to n_features → output (n_features,)

    Pure Python, zero external deps. ~6,240 params with defaults.
    """

    def __init__(self, n_features: int = 32, d_model: int = 32,
                 n_heads: int = 4):
        assert d_model % n_heads == 0, "d_model must be divisible by n_heads"
        self.n_features = n_features
        self.d_model = d_model
        self.n_heads = n_heads
        self.d_k = d_model // n_heads  # 8 with defaults
        scale = math.sqrt(2.0 / d_model)
        scale_k = math.sqrt(2.0 / self.d_k)

        # Feature embeddings: (n_features, d_model)
        self.embed = [[_randn() * scale for _ in range(d_model)]
                      for _ in range(n_features)]

        # Per-head QKV projections
        # Each head: w_q (d_model, d_k), w_k (d_model, d_k), w_v (d_model, d_k)
        self.w_q: List[List[List[float]]] = []  # list of n_heads matrices
        self.w_k: List[List[List[float]]] = []
        self.w_v: List[List[List[float]]] = []
        for _ in range(n_heads):
            self.w_q.append([[_randn() * scale_k for _ in range(self.d_k)]
                              for _ in range(d_model)])
            self.w_k.append([[_randn() * scale_k for _ in range(self.d_k)]
                              for _ in range(d_model)])
            self.w_v.append([[_randn() * scale_k for _ in range(self.d_k)]
                              for _ in range(d_model)])

        # Output projection after concat: (d_model, d_model)
        self.w_o = [[_randn() * scale for _ in range(d_model)]
                     for _ in range(d_model)]

        # LayerNorm (post-residual)
        self.ln_gamma = [1.0] * d_model
        self.ln_beta = [0.0] * d_model

        # Pool → feature projection: (d_model) → (n_features)
        self.w_out = [[_randn() * scale for _ in range(d_model)]
                       for _ in range(n_features)]
        self.b_out = [0.0 for _ in range(n_features)]

        self._caches: Dict[str, Any] = {}

    def forward(self, x: List[float]) -> List[float]:
        """x: (n_features,) → (n_features,)"""
        nf, dm, nh, dk = self.n_features, self.d_model, self.n_heads, self.d_k

        # 1. Embed: tokens[i][j] = x[i] * embed[i][j]
        tokens = [[x[i] * self.embed[i][j] for j in range(dm)]
                  for i in range(nf)]

        # 2. For each head: Q_h, K_h, V_h, attention
        q_heads, k_heads, v_heads = [], [], []
        attn_heads, attended_heads = [], []
        for h in range(nh):
            # Q_h = tokens @ w_q_h: (nf, dm) @ (dm, dk) → (nf, dk)
            qh = [[sum(tokens[i][k] * self.w_q[h][k][j] for k in range(dm))
                   for j in range(dk)] for i in range(nf)]
            kh = [[sum(tokens[i][k] * self.w_k[h][k][j] for k in range(dm))
                   for j in range(dk)] for i in range(nf)]
            vh = [[sum(tokens[i][k] * self.w_v[h][k][j] for k in range(dm))
                   for j in range(dk)] for i in range(nf)]

            # Scores = Q @ K^T / sqrt(dk): (nf, dk) @ (nf, dk)^T → (nf, nf)
            s = 1.0 / math.sqrt(dk)
            scores_h = [[sum(qh[i][h_] * kh[j][h_] * s for h_ in range(dk))
                        for j in range(nf)] for i in range(nf)]

            # Softmax row-wise
            attn_h = []
            for i in range(nf):
                row = scores_h[i]
                max_val = max(row)
                exp_row = [math.exp(v - max_val) for v in row]
                sum_exp = sum(exp_row)
                attn_h.append([e / sum_exp for e in exp_row])

            # Attended = attn @ V: (nf, nf) @ (nf, dk) → (nf, dk)
            attended_h = [[sum(attn_h[i][j] * vh[j][k] for j in range(nf))
                           for k in range(dk)] for i in range(nf)]

            q_heads.append(qh)
            k_heads.append(kh)
            v_heads.append(vh)
            attn_heads.append(attn_h)
            attended_heads.append(attended_h)

        # 3. Concat heads: (nf, dk * nh) = (nf, dm)
        concat = [[attended_heads[h][i][j]
                   for h in range(nh) for j in range(dk)]
                  for i in range(nf)]

        # 4. Output projection: concat @ w_o → (nf, dm)
        projected = [[sum(concat[i][k] * self.w_o[k][j] for k in range(dm))
                      for j in range(dm)] for i in range(nf)]

        # 5. Residual: projected + tokens
        res = [[projected[i][j] + tokens[i][j] for j in range(dm)]
               for i in range(nf)]

        # 6. LayerNorm
        mean = [sum(res[i][j] for j in range(dm)) / dm for i in range(nf)]
        var = [sum((res[i][j] - mean[i]) ** 2 for j in range(dm)) / dm
               for i in range(nf)]
        inv_std = [1.0 / math.sqrt(var[i] + 1e-5) for i in range(nf)]
        normed = [[self.ln_gamma[j] * (res[i][j] - mean[i]) * inv_std[i]
                   + self.ln_beta[j] for j in range(dm)] for i in range(nf)]

        # 7. Mean pool over features: (nf, dm) → (dm,)
        pooled = [sum(normed[i][j] for i in range(nf)) / nf for j in range(dm)]

        # 8. Output: pooled @ w_out + b_out → (nf,)
        out = [sum(pooled[k] * self.w_out[i][k] for k in range(dm))
               + self.b_out[i] for i in range(nf)]

        # Cache for backward
        self._caches = {
            "x": x, "tokens": tokens,
            "q_heads": q_heads, "k_heads": k_heads, "v_heads": v_heads,
            "attn_heads": attn_heads, "attended_heads": attended_heads,
            "concat": concat, "projected": projected,
            "res": res,
            "mean": mean, "var": var, "inv_std": inv_std, "normed": normed,
            "pooled": pooled,
        }
        return out

    def backward(self, grad_out: List[float], pg: ParameterGroup,
                 prefix: str = "") -> List[float]:
        """Backprop through multi-head attention. Returns grad w.r.t. input x."""
        nf, dm, nh, dk = self.n_features, self.d_model, self.n_heads, self.d_k
        c = self._caches
        x = c["x"]
        tokens = c["tokens"]
        qh = c["q_heads"]
        kh = c["k_heads"]
        vh = c["v_heads"]
        attn_h = c["attn_heads"]
        attended_h = c["attended_heads"]
        concat = c["concat"]
        projected = c["projected"]
        res = c["res"]
        mean = c["mean"]
        inv_std = c["inv_std"]
        normed = c["normed"]
        pooled = c["pooled"]

        eps = 1e-5

        # ── Step 8: grad through w_out/b_out ──
        d_pooled = [sum(grad_out[i] * self.w_out[i][k] for i in range(nf))
                    for k in range(dm)]
        d_w_out = [[pooled[k] * grad_out[i] for k in range(dm)]
                   for i in range(nf)]
        d_b_out = list(grad_out)
        pg.add(prefix + "W", self.w_out, d_w_out)
        pg.add(prefix + "b", self.b_out, d_b_out)

        # ── Step 7: grad through mean pool ──
        d_normed = [[d_pooled[j] / nf for j in range(dm)] for i in range(nf)]

        # ── Step 6: grad through LayerNorm ──
        # normed[i][j] = gamma[j] * (res[i][j] - mean[i]) * inv_std[i] + beta[j]
        # For each sample i:
        # d_normed[i][j] flows through gamma, beta, res[i][j]
        d_gamma = [0.0] * dm
        d_beta = [0.0] * dm
        d_ln_res = [[0.0] * dm for _ in range(nf)]

        for i in range(nf):
            ri = res[i]
            mi = mean[i]
            istd = inv_std[i]

            # d_beta[j] = d_normed[i][j]
            for j in range(dm):
                d_beta[j] += d_normed[i][j]
                # d_gamma[j] += d_normed[i][j] * (ri[j] - mi) * istd
                d_gamma[j] += d_normed[i][j] * (ri[j] - mi) * istd

            # d_ln_res[i][j] = d_normed[i][j] * gamma[j] * istd  (direct path)
            # + contributions through mean and var
            # For full LN gradient, need mean/var paths too
            d_x_hat = [d_normed[i][j] * self.ln_gamma[j] for j in range(dm)]

            # d_var = sum(d_x_hat[j] * (ri[j] - mi) * (-0.5) * (var[i] + eps)^(-1.5))
            var_i = c["var"][i]
            d_var = sum(d_x_hat[j] * (ri[j] - mi) for j in range(dm))
            d_var *= -0.5 * (var_i + eps) ** (-1.5)

            # d_mean = sum(d_x_hat[j] * (-istd)) + d_var * (-2*sum(ri[j]-mi)/nf)
            d_mean = sum(d_x_hat[j] for j in range(dm)) * (-istd)
            d_mean += d_var * (-2.0 * sum(ri[j] - mi for j in range(dm)) / dm)

            for j in range(dm):
                d_ln_res[i][j] = (d_x_hat[j] * istd
                                  + d_var * 2.0 * (ri[j] - mi) / dm
                                  + d_mean / dm)

        pg.add(prefix + "ln_g", self.ln_gamma, d_gamma)
        pg.add(prefix + "ln_b", self.ln_beta, d_beta)

        # ── Step 5: grad through residual (projected + tokens) ──
        d_projected = [[d_ln_res[i][j] for j in range(dm)] for i in range(nf)]
        d_tokens_res = [[d_ln_res[i][j] for j in range(dm)] for i in range(nf)]

        # ── Step 4: grad through output projection w_o ──
        # projected[i][j] = sum_k(concat[i][k] * w_o[k][j])
        d_concat = [[sum(d_projected[i][j] * self.w_o[k][j] for j in range(dm))
                     for k in range(dm)] for i in range(nf)]
        d_w_o = [[sum(concat[i][k] * d_projected[i][j] for i in range(nf))
                  for j in range(dm)] for k in range(dm)]
        pg.add(prefix + "w_o", self.w_o, d_w_o)

        # ── Step 3: grad through concat (split back to heads) ──
        d_attended = []
        for h in range(nh):
            start = h * dk
            d_attended_h = [[d_concat[i][start + j] for j in range(dk)]
                            for i in range(nf)]
            d_attended.append(d_attended_h)

        # ── Step 2: for each head, backprop through attention + QKV ──
        d_tokens_qkv = [[0.0] * dm for _ in range(nf)]

        for h in range(nh):
            da = d_attended[h]
            qh_h = qh[h]
            kh_h = kh[h]
            vh_h = vh[h]
            attn_h_h = attn_h[h]

            # d_attn: grad through A @ V
            d_attn = [[sum(da[i][k] * vh_h[j][k] for k in range(dk))
                       for j in range(nf)] for i in range(nf)]
            d_v = [[sum(attn_h_h[i][j] * da[i][k] for i in range(nf))
                    for k in range(dk)] for j in range(nf)]

            # Softmax gradient
            d_scores = []
            for i in range(nf):
                row_a = attn_h_h[i]
                row_da = d_attn[i]
                dot = sum(row_a[k] * row_da[k] for k in range(nf))
                d_scores.append([row_a[j] * (row_da[j] - dot)
                                 for j in range(nf)])

            # Grad through Q @ K^T / sqrt(dk)
            s = 1.0 / math.sqrt(dk)
            d_q = [[sum(d_scores[i][j] * kh_h[j][h_] * s for j in range(nf))
                    for h_ in range(dk)] for i in range(nf)]
            d_k = [[sum(d_scores[i][j] * qh_h[i][h_] * s for i in range(nf))
                    for h_ in range(dk)] for j in range(nf)]

            # Grad through QKV projections
            # w_q[h][k][j] = weight from input dim k to output dim j
            d_w_q_h = [[sum(tokens[i][k] * d_q[i][j] for i in range(nf))
                        for j in range(dk)] for k in range(dm)]
            d_w_k_h = [[sum(tokens[i][k] * d_k[i][j] for i in range(nf))
                        for j in range(dk)] for k in range(dm)]
            d_w_v_h = [[sum(tokens[i][k] * d_v[i][j] for i in range(nf))
                        for j in range(dk)] for k in range(dm)]

            pg.add(prefix + f"w_q_{h}", self.w_q[h], d_w_q_h)
            pg.add(prefix + f"w_k_{h}", self.w_k[h], d_w_k_h)
            pg.add(prefix + f"w_v_{h}", self.w_v[h], d_w_v_h)

            # Grad w.r.t. tokens from this head's QKV path
            for i in range(nf):
                for k in range(dm):
                    grad_q = sum(d_q[i][j] * self.w_q[h][k][j] for j in range(dk))
                    grad_k = sum(d_k[i][j] * self.w_k[h][k][j] for j in range(dk))
                    grad_v = sum(d_v[i][j] * self.w_v[h][k][j] for j in range(dk))
                    d_tokens_qkv[i][k] += grad_q + grad_k + grad_v

        # ── Combine residual + QKV paths ──
        d_tokens = [[d_tokens_qkv[i][j] + d_tokens_res[i][j]
                     for j in range(dm)] for i in range(nf)]

        # ── Step 1: grad through embedding ──
        d_embed = [[x[i] * d_tokens[i][j] for j in range(dm)]
                   for i in range(nf)]
        pg.add(prefix + "embed", self.embed, d_embed)

        # Grad w.r.t. input x
        d_x = [sum(d_tokens[i][j] * self.embed[i][j] for j in range(dm))
               for i in range(nf)]
        return d_x

    def param_count(self) -> int:
        nf, dm, nh, dk = self.n_features, self.d_model, self.n_heads, self.d_k
        embed = nf * dm
        qkv = nh * 3 * dm * dk
        w_o = dm * dm
        ln = dm * 2
        out = nf * dm + nf
        return embed + qkv + w_o + ln + out


class LayerNorm:
    """Layer normalisation over a single sample (no learnable affine by default)."""

    def __init__(self, dim: int, affine: bool = True, eps: float = 1e-5):
        self.dim = dim
        self.eps = eps
        self.affine = affine
        self.gamma = [1.0] * dim if affine else None
        self.beta = [0.0] * dim if affine else None
        self._x = None
        self._mean = 0.0
        self._std = 0.0

    def forward(self, x: List[float]) -> List[float]:
        self._x = x
        n = len(x)
        mean = sum(x) / n
        var = sum((xi - mean) ** 2 for xi in x) / n
        std = math.sqrt(var + self.eps)
        self._mean = mean
        self._std = std

        x_hat = [(xi - mean) / std for xi in x]

        if self.affine and self.gamma is not None and self.beta is not None:
            return [self.gamma[i] * x_hat[i] + self.beta[i] for i in range(n)]
        return x_hat

    def backward(self, grad_out: List[float], pg: ParameterGroup,
                 prefix: str = "") -> List[float]:
        """Backprop through layernorm for a single sample."""
        x = self._x
        n = len(x)
        mean = self._mean
        std = self._std

        # x_hat = (x - mean) / std
        x_hat = [(xi - mean) / std for xi in x]

        # If affine: gradient through gamma, beta
        if self.affine and self.gamma is not None and self.beta is not None:
            d_gamma = [grad_out[i] * x_hat[i] for i in range(n)]
            d_beta = grad_out[:]
            pg.add(prefix + "ln_gamma", self.gamma, d_gamma)
            pg.add(prefix + "ln_beta", self.beta, d_beta)

            # Continue gradient through gamma
            grad_in = [grad_out[i] * self.gamma[i] for i in range(n)]
        else:
            grad_in = grad_out[:]

        # Manual backprop through layernorm for single sample
        # dx_hat = grad_in (the gradient that arrived at x_hat)
        # Then backprop through: x_hat = (x - mean) / std
        # d_var = sum(dx_hat * (x - mean) * (-0.5) * (var+eps)^(-1.5))
        # d_mean = -sum(dx_hat) / std + d_var * sum(-2*(x-mean))/n
        # dx = dx_hat / std + d_var * 2*(x-mean)/n + d_mean/n

        var = std * std - self.eps
        if var < 0:
            var = 0.0

        d_x_hat = grad_in
        d_var = sum(d_x_hat[i] * (x[i] - mean) for i in range(n)) * (-0.5) * (var + self.eps) ** (-1.5) if (var + self.eps) > 0 else 0.0
        d_mean = -sum(d_x_hat) / std if std > 0 else 0.0
        if n > 0:
            d_mean += d_var * sum(-2.0 * (x[i] - mean) for i in range(n)) / n

        dx = [0.0] * n
        for i in range(n):
            dx[i] = d_x_hat[i] / std if std > 0 else 0.0
            dx[i] += d_var * 2.0 * (x[i] - mean) / n
            dx[i] += d_mean / n

        return dx

    def param_count(self) -> int:
        return (self.dim * 2) if self.affine else 0


class ReLU:
    @staticmethod
    def forward(x: List[float]) -> List[float]:
        return [max(0.0, v) for v in x]

    @staticmethod
    def backward(grad_out: List[float], cache: List[float]) -> List[float]:
        return [grad_out[i] if cache[i] > 0 else 0.0 for i in range(len(grad_out))]


class Sigmoid:
    @staticmethod
    def forward(x: List[float]) -> List[float]:
        # Clamp to avoid overflow
        def _s(v):
            if v > 20:
                return 1.0
            if v < -20:
                return 0.0
            return 1.0 / (1.0 + math.exp(-v))
        return [_s(v) for v in x]

    @staticmethod
    def backward(grad_out: List[float], cache: List[float]) -> List[float]:
        """cache = sigmoid output values."""
        return [grad_out[i] * cache[i] * (1.0 - cache[i]) for i in range(len(grad_out))]


# ════════════════════════════════════════════════════════════════
#  Triage Vector — 4-signal subconscious output
# ════════════════════════════════════════════════════════════════


class TriageVector:
    """
    Multi-dimensional subconscious output vector.
    
    Replaces the single Risk Score [0.0, 1.0] with 4 independent signals,
    inspired by clinical triage frameworks:
    
      idx 0 — crash_risk:       Global probability of spiral crash [0.0, 1.0]
      idx 1 — compress_urgency: Context compression need  [0.0, 1.0]
      idx 2 — tool_downgrade:   Tool privilege reduction flag (0.5+ = downgrade)
      idx 3 — mode_switch:      Cognitive mode change (0.0=noop, 0.3=react, 0.6=plan_execute, 0.9=interrupt)
    
    Attributes (derived at decode time):
      crash_risk, compress_urgency, tool_downgrade, mode_switch
    """
    __slots__ = ('crash_risk', 'compress_urgency', 'tool_downgrade', 'mode_switch')
    
    MODE_MAP = {
        0.0: "noop",
        0.3: "react",
        0.6: "plan_execute",
        0.9: "interrupt",
    }
    
    def __init__(self, crash_risk: float = 0.0, compress_urgency: float = 0.0,
                 tool_downgrade: float = 0.0, mode_switch: float = 0.0):
        self.crash_risk = max(0.0, min(1.0, crash_risk))
        self.compress_urgency = max(0.0, min(1.0, compress_urgency))
        self.tool_downgrade = max(0.0, min(1.0, tool_downgrade))
        self.mode_switch = max(0.0, min(1.0, mode_switch))
    
    @classmethod
    def from_raw(cls, raw: List[float]) -> "TriageVector":
        """Build from a 4-element raw output vector."""
        if len(raw) < 4:
            raw = list(raw) + [0.0] * (4 - len(raw))
        return cls(crash_risk=raw[0], compress_urgency=raw[1],
                   tool_downgrade=raw[2], mode_switch=raw[3])
    
    def to_list(self) -> List[float]:
        return [self.crash_risk, self.compress_urgency, self.tool_downgrade, self.mode_switch]
    
    @property
    def mode_name(self) -> str:
        """Decode mode_switch to enum name."""
        for threshold, name in sorted(self.MODE_MAP.items(), reverse=True):
            if self.mode_switch >= threshold:
                return name
        return "noop"
    
    @property
    def should_downgrade(self) -> bool:
        """Tool downgrade flag (True if risk > 0.5)."""
        return self.tool_downgrade >= 0.5
    
    @property
    def needs_compression(self) -> bool:
        """Compression needed (True if urgency > 0.6)."""
        return self.compress_urgency >= 0.6
    
    @property
    def is_critical(self) -> bool:
        """Critical state: crash risk > 0.7 or any signal > 0.85."""
        return (self.crash_risk >= 0.7 or self.compress_urgency >= 0.85
                or self.tool_downgrade >= 0.85)
    
    def to_dict(self) -> dict:
        return {
            "crash_risk": round(self.crash_risk, 4),
            "compress_urgency": round(self.compress_urgency, 4),
            "tool_downgrade": round(self.tool_downgrade, 4),
            "mode_switch": round(self.mode_switch, 4),
            "mode_name": self.mode_name,
            "should_downgrade": self.should_downgrade,
            "needs_compression": self.needs_compression,
            "is_critical": self.is_critical,
        }
    
    def __repr__(self) -> str:
        return (f"TriageVector(crash={self.crash_risk:.3f}, "
                f"compress={self.compress_urgency:.3f}, "
                f"tool={self.tool_downgrade:.3f}, "
                f"mode={self.mode_name})")


class Dropout:
    def __init__(self, p: float = 0.1):
        self.p = p
        self._mask: Optional[List[float]] = None
        self._training = True

    def train(self, mode: bool = True):
        self._training = mode

    def forward(self, x: List[float]) -> List[float]:
        if not self._training or self.p <= 0.0:
            self._mask = None
            return x
        scale = 1.0 / (1.0 - self.p)
        self._mask = [1.0 if random.random() > self.p else 0.0 for _ in x]
        return [x[i] * self._mask[i] * scale for i in range(len(x))]

    def backward(self, grad_out: List[float]) -> List[float]:
        if self._mask is None or self.p <= 0.0:
            return grad_out
        scale = 1.0 / (1.0 - self.p)
        return [grad_out[i] * self._mask[i] * scale for i in range(len(grad_out))]


# ════════════════════════════════════════════════════════════════
#  Loss functions
# ════════════════════════════════════════════════════════════════

def bce_loss(pred: float, target: float, eps: float = 1e-7) -> float:
    """Binary cross-entropy for scalar output."""
    p = max(eps, min(1.0 - eps, pred))
    return -target * math.log(p) - (1.0 - target) * math.log(1.0 - p)


def bce_grad(pred: float, target: float) -> float:
    """Gradient of BCE w.r.t. prediction."""
    p = max(1e-7, min(1.0 - 1e-7, pred))
    return (p - target) / (p * (1.0 - p))


def mse_loss(pred: float, target: float) -> float:
    return (pred - target) ** 2


def mse_grad(pred: float, target: float) -> float:
    return 2.0 * (pred - target)


# ════════════════════════════════════════════════════════════════
#  Adam Optimiser (pure Python)
# ════════════════════════════════════════════════════════════════

class Adam:
    """
    Adam optimiser with L2 weight decay.

    Works on a ParameterGroup collected during backward.
    """

    def __init__(self, pg: ParameterGroup, lr: float = 0.001,
                 beta1: float = 0.9, beta2: float = 0.999,
                 eps: float = 1e-8, weight_decay: float = 1e-4):
        self.pg = pg
        self.lr = lr
        self.beta1 = beta1
        self.beta2 = beta2
        self.eps = eps
        self.weight_decay = weight_decay
        self._t = 0
        # state: (m, v) per parameter
        self._state: Dict[str, Tuple[list, list]] = {}

    def step(self, pg: Optional[ParameterGroup] = None):
        """Apply one Adam step to all parameters.

        Args:
            pg: Current ParameterGroup (from backward). If None, uses self.pg (legacy).
        """
        pg = pg or self.pg
        self._t += 1
        t = self._t
        lr = self.lr
        b1, b2, eps = self.beta1, self.beta2, self.eps

        for name, val, grad in pg.params:
            flat_val = _tensor_flatten(val)
            flat_grad = _tensor_flatten(grad)

            # L2 weight decay
            if self.weight_decay > 0:
                flat_grad = [g + self.weight_decay * v for v, g in zip(flat_val, flat_grad)]

            # Adam moment estimates
            key = name
            if key not in self._state:
                self._state[key] = ([0.0] * len(flat_grad), [0.0] * len(flat_grad))
            m, v = self._state[key]

            new_m = [b1 * m[i] + (1.0 - b1) * flat_grad[i] for i in range(len(flat_grad))]
            new_v = [b2 * v[i] + (1.0 - b2) * (flat_grad[i] ** 2) for i in range(len(flat_grad))]
            self._state[key] = (new_m, new_v)

            # Bias-corrected
            m_hat = [new_m[i] / (1.0 - b1 ** t) for i in range(len(new_m))]
            v_hat = [new_v[i] / (1.0 - b2 ** t) for i in range(len(new_v))]

            # Update
            update = [lr * m_hat[i] / (math.sqrt(v_hat[i]) + eps) for i in range(len(m_hat))]
            flat_new = [flat_val[i] - update[i] for i in range(len(flat_val))]

            _assign_from_flat(val, flat_new)

    def state_dict(self) -> dict:
        return {
            "t": self._t,
            "lr": self.lr,
            "beta1": self.beta1,
            "beta2": self.beta2,
            "eps": self.eps,
            "weight_decay": self.weight_decay,
        }


def _assign_from_flat(t: list, flat: List[float], idx: List[int] = None):
    """Assign flattened values back into nested list structure."""
    if idx is None:
        idx = [0]
    if isinstance(t, list):
        if t and not isinstance(t[0], list):
            for i in range(len(t)):
                t[i] = flat[idx[0]]
                idx[0] += 1
        else:
            for sub in t:
                _assign_from_flat(sub, flat, idx)


# ════════════════════════════════════════════════════════════════
#  Temporal Buffer (for 1D-CNN/TCN temporal processing)
# ════════════════════════════════════════════════════════════════


class TemporalBuffer:
    """Ring buffer of recent feature vectors for temporal conv processing.

    Default capacity=8 captures the last ~8 spiral cycles, enough for
    1D-CNN kernels of size 3-5 to detect short-term failure patterns.
    """

    def __init__(self, capacity: int = 8, input_dim: int = 32):
        self.capacity = capacity
        self.input_dim = input_dim
        self._buffer: List[List[float]] = []
        self._pos = 0

    def push(self, vec: List[float]) -> None:
        """Add one feature vector to the buffer."""
        if len(self._buffer) < self.capacity:
            self._buffer.append(list(vec))
        else:
            self._buffer[self._pos] = list(vec)
        self._pos = (self._pos + 1) % self.capacity

    def is_full(self) -> bool:
        return len(self._buffer) == self.capacity

    def get_sequence(self) -> List[List[float]]:
        """Return sequence in chronological order (oldest first)."""
        if not self.is_full():
            return list(self._buffer)
        # Rotate so oldest is at index 0
        return self._buffer[self._pos:] + self._buffer[:self._pos]

    def reset(self) -> None:
        self._buffer.clear()
        self._pos = 0


# ── 1D Convolution utilities ──


class Conv1D:
    """1D convolution (valid padding) for temporal feature processing.

    Pure NumPy-free implementation using nested lists.
    Kernel shapes: [out_channels, in_channels, kernel_size]
    """

    def __init__(self, in_channels: int, out_channels: int,
                 kernel_size: int = 3, padding: int = 1):
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.padding = padding

        # He init
        std = math.sqrt(2.0 / (in_channels * kernel_size))
        self.W = [[[_randn() * std for _ in range(kernel_size)]
                   for _ in range(in_channels)]
                  for _ in range(out_channels)]
        self.b = [0.0] * out_channels

    def param_count(self) -> int:
        return (self.out_channels * self.in_channels * self.kernel_size
                + self.out_channels)

    def forward(self, x: List[List[float]]) -> List[List[float]]:
        """x: [batch, in_channels] (list of lists, one per time step).
        Returns [batch, out_channels] (same length as input).
        """
        batch = len(x)
        # Pad both ends
        pad = [0.0] * self.in_channels
        padded = [pad] * self.padding + x + [pad] * self.padding

        result = []
        for t in range(batch):
            out = [0.0] * self.out_channels
            for oc in range(self.out_channels):
                s = 0.0
                for ic in range(self.in_channels):
                    for k in range(self.kernel_size):
                        s += self.W[oc][ic][k] * padded[t + k][ic]
                out[oc] = s + self.b[oc]
            result.append(out)
        return result

    def get_state(self) -> dict:
        return {"W": self.W, "b": self.b, "kernel_size": self.kernel_size}

    def load_state(self, state: dict) -> None:
        self.W = state["W"]
        self.b = state["b"]
        self.kernel_size = state.get("kernel_size", self.kernel_size)


class TemporalConvNet:
    """Two-layer 1D-CNN with ReLU + max pooling for temporal feature extraction.

    Architecture:
      Input (T×input_dim) → Conv1D(input_dim→32, k=3) → ReLU → MaxPool(k=2)
                           → Conv1D(32→16, k=3) → ReLU → MaxPool(k=2)
                           → Flatten → Linear → output_dim

    Total downsampling: T → T//4.
    Output: vector of length output_dim (default 32, matching MLP l3 input).
    """

    def __init__(self, input_dim: int = 32, hidden_dim: int = 32,
                 output_dim: int = 32, buffer_size: int = 8):
        self.conv1 = Conv1D(input_dim, hidden_dim, kernel_size=3, padding=1)
        self.conv2 = Conv1D(hidden_dim, hidden_dim // 2, kernel_size=3, padding=1)
        self.pool_k = 2

        # Flatten size after conv2 + pool: (buffer_size // 4) * (hidden_dim // 2)
        flat_size = (buffer_size // 4) * (hidden_dim // 2)
        self.fc = Linear(flat_size, output_dim)
        self.relu = ReLU()

        self._channel_dim = hidden_dim // 2
        self._buffer_size = buffer_size

    def param_count(self) -> int:
        return (self.conv1.param_count() + self.conv2.param_count()
                + self.fc.param_count())

    def forward(self, seq: List[List[float]]) -> List[float]:
        """seq: list of T vectors, each [input_dim].
        Returns single output_dim vector.
        """
        # Conv1 + ReLU + MaxPool
        h = self.conv1.forward(seq)
        h = [self.relu.forward(t) for t in h]
        h = self._max_pool(h)  # T // 2

        # Conv2 + ReLU + MaxPool
        h = self.conv2.forward(h)
        h = [self.relu.forward(t) for t in h]
        h = self._max_pool(h)  # T // 4

        # Flatten
        flat = []
        for t in h:
            flat.extend(t)

        # FC projection
        return self.fc.forward(flat)

    def _max_pool(self, x: List[List[float]]) -> List[List[float]]:
        """Pool over time dimension with stride k=2."""
        result = []
        for i in range(0, len(x), self.pool_k):
            if i + 1 >= len(x):
                result.append(x[i])
            else:
                pooled = [max(a, b) for a, b in zip(x[i], x[i + 1])]
                result.append(pooled)
        return result

    def get_state(self) -> dict:
        return {
            "conv1": self.conv1.get_state(),
            "conv2": self.conv2.get_state(),
            "fc_W": self.fc.W,
            "fc_b": self.fc.b,
        }

    def load_state(self, state: dict) -> None:
        self.conv1.load_state(state["conv1"])
        self.conv2.load_state(state["conv2"])
        self.fc.W = state["fc_W"]
        self.fc.b = state["fc_b"]


# ════════════════════════════════════════════════════════════════
#  Deep Risk Network
# ════════════════════════════════════════════════════════════════

class DeepRiskNet:
    """
    Tabular Deep MLP for subconscious failure-risk prediction.

    Architecture (5-layer MLP + optional 1D-CNN temporal):
      Non-temporal (default):
        Input(32) → Attn → Linear(32→64) → LayerNorm → ReLU → Dropout(0.1)
                         → Linear(64→64) → LayerNorm → ReLU → Dropout(0.1)
                         → Linear(64→32) → LayerNorm → ReLU
                         → Linear(32→16) → ReLU
                         → Linear(16→4) → Sigmoid → TriageVector(4 heads)
                         → ~15.5K params
      Temporal (use_temporal=True, buffer full):
        Seq(8×32) → Conv1D(32→32) → ReLU → MaxPool
                  → Conv1D(32→16) → ReLU → MaxPool
                  → Flatten → FC(32) → Lite Linear(32→16) → Linear(16→4)
                  → ~21.2K params (+5.7K for temporal conv)
      Early Exits (BranchyNet):
        - Head at l2 (64→4) and l3/l4 boundary (32→4)
        - predict_early_exit(threshold) returns early if confident
      Subnet Extraction (HeteroFL):
        - extract_subnet(scale) → smaller model from first k% neurons
    """

    def __init__(self, n_features: int = 32, hidden_dim: int = 64,
                 dropout: float = 0.1, lr: float = 0.001,
                 use_temporal: bool = False,
                 temporal_buffer_size: int = 8):
        self.n_features = n_features
        self.hidden_dim = hidden_dim
        self.dropout_p = dropout
        self.lr = lr
        self.use_temporal = use_temporal
        self.temporal_buffer_size = temporal_buffer_size

        # Build layers
        self.attn = IntraFeatureAttention(n_features, d_model=32, n_heads=4)
        self.l1 = Linear(n_features, hidden_dim)
        self.ln1 = LayerNorm(hidden_dim)
        self.relu1 = ReLU()
        self.drop1 = Dropout(dropout)

        self.l2 = Linear(hidden_dim, hidden_dim)
        self.ln2 = LayerNorm(hidden_dim)
        self.relu2 = ReLU()
        self.drop2 = Dropout(dropout)

        self.l3 = Linear(hidden_dim, hidden_dim // 2)
        self.ln3 = LayerNorm(hidden_dim // 2)
        self.relu3 = ReLU()

        self.l4 = Linear(hidden_dim // 2, hidden_dim // 4)
        self.relu4 = ReLU()

        self.l5 = Linear(hidden_dim // 4, 4)  # Triage Vector: 4 outputs
        self.sigmoid = Sigmoid()

        # Temporal processing (1D-CNN, optional, default off)
        self.temporal_buffer: Optional[TemporalBuffer] = None
        self.temporal_conv: Optional[TemporalConvNet] = None
        if use_temporal:
            self.temporal_buffer = TemporalBuffer(
                capacity=temporal_buffer_size, input_dim=n_features)
            self.temporal_conv = TemporalConvNet(
                input_dim=n_features, hidden_dim=32,
                output_dim=32, buffer_size=temporal_buffer_size)

        # Caches for backward pass
        self._caches: Dict[str, Any] = {}

        # Training flag for dropout
        self._training = True
        self._has_trained = False  # Was the model ever trained?

        # Parameter group
        self._params = ParameterGroup()
        temporal_count = (self.temporal_conv.param_count()
                          if self.temporal_conv else 0)
        self._param_count = (
            self.attn.param_count() +
            self.l1.param_count() + self.ln1.param_count() +
            self.l2.param_count() + self.ln2.param_count() +
            self.l3.param_count() + self.ln3.param_count() +
            self.l4.param_count() + self.l5.param_count() +
            temporal_count
        )

        # Optimiser (created on first .fit() call)
        self._optim: Optional[Adam] = None

    def train_mode(self, mode: bool = True):
        self._training = mode
        self.drop1.train(mode)
        self.drop2.train(mode)

    def forward(self, x: List[float]) -> List[float]:
        """Forward pass for a single sample. Returns list of 4 triage signals [0, 1].

        When temporal mode is active and buffer is full, uses 1D-CNN temporal
        path (conv1→conv2→fc→l4→l5), bypassing l1-l2-l3 MLP.
        When temporal mode is off or buffer not yet full, uses original MLP path.
        """
        self._caches = {}

        # ── Temporal path (when enabled + buffer full) ──
        if self.use_temporal and self.temporal_buffer and self.temporal_buffer.is_full():
            seq = self.temporal_buffer.get_sequence()
            h = self.temporal_conv.forward(seq)  # → [32]
            # Skip l1-l2-l3, feed temporal features into l4 directly
            self._caches["temporal_seq_len"] = len(seq)
        else:
            # ── Standard MLP path ──
            h = x
            # Intra-feature attention
            h = self.attn.forward(h)
            self._caches["attn_out"] = h

            # Layer 1
            h = self.l1.forward(h)
            self._caches["l1"] = h
            h = self.ln1.forward(h)
            self._caches["ln1_in"] = h
            h = self.relu1.forward(h)
            self._caches["relu1_in"] = h
            h = self.drop1.forward(h)

            # Layer 2
            h = self.l2.forward(h)
            self._caches["l2"] = h
            h = self.ln2.forward(h)
            self._caches["ln2_in"] = h
            h = self.relu2.forward(h)
            self._caches["relu2_in"] = h
            h = self.drop2.forward(h)

            # Layer 3
            h = self.l3.forward(h)
            self._caches["l3"] = h
            h = self.ln3.forward(h)
            self._caches["ln3_in"] = h
            h = self.relu3.forward(h)
            self._caches["relu3_in"] = h

        # Layer 4
        h = self.l4.forward(h)
        self._caches["l4"] = h
        h = self.relu4.forward(h)
        self._caches["relu4_in"] = h

        # Layer 5 (output) — 4-dimensional Triage Vector
        h = self.l5.forward(h)
        self._caches["l5"] = h
        out = self.sigmoid.forward(h)  # 4 sigmoid values
        self._caches["sigmoid_out"] = out

        return out

    def push_temporal(self, x: List[float]) -> None:
        """Record a feature vector for temporal sequence processing.

        Call this after each spiral cycle to build up the temporal buffer.
        When the buffer reaches capacity, forward() switches to the
        1D-CNN temporal path automatically.
        """
        if self.temporal_buffer is not None:
            self.temporal_buffer.push(x)

    # ── Elastic Neural Network (HeteroFL: subnet extraction) ──

    def extract_subnet(self, scale: float = 0.5) -> "DeepRiskNet":
        """Extract a smaller subnet from this (super)network.

        HeteroFL-style: keep the first 'scale' fraction of neurons
        in each hidden layer by slicing weight matrix rows/columns.

        Args:
            scale: fraction of hidden dim to keep (0.25 ~ 1.0)

        Returns:
            new DeepRiskNet with hidden_dim = scale * original hidden_dim
            All weights copied from the supernet (no training needed).
        """
        h = max(4, int(self.hidden_dim * scale))
        h2 = max(2, h // 2)
        h4 = max(1, h // 4)

        subnet = DeepRiskNet(
            n_features=self.n_features,
            hidden_dim=h,
            dropout=self.dropout_p,
            use_temporal=False,
        )

        # Helper: slice rows and optionally columns of 2D weight matrix
        def slice_2d(W, rows, cols=None):
            return [row[:cols] if cols else list(row) for row in W[:rows]]

        def slice_1d(b, n):
            return list(b[:n])

        # Attention (unchanged input dimension, scaled d_model)
        d_sub = max(4, int(self.attn.d_model * scale))
        n_heads = max(1, int(self.attn.n_heads * scale))
        subnet.attn = IntraFeatureAttention(self.n_features, d_sub, n_heads)
        embed_size = self.n_features * d_sub
        if self.attn.embed and len(self.attn.embed) >= embed_size:
            subnet.attn.embed = [row[:d_sub] for row in self.attn.embed[:self.n_features]]
        if self.attn.w_q:
            n_q = min(n_heads, len(self.attn.w_q))
            subnet.attn.w_q = [
                [row[:d_sub] for row in head[:d_sub]]
                for head in self.attn.w_q[:n_q]
            ]
            subnet.attn.n_heads = n_q
            subnet.attn.d_model = d_sub
            # Similarly for w_k, w_v, w_o
            subnet.attn.w_k = [
                [row[:d_sub] for row in head[:d_sub]]
                for head in self.attn.w_k[:n_q]
            ]
            subnet.attn.w_v = [
                [row[:d_sub] for row in head[:d_sub]]
                for head in self.attn.w_v[:n_q]
            ]
            if self.attn.w_o:
                subnet.attn.w_o = [row[:d_sub] for row in self.attn.w_o[:d_sub]]
            if self.attn.w_out:
                subnet.attn.w_out = slice_2d(self.attn.w_out, self.n_features, d_sub)
            if self.attn.b_out:
                subnet.attn.b_out = slice_1d(self.attn.b_out, self.n_features)

        # Layer 1: n_features → h
        subnet.l1.W = slice_2d(self.l1.W, h)
        subnet.l1.b = slice_1d(self.l1.b, h)
        if self.ln1.gamma is not None:
            subnet.ln1.gamma = slice_1d(self.ln1.gamma, h)
            subnet.ln1.beta = slice_1d(self.ln1.beta, h)

        # Layer 2: h → h
        subnet.l2.W = slice_2d(self.l2.W, h, h)
        subnet.l2.b = slice_1d(self.l2.b, h)
        if self.ln2.gamma is not None:
            subnet.ln2.gamma = slice_1d(self.ln2.gamma, h)
            subnet.ln2.beta = slice_1d(self.ln2.beta, h)

        # Layer 3: h → h2
        subnet.l3.W = slice_2d(self.l3.W, h2, h)
        subnet.l3.b = slice_1d(self.l3.b, h2)
        if self.ln3.gamma is not None:
            subnet.ln3.gamma = slice_1d(self.ln3.gamma, h2)
            subnet.ln3.beta = slice_1d(self.ln3.beta, h2)

        # Layer 4: h2 → h4
        subnet.l4.W = slice_2d(self.l4.W, h4, h2)
        subnet.l4.b = slice_1d(self.l4.b, h4)

        # Layer 5: h4 → 4 (output heads, always 4)
        subnet.l5.W = slice_2d(self.l5.W, 4, h4)
        subnet.l5.b = slice_1d(self.l5.b, 4)

        # Recompute param count
        subnet._param_count = (
            subnet.attn.param_count() +
            subnet.l1.param_count() + subnet.ln1.param_count() +
            subnet.l2.param_count() + subnet.ln2.param_count() +
            subnet.l3.param_count() + subnet.ln3.param_count() +
            subnet.l4.param_count() + subnet.l5.param_count()
        )
        return subnet

    # ── BranchyNet: early exit heads ──

    def _build_early_exit_heads(self) -> None:
        """Build early exit heads at l2 and l4 for BranchyNet inference.

        These are small Linear layers that produce TriageVector from
        intermediate representations, allowing early termination when
        confidence is high.
        """
        self._exit_l2 = Linear(self.hidden_dim, 4)  # from l2 output (64-dim)
        self._exit_l4 = Linear(self.hidden_dim // 2, 4)  # from l3/l4 (32-dim)
        self._exit_heads_built = True

    def predict_early_exit(self, x: List[float],
                           exit_threshold: float = 0.85) -> "TriageVector":
        """Forward with BranchyNet early exits.

        Evaluates exit heads at l2 and l4 before the final l5.
        If any exit has crash_risk >= exit_threshold or <= 1-exit_threshold
        (high confidence), returns early for compute savings.

        Args:
            x: input feature vector
            exit_threshold: confidence threshold for early exit (default 0.85)

        Returns:
            TriageVector from the earliest exit that meets confidence threshold,
            or from l5 if no early exit triggers.
        """
        if not getattr(self, '_exit_heads_built', False):
            self._build_early_exit_heads()

        self._caches = {}
        h = x

        # Attention
        h = self.attn.forward(h)
        self._caches["attn_out"] = h

        # Layer 1
        h = self.l1.forward(h)
        h = self.ln1.forward(h)
        h = self.relu1.forward(h)
        h = self.drop1.forward(h)

        # Layer 2 + Exit
        h = self.l2.forward(h)
        h_ln = self.ln2.forward(h)
        h_r = self.relu2.forward(h_ln)

        # Exit 1: from l2 output → 4 signals
        exit1_raw = self._exit_l2.forward(h_r)
        exit1 = self.sigmoid.forward(exit1_raw)
        if any(v >= exit_threshold for v in exit1) or \
           any(v <= 1.0 - exit_threshold for v in exit1):
            return TriageVector.from_raw(exit1)

        h = self.drop2.forward(h_r)

        # Layer 3 + Exit (at l3/l4 boundary, 32-dim)
        h = self.l3.forward(h)
        h = self.ln3.forward(h)
        h = self.relu3.forward(h)

        exit2_raw = self._exit_l4.forward(h)
        exit2 = self.sigmoid.forward(exit2_raw)
        if any(v >= exit_threshold for v in exit2) or \
           any(v <= 1.0 - exit_threshold for v in exit2):
            return TriageVector.from_raw(exit2)

        # Layer 4
        h = self.l4.forward(h)
        self._caches["l4"] = h
        h = self.relu4.forward(h)
        self._caches["relu4_in"] = h

        # Layer 5 (main exit)
        h = self.l5.forward(h)
        out = self.sigmoid.forward(h)
        self._caches["sigmoid_out"] = out
        return TriageVector.from_raw(out)

    def backward(self, target: float):
        """Backprop through the network for a single (x, target) pair.
        
        All 4 output heads are trained on the same crash/no-crash target
        initially. Each head specialises via different random initialisation
        and gradient patterns.
        """
        self._params = ParameterGroup()  # fresh collection per backward

        pred_raw = self._caches["sigmoid_out"]  # list of 4 sigmoid outputs

        # dL/d_logit for each output: BCE(Sigmoid(z_i), target) = pred_i - target
        # one gradient per output head (4-dim)
        grad_logits = [p - target for p in pred_raw]

        # L5: Linear(16 → 4) → Sigmoid
        # The gradient above IS the combined BCE+sigmoid gradient at logit level
        # L5 backward: 4 logit gradients → dx (16-dim input gradient)
        dx5 = self.l5.backward(grad_logits, self._params, prefix="l5_")

        # L4: ReLU → Linear(32 → 16)
        # dx5 is 16-dim (input to L5 = output of ReLU4)
        relu4_in = self._caches["relu4_in"]
        dx4 = self.relu4.backward(dx5, relu4_in)
        dx4_lin = self.l4.backward(dx4, self._params, prefix="l4_")

        # L3: ReLU → LayerNorm → Linear(64 → 32)
        relu3_in = self._caches["relu3_in"]
        dx3_r = self.relu3.backward(dx4_lin, relu3_in)
        dx3_ln = self.ln3.backward(dx3_r, self._params, prefix="l3_")
        dx3 = self.l3.backward(dx3_ln, self._params, prefix="l3_")

        # L2: Dropout → ReLU → LayerNorm → Linear(64 → 64)
        dx2_d = self.drop2.backward(dx3)
        relu2_in = self._caches["relu2_in"]
        dx2_r = self.relu2.backward(dx2_d, relu2_in)
        dx2_ln = self.ln2.backward(dx2_r, self._params, prefix="l2_")
        dx2 = self.l2.backward(dx2_ln, self._params, prefix="l2_")

        # L1: Dropout → ReLU → LayerNorm → Linear(32 → 64)
        dx1_d = self.drop1.backward(dx2)
        relu1_in = self._caches["relu1_in"]
        dx1_r = self.relu1.backward(dx1_d, relu1_in)
        dx1_ln = self.ln1.backward(dx1_r, self._params, prefix="l1_")
        dx_lin = self.l1.backward(dx1_ln, self._params, prefix="l1_")

        # Attention: backprop into IntraFeatureAttention
        _ = self.attn.backward(dx_lin, self._params, prefix="attn_")

    def predict(self, x: List[float]) -> TriageVector:
        """Forward pass only (no training state). Returns TriageVector."""
        was_training = self._training
        self.train_mode(False)
        raw = self.forward(x)
        self.train_mode(was_training)
        return TriageVector.from_raw(raw)

    def predict_batch(self, X: List[List[float]]) -> List[TriageVector]:
        """Batch prediction. Returns list of TriageVectors."""
        was_training = self._training
        self.train_mode(False)
        results = [TriageVector.from_raw(self.forward(x)) for x in X]
        self.train_mode(was_training)
        return results

    def fit(self, X: List[List[float]], y: List[float],
            epochs: int = 10, batch_size: int = 32,
            dp: Optional["DifferentialPrivacy"] = None,
            verbose: bool = False) -> Dict[str, Any]:
        """
        Train the network using Adam optimiser with optional DP-SGD.

        Args:
            X: feature matrix (N × 32)
            y: target labels (0.0 = success, 1.0 = failure)
            epochs: training epochs
            batch_size: mini-batch size
            dp: optional DifferentialPrivacy instance for DP-SGD
            verbose: log per-epoch loss

        Returns:
            training summary dict
        """
        n = len(X)
        if n < 2:
            return {"trained": False, "reason": "insufficient samples", "samples": n}

        # Create optimiser on first fit
        if self._optim is None:
            self._optim = Adam(
                self._params, lr=self.lr,
                weight_decay=1e-4,
            )

        self.train_mode(True)
        epoch_losses = []

        for epoch in range(epochs):
            # Shuffle
            indices = list(range(n))
            random.shuffle(indices)

            total_loss = 0.0
            n_batches = 0

            for start in range(0, n, batch_size):
                batch_idx = indices[start:start + batch_size]
                batch_loss = 0.0

                for idx in batch_idx:
                    self.forward(X[idx])
                    raw_out = self._caches["sigmoid_out"]
                    # Sum BCE over all 4 output heads (trained on same crash target)
                    loss = sum(bce_loss(p, y[idx]) for p in raw_out)
                    batch_loss += loss
                    self.backward(y[idx])

                # Average gradients over batch
                scale = 1.0 / max(1, len(batch_idx))
                for _, _, g in self._params.params:
                    _scale_inplace(g, scale)

                # DP-SGD: clip + noise via DifferentialPrivacy module
                if dp is not None:
                    # Flatten all grads into one big vector
                    param_tensors = [g for _, _, g in self._params.params]
                    flat = _tensor_flatten(param_tensors)
                    dp.clip_gradient(flat, clip_norm=1.0)
                    dp.add_noise(flat)
                    # Unflatten back into parameter grads
                    offset = 0
                    for _, _, g in self._params.params:
                        g_flat_len = len(_tensor_flatten(g))
                        for i in range(len(g)):
                            if isinstance(g[i], list):
                                for j in range(len(g[i])):
                                    g[i][j] = flat[offset]
                                    offset += 1
                            else:
                                g[i] = flat[offset]
                                offset += 1
                else:
                    # Manual gradient clipping (fallback if no DP)
                    total_norm = math.sqrt(
                        sum(_l2_norm_sq(_tensor_flatten(g))
                            for _, _, g in self._params.params)
                    )
                    if total_norm > 1.0:
                        scale_clip = 1.0 / total_norm
                        for _, _, g in self._params.params:
                            _scale_inplace(g, scale_clip)

                # Optimiser step — pass current params
                self._optim.step(pg=self._params)
                self._params.zero_grad()

                total_loss += batch_loss
                n_batches += 1

            avg_loss = total_loss / max(1, n_batches)
            epoch_losses.append(avg_loss)
            if verbose:
                logger.debug(f"  Epoch {epoch + 1}/{epochs}: loss={avg_loss:.4f}")

        self.train_mode(False)

        self._has_trained = True

        # Compute final accuracy on training set (using crash_risk head)
        predictions = self.predict_batch(X)
        correct = sum(1 for p, t in zip(predictions, y) if abs(p.crash_risk - t) < 0.5)
        accuracy = correct / max(1, n)

        return {
            "trained": True,
            "samples": n,
            "epochs": epochs,
            "final_loss": round(epoch_losses[-1], 4) if epoch_losses else 0,
            "accuracy": round(accuracy, 4),
            "params": self._param_count,
        }

    # ── Serialisation ──

    def quantize(self, bits: int = 8) -> dict:
        """Quantize model weights to int8/16 for compact serialization.

        Returns a quantized state dict with scale/zero_point per weight matrix.
        Bits: 8 (int8, ~25% of float32 size) or 16 (int16, ~50%).
        """
        raw = self.to_dict(quantize_bits=0)
        params = raw.get("params", {})
        qstate = {}
        for name, values in params.items():
            if not isinstance(values, list) or not values:
                continue
            flat = _tensor_flatten(values) if isinstance(values[0], list) else values
            if not flat:
                continue
            vmin, vmax = min(flat), max(flat)
            if vmax - vmin < 1e-10:
                qstate[name] = {"scale": 1.0, "zero": 0, "data": [0] * len(flat)}
                continue
            levels = 2 ** bits - 1
            scale = (vmax - vmin) / levels
            zero = round(-vmin / scale)
            qdata = [max(0, min(levels, round(v / scale) + zero)) for v in flat]
            qstate[name] = {"scale": scale, "zero": zero, "data": qdata}
        return qstate

    def dequantize(self, qstate: dict) -> dict:
        """Restore quantized state dict to float weights.

        Returns {name: [float_values]} (1D arrays, caller must reshape).
        """
        result = {}
        for name, q in qstate.items():
            scale, zero, data = q["scale"], q["zero"], q["data"]
            result[name] = [(d - zero) * scale for d in data]
        return result

    def to_dict(self, quantize_bits: int = 0) -> dict:
        W = lambda l: l.W
        b = lambda l: l.b
        g = lambda l: l.gamma if l.affine else None
        bt = lambda l: l.beta if l.affine else None
        return {
            "n_features": self.n_features,
            "hidden_dim": self.hidden_dim,
            "dropout": self.dropout_p,
            "arch": "DeepRiskNet",
            "version": 5,
            "use_temporal": self.use_temporal,
            "temporal_buffer_size": self.temporal_buffer_size,
            "params": {
                # Multi-head attention (version 3)
                "attn_n_heads": self.attn.n_heads,
                "attn_d_model": self.attn.d_model,
                "attn_embed": self.attn.embed,
                "attn_w_q": self.attn.w_q,
                "attn_w_k": self.attn.w_k,
                "attn_w_v": self.attn.w_v,
                "attn_w_o": self.attn.w_o,
                "attn_ln_g": self.attn.ln_gamma,
                "attn_ln_b": self.attn.ln_beta,
                "attn_w_out": self.attn.w_out,
                "attn_b_out": self.attn.b_out,
                # MLP layers
                "l1_W": W(self.l1), "l1_b": b(self.l1),
                "ln1_g": g(self.ln1), "ln1_b": bt(self.ln1),
                "l2_W": W(self.l2), "l2_b": b(self.l2),
                "ln2_g": g(self.ln2), "ln2_b": bt(self.ln2),
                "l3_W": W(self.l3), "l3_b": b(self.l3),
                "ln3_g": g(self.ln3), "ln3_b": bt(self.ln3),
                "l4_W": W(self.l4), "l4_b": b(self.l4),
                "l5_W": W(self.l5), "l5_b": b(self.l5),
                # Temporal conv (version 5)
                "temporal_state": (self.temporal_conv.get_state()
                                   if self.temporal_conv else None),
            },
            "optim": self._optim.state_dict() if self._optim else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeepRiskNet":
        version = d.get("version", 1)
        net = cls(
            n_features=d.get("n_features", 32),
            hidden_dim=d.get("hidden_dim", 64),
            dropout=d.get("dropout", 0.1),
            use_temporal=d.get("use_temporal", False),
            temporal_buffer_size=d.get("temporal_buffer_size", 8),
        )
        W = lambda l, k: d["params"].get(k, l.W)
        b_ = lambda l, k: d["params"].get(k, l.b)

        params = d.get("params", {})

        if "l1_W" in params:
            net.l1.W = params["l1_W"]
        if "l1_b" in params:
            net.l1.b = params["l1_b"]
        if "ln1_g" in params and net.ln1.gamma is not None:
            net.ln1.gamma = params["ln1_g"]
            net.ln1.beta = params.get("ln1_b", net.ln1.beta)
        if "l2_W" in params:
            net.l2.W = params["l2_W"]
        if "l2_b" in params:
            net.l2.b = params["l2_b"]
        if "ln2_g" in params and net.ln2.gamma is not None:
            net.ln2.gamma = params["ln2_g"]
            net.ln2.beta = params.get("ln2_b", net.ln2.beta)
        if "l3_W" in params:
            net.l3.W = params["l3_W"]
        if "l3_b" in params:
            net.l3.b = params["l3_b"]
        if "ln3_g" in params and net.ln3.gamma is not None:
            net.ln3.gamma = params["ln3_g"]
            net.ln3.beta = params.get("ln3_b", net.ln3.beta)
        if "l4_W" in params:
            net.l4.W = params["l4_W"]
        if "l4_b" in params:
            net.l4.b = params["l4_b"]
        if "l5_W" in params:
            net.l5.W = params["l5_W"]
            # Version 4: l5 has 4 output heads (triple vector)
            # Old models (v1-3) have l5_W = 1×16; migrate by repeating row 4×
            if version < 4 and len(net.l5.W) == 1:
                net.l5.W = [net.l5.W[0][:], net.l5.W[0][:], net.l5.W[0][:], net.l5.W[0][:]]
        if "l5_b" in params:
            net.l5.b = params["l5_b"]
            if version < 4 and len(net.l5.b) == 1:
                net.l5.b = [net.l5.b[0]] * 4

        # Version 3: multi-head attention
        if version >= 3 and "attn_n_heads" in params:
            n_heads = params["attn_n_heads"]
            d_model = params.get("attn_d_model", 32)
            net.attn = IntraFeatureAttention(net.n_features, d_model, n_heads)
            if "attn_embed" in params:
                net.attn.embed = params["attn_embed"]
            if "attn_w_q" in params:
                net.attn.w_q = params["attn_w_q"]
            if "attn_w_k" in params:
                net.attn.w_k = params["attn_w_k"]
            if "attn_w_v" in params:
                net.attn.w_v = params["attn_w_v"]
            if "attn_w_o" in params:
                net.attn.w_o = params["attn_w_o"]
            if "attn_ln_g" in params:
                net.attn.ln_gamma = params["attn_ln_g"]
            if "attn_ln_b" in params:
                net.attn.ln_beta = params["attn_ln_b"]
            if "attn_w_out" in params:
                net.attn.w_out = params["attn_w_out"]
            if "attn_b_out" in params:
                net.attn.b_out = params["attn_b_out"]
        # Version 1/2: backward compat single-head attention
        elif "attn_embed" in params:
            net.attn.embed = params["attn_embed"]
            # Convert single-head w_q/w_k/w_v → 1-head list
            single_w_q = params["attn_w_q"]
            single_w_k = params["attn_w_k"]
            single_w_v = params["attn_w_v"]
            d_model = len(single_w_q)  # square matrix
            if d_model == 16:
                # Old single-head d_model=16 → new d_model=32 requires rebuild
                # Pad old embed (32×16) to 32×32 with zeros
                old_embed = params.get("attn_embed", [])
                net.attn = IntraFeatureAttention(net.n_features, d_model=32, n_heads=4)
                if old_embed and len(old_embed[0]) == 16:
                    net.attn.embed = [row + [0.0] * 16 for row in old_embed]
                else:
                    net.attn.embed = params.get("attn_embed", net.attn.embed)
                # w_out also needs padding (old: n_features×16, new: n_features×32)
                if "attn_w_out" in params:
                    old_w_out = params["attn_w_out"]
                    if old_w_out and len(old_w_out[0]) == 16:
                        net.attn.w_out = [row + [0.0] * 16 for row in old_w_out]
                    else:
                        net.attn.w_out = old_w_out
                if "attn_b_out" in params:
                    old_b_out = params["attn_b_out"]
                    if len(old_b_out) == 16:
                        net.attn.b_out = old_b_out + [0.0] * 16
                    else:
                        net.attn.b_out = old_b_out
            else:
                # d_model=32 → single head → add to list
                net.attn.d_k = d_model
                net.attn.w_q = [single_w_q]
                net.attn.w_k = [single_w_k]
                net.attn.w_v = [single_w_v]
                # w_o defaults to identity since single-head
                if "attn_w_out" in params:
                    net.attn.w_out = params["attn_w_out"]
                if "attn_b_out" in params:
                    net.attn.b_out = params["attn_b_out"]

        # Version 5: temporal conv
        if version >= 5 and net.temporal_conv is not None:
            temporal_state = params.get("temporal_state")
            if temporal_state:
                net.temporal_conv.load_state(temporal_state)

        return net

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, s: str) -> "DeepRiskNet":
        return cls.from_dict(json.loads(s))

    # ── Convenience (compat with old RandomForest API) ──

    def empty(self) -> bool:
        """Return True if model was never trained (no training history)."""
        return not getattr(self, '_has_trained', False)

    def size_bytes(self) -> int:
        return len(self.to_json().encode("utf-8"))

    def model_size(self) -> str:
        size = self.size_bytes()
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / 1024 / 1024:.1f} MB"

    # ── Legacy API compat ──

    @property
    def trees(self) -> List:
        """Return list containing self (compat with old RandomForest API)."""
        return [self]

    @property
    def oob_errors(self) -> List[float]:
        """No OOB errors in NN — return empty list."""
        return []

    def feature_importance(self) -> List[float]:
        """
        Approximate feature importance via first-layer weight magnitudes.
        Returns a normalised score per input feature (32-dim).
        """
        # Use L1 norm of W1 per input feature
        W1 = self.l1.W  # [64 × 32] matrix
        n_features = len(W1[0]) if W1 else 32
        importance = [0.0] * n_features
        for col in range(n_features):
            importance[col] = sum(abs(W1[row][col]) for row in range(len(W1)))
        total = sum(importance) or 1.0
        return [round(v / total, 4) for v in importance]

    def consolidate(self, prune_threshold: float = 0.01,
                    freeze_thresh: float = 0.8,
                    min_samples: int = 10,
                    target_ram_kb: Optional[int] = None) -> dict:
        """
        Neural network consolidation (no pruning needed for NN — weights are dense).

        For NN, consolidation = weight decay + optional magnitude-based pruning.
        Since weights are continuous, we just report stats.
        """
        size_before = self.size_bytes()
        # No-op for NN (all weights are dense)
        return {
            "pruned": 0,
            "frozen": 0,
            "size_before": self.model_size(),
            "size_after": self.model_size(),
            "target_kb": target_ram_kb,
            "hit_target": True,
            "trees_remaining": 1,
        }

    def prune_inactive(self, threshold: float = 0.01) -> int:
        """No-op for NN."""
        return 0

    def prune_by_percentile(self, keep_ratio: float) -> int:
        """No-op for NN."""
        return 0

    def freeze_important(self, imp_thresh: float = 0.8,
                         min_samples: int = 10) -> int:
        """No-op for NN."""
        return 0

    def to_dict_sparse(self) -> dict:
        """Alias for gossip-friendly serialisation."""
        d = self.to_dict()
        d["format"] = "nn_dense"
        return d

    def to_json_sparse(self) -> str:
        return json.dumps(self.to_dict_sparse(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict_sparse(cls, d: dict) -> "DeepRiskNet":
        return cls.from_dict(d)

    @classmethod
    def from_json_sparse(cls, s: str) -> "DeepRiskNet":
        return cls.from_json(s)


# ════════════════════════════════════════════════════════════════
#  Legacy compatibility aliases
# ════════════════════════════════════════════════════════════════

# These names are consumed by other modules (snapshot.py, api.py, etc.)
# Map them to the new model for backward compatibility.

DecisionTree = DeepRiskNet  # Not a tree any more, but API shape is close enough
RandomForest = DeepRiskNet  # Not a forest any more, but same interface
DecisionNode = None  # Deprecated — removed


def _scale_inplace(t: list, s: float):
    """Scale all elements in-place."""
    if isinstance(t, list):
        if t and not isinstance(t[0], list):
            for i in range(len(t)):
                t[i] *= s
        else:
            for sub in t:
                _scale_inplace(sub, s)
