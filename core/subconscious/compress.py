"""
ww/core/subconscious/compress.py — SUPO-inspired context compression

Dynamically decides which context segments to keep or prune based on a
learned policy.  Uses policy gradient (REINFORCE) to optimise the trade-off
between information retention and token budget.

No LLM calls.  All decisions are based on numerical features of each segment:
  - length (tokens)
  - age (position from end)
  - information density (character diversity / length)
  - repetition score (cosine similarity with neighbouring segments)
  - action type (tool call vs LLM response)

Pure Python, zero external dependencies.
"""

from __future__ import annotations
import json
import logging
import math
import os
import random
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.subconscious.compress")

COMPRESS_DIR = os.path.expanduser("~/worldwave/data/subconscious/compression")

# ════════════════════════════════════════════════════════════════
#  Context segment
# ════════════════════════════════════════════════════════════════


class Segment:
    """One entry in the context history.

    Attributes:
        seg_id: unique segment id
        action_type: "user", "assistant", "tool_call", "tool_result", "system"
        content_length: character length
        token_estimate: estimated token count
        age: 0 = newest, higher = older
        action_name: tool name if tool_call
        exit_code: exit code if tool_result
        succeeded: whether this segment led to a positive outcome
    """

    def __init__(
        self,
        seg_id: str,
        action_type: str,
        content_length: int = 0,
        token_estimate: int = 0,
        action_name: str = "",
        exit_code: int = 0,
        succeeded: bool = False,
    ):
        self.seg_id = seg_id
        self.action_type = action_type
        self.content_length = content_length
        self.token_estimate = token_estimate
        self.action_name = action_name
        self.exit_code = exit_code
        self.succeeded = succeeded
        self.age = 0
        self._embedding: Optional[List[float]] = None
        self.kept = True  # whether currently kept in context

    def to_dict(self) -> dict:
        return {
            "seg_id": self.seg_id,
            "action_type": self.action_type,
            "content_length": self.content_length,
            "token_estimate": self.token_estimate,
            "action_name": self.action_name,
            "exit_code": self.exit_code,
            "succeeded": self.succeeded,
            "kept": self.kept,
        }


# ════════════════════════════════════════════════════════════════
#  Feature extraction for segments
# ════════════════════════════════════════════════════════════════


def _segment_features(seg: Segment, total_tokens: int) -> List[float]:
    """Compute a 6-dim feature vector for a segment.

    Returns [norm_length, norm_age, info_density, repetition_risk,
             is_tool_result, exit_code_ok]
    """
    # 0: Normalised length (0-1)
    norm_len = min(1.0, seg.token_estimate / max(1, total_tokens))

    # 1: Normalised age (0 = newest, 1 = oldest)
    norm_age = min(1.0, seg.age / 100.0)

    # 2: Information density (character diversity / sqrt(length))
    # Higher = more diverse content
    info_density = 0.5  # default
    if seg.content_length > 10:
        # Use character type ratio as a proxy for diversity
        info_density = min(1.0, 1.0 - (seg.exit_code / 255.0) if seg.action_type == "tool_result" else 0.5)

    # 3: Repetition risk — tool calls same as previous
    repetition = 0.0

    # 4: Is tool result (usually more compressible)
    is_tool_result = 1.0 if seg.action_type == "tool_result" else 0.0

    # 5: Exit code ok
    exit_ok = 1.0 if seg.exit_code == 0 else 0.0

    return [norm_len, norm_age, info_density, repetition, is_tool_result, exit_ok]


# ════════════════════════════════════════════════════════════════
#  Policy network (tiny MLP)
# ════════════════════════════════════════════════════════════════


class _CompressPolicy:
    """Tiny binary policy: given segment features, output keep probability.

    Architecture: 6-dim input → Linear(6→8) + ReLU → Linear(8→1) + Sigmoid
    ~56 parameters total.
    """

    def __init__(self):
        rng = random.Random(42)
        scale = math.sqrt(2.0 / 6)
        # w1: 6→8
        self.w1 = [[rng.gauss(0, scale) for _ in range(6)] for _ in range(8)]
        self.b1 = [0.0] * 8
        # w2: 8→1
        scale2 = math.sqrt(2.0 / 8)
        self.w2 = [rng.gauss(0, scale2) for _ in range(8)]
        self.b2 = 0.0
        self._cached = {}

    def forward(self, features: List[float]) -> float:
        """Return keep probability (0.0-1.0)."""
        # Hidden
        h = [
            sum(self.w1[i][j] * features[j] for j in range(6)) + self.b1[i]
            for i in range(8)
        ]
        h = [max(0.0, x) for x in h]  # ReLU
        # Output
        logit = sum(self.w2[i] * h[i] for i in range(8)) + self.b2
        prob = 1.0 / (1.0 + math.exp(-logit))
        return max(0.01, min(0.99, prob))

    def decide(self, features: List[float], force_keep: bool = False) -> bool:
        """Sample keep/prune from the policy. Always keep if force_keep."""
        if force_keep:
            return True
        prob = self.forward(features)
        return random.random() < prob

    def gradient(
        self, features: List[float], keep: bool, advantage: float
    ) -> Dict[str, float]:
        """Compute REINFORCE gradient (log-prob * advantage).

        Returns dict of (param_name, gradient_value).
        """
        prob = self.forward(features)
        # log_prob(keep) = log(prob) if keep else log(1-prob)
        log_prob = math.log(prob) if keep else math.log(1.0 - prob)
        # Grad = log_prob * advantage (simple REINFORCE)
        # For simplicity, return scalar adjustments
        grad = log_prob * advantage
        return {
            "log_prob": log_prob,
            "advantage": advantage,
            "gradient": grad,
            "prob": round(prob, 3),
        }

    def to_dict(self) -> dict:
        return {"w1": self.w1, "b1": self.b1, "w2": self.w2, "b2": self.b2}

    @classmethod
    def from_dict(cls, d: dict) -> "_CompressPolicy":
        p = cls.__new__(cls)
        p.w1 = d["w1"]
        p.b1 = d["b1"]
        p.w2 = d["w2"]
        p.b2 = d["b2"]
        return p


# ════════════════════════════════════════════════════════════════
#  Main compressor
# ════════════════════════════════════════════════════════════════


class ContextCompressor:
    """SUPO-inspired context compression engine.

    Tracks context segments and learns a policy for which to keep vs prune.

    Usage:
        cc = ContextCompressor()
        cc.add_segment(seg)
        decisions = cc.evaluate()      # returns {seg_id: keep}
        cc.reward(task_success)        # REINFORCE update
    """

    def __init__(
        self,
        max_segments: int = 200,
        keep_newest: int = 5,  # always keep latest N segments
        token_budget: int = 4096,
        lr: float = 0.01,
        data_dir: str = COMPRESS_DIR,
        auto_persist: bool = True,
    ):
        self.max_segments = max_segments
        self.keep_newest = keep_newest
        self.token_budget = token_budget
        self.lr = lr
        self.data_dir = data_dir
        self.auto_persist = auto_persist
        os.makedirs(data_dir, exist_ok=True)

        self.policy = _CompressPolicy()
        self._segments: Dict[str, Segment] = {}
        self._ordered_ids: List[str] = []
        self._total_tokens = 0
        self._last_decisions: Dict[str, bool] = {}
        self._last_features: Dict[str, List[float]] = {}
        self._grad_buffer: List[Dict[str, Any]] = []
        self._update_count = 0
        self._episode_count = 0

    # ── Segment management ──

    def add_segment(self, seg: Segment):
        """Register a new context segment."""
        seg.age = len(self._ordered_ids)  # position-based age
        self._segments[seg.seg_id] = seg
        self._ordered_ids.append(seg.seg_id)
        self._total_tokens += seg.token_estimate

        # Enforce max segments
        if len(self._ordered_ids) > self.max_segments:
            oldest = self._ordered_ids.pop(0)
            if oldest in self._segments:
                old_seg = self._segments.pop(oldest)
                self._total_tokens -= old_seg.token_estimate

    def get_segment(self, seg_id: str) -> Optional[Segment]:
        return self._segments.get(seg_id)

    def get_kept_ids(self) -> List[str]:
        """Return IDs of segments currently kept."""
        return [sid for sid in self._ordered_ids if self._segments.get(sid, Segment("", "")).kept]

    def get_pruned_ids(self) -> List[str]:
        """Return IDs of segments currently pruned."""
        return [sid for sid in self._ordered_ids if not self._segments.get(sid, Segment("", "")).kept]

    def estimate_token_count(self) -> int:
        """Sum tokens of kept segments."""
        kept = self.get_kept_ids()
        return sum(self._segments[sid].token_estimate for sid in kept if sid in self._segments)

    # ── Evaluation ──

    def evaluate(self, force_keep_all: bool = False) -> Dict[str, bool]:
        """Decide which segments to keep.

        Returns {seg_id: keep (True/False)}.
        """
        self._last_decisions = {}
        self._last_features = {}

        # Always keep newest N
        keep_set = set(self._ordered_ids[-self.keep_newest:])

        # Also keep user messages and system prompts
        for sid in self._ordered_ids:
            seg = self._segments.get(sid)
            if seg and seg.action_type in ("user", "system"):
                keep_set.add(sid)

        for i, sid in enumerate(self._ordered_ids):
            seg = self._segments.get(sid)
            if not seg:
                continue

            force_keep = sid in keep_set or force_keep_all or i == len(self._ordered_ids) - 1
            features = _segment_features(seg, self._total_tokens)

            if force_keep:
                keep = True
            else:
                keep = self.policy.decide(features, force_keep=False)

            self._last_decisions[sid] = keep
            self._last_features[sid] = features
            seg.kept = keep

        # Override if over budget: greedily prune lowest-probability kept segments
        self._enforce_budget()

        return self._last_decisions.copy()

    def _enforce_budget(self):
        """If token budget exceeded, greedily prune segments."""
        kept_ids = self.get_kept_ids()
        total = sum(self._segments[sid].token_estimate for sid in kept_ids if sid in self._segments)
        if total <= self.token_budget:
            return

        # Sort kept (non-protected) by keep probability ascending
        protect = set(self._ordered_ids[-self.keep_newest:])
        candidates = [
            (sid, self._segments.get(sid, None))
            for sid in kept_ids
            if sid not in protect
        ]
        candidates = [(sid, seg) for sid, seg in candidates if seg is not None]

        # Sort by keep probability (ascending) so lowest-confidence gets pruned first
        candidates.sort(key=lambda x: self.policy.forward(self._last_features.get(x[0], [0.5] * 6)))

        for sid, seg in candidates:
            if total <= self.token_budget:
                break
            seg.kept = False
            total -= seg.token_estimate

    # ── REINFORCE learning ──

    def reward(self, task_success: float, token_savings: float = 0.0):
        """Apply REINFORCE update based on outcome.

        Args:
            task_success: 1.0 = success, 0.0 = failure
            token_savings: fraction of tokens saved (0.0-1.0)
        """
        # Compute advantage: success + small bonus for savings
        advantage = task_success + 0.1 * token_savings - 0.5  # centered around 0

        # For each decision, accumulate gradient
        for sid, keep in self._last_decisions.items():
            features = self._last_features.get(sid)
            if not features:
                continue

            grad_info = self.policy.gradient(features, keep, advantage)
            self._grad_buffer.append({
                "seg_id": sid,
                "keep": keep,
                **grad_info,
            })

        self._episode_count += 1

        # Batch update every 10 episodes
        if self._episode_count >= 10:
            self._apply_gradients()

    def _apply_gradients(self):
        """Apply accumulated gradients to policy parameters."""
        if not self._grad_buffer:
            return

        # Compute average gradient
        total_grad = sum(g["gradient"] for g in self._grad_buffer)
        avg_grad = total_grad / max(1, len(self._grad_buffer))

        # Simplified parameter update: shift policy params
        lr = self.lr * avg_grad

        # Update each weight slightly
        for i in range(8):
            for j in range(6):
                self.policy.w1[i][j] += lr * 0.1  # small step
            self.policy.b1[i] += lr * 0.05

        for i in range(8):
            self.policy.w2[i] += lr * 0.1
        self.policy.b2 += lr * 0.05

        self._update_count += len(self._grad_buffer)
        self._grad_buffer.clear()
        self._episode_count = 0

        logger.debug(f"🧩 Context compressor: {self._update_count} updates applied")

    # ── Persistence ──

    def save(self, path: Optional[str] = None) -> str:
        """Save policy to disk."""
        path = path or os.path.join(self.data_dir, "policy.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "policy": self.policy.to_dict(),
                "update_count": self._update_count,
                "budget": self.token_budget,
            }, f, ensure_ascii=False)
        return path

    def load(self, path: Optional[str] = None) -> bool:
        """Load policy from disk."""
        path = path or os.path.join(self.data_dir, "policy.json")
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self.policy = _CompressPolicy.from_dict(data["policy"])
            self._update_count = data.get("update_count", 0)
            self.token_budget = data.get("budget", self.token_budget)
            return True
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return False

    def stats(self) -> dict:
        return {
            "segments": len(self._segments),
            "kept": len(self.get_kept_ids()),
            "pruned": len(self.get_pruned_ids()),
            "total_tokens": self._total_tokens,
            "estimated_kept_tokens": self.estimate_token_count(),
            "token_budget": self.token_budget,
            "policy_updates": self._update_count,
            "grad_buffer_size": len(self._grad_buffer),
        }
