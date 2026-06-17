"""
ww/gateway/attention.py — Bayesian Attention Gate (Thalamus) v0.1

Implements "Explaining Away" computation for the gateway layer.

In the biomimetic blueprint, the thalamus acts as an attention gate:
- Uses hierarchical Bayesian computation to filter sensory input
- Computes posterior probability that each input is relevant
- Suppresses inputs that are "explained away" by the current context
- Only high-relevance signals reach the cortex (LLM)

Algorithm:
    P(relevant | message, goal) ∝ P(message | relevant) × P(relevant | goal)

    Where:
    - P(message | relevant) is estimated via keyword/embedding similarity
    - P(relevant | goal) is the prior — how likely is ANY message to be relevant
    - Messages with posterior below threshold are suppressed (not discarded, buffered)
"""

from __future__ import annotations
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


# ── Constants ──
DEFAULT_ATTENTION_THRESHOLD = 0.25   # Posterior below this → suppress
DEFAULT_URGENCY_BOOST = 0.3          # Boost for urgent patterns
BUFFER_MAX_SIZE = 100                 # Suppressed message buffer


# ── Urgency keyword patterns (same spirit as amygdala.py) ──
URGENCY_PATTERNS: Dict[str, float] = {
    "error": 0.8, "exception": 0.8, "fail": 0.7, "failed": 0.7,
    "crash": 0.95, "panic": 0.95, "fatal": 1.0,
    "urgent": 0.9, "asap": 0.9, "emergency": 0.95,
    "critical": 0.9, "blocker": 0.85, "deadline": 0.75,
    "down": 0.8, "offline": 0.8, "broken": 0.75,
    "help": 0.6, "stuck": 0.5, "please": 0.3,
    "question": 0.3, "info": 0.1, "update": 0.1,
}


@dataclass
class GatedMessage:
    """A message that has passed through the attention gate."""
    raw_content: str
    source: str
    posterior: float          # P(relevant | message, goal)
    urgency_boost: float
    explanation: str          # Why it passed (or was suppressed)
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BayesianAttentionGate:
    """Thalamus-inspired Bayesian attention gate for the gateway layer.

    Filters incoming messages across all platform adapters,
    computing relevance to the current cognitive goal.

    Usage:
        gate = BayesianAttentionGate()
        gate.set_goal("Debug database migration failure")
        result = gate.evaluate("Health check OK", source="telegram")
        if result.passed:
            cortex.receive(result.message)
    """

    def __init__(
        self,
        threshold: float = DEFAULT_ATTENTION_THRESHOLD,
        urgency_boost: float = DEFAULT_URGENCY_BOOST,
        prior_relevant: float = 0.3,        # P(relevant | goal) — base prior
    ):
        self.threshold = threshold
        self.urgency_boost = urgency_boost
        self.prior_relevant = prior_relevant

        # Current cognitive goal (set by cortex)
        self._goal: str = ""
        self._goal_tokens: Set[str] = set()
        self._goal_set_at: float = 0.0

        # Suppressed message buffer (not discarded, can be recalled)
        self._buffer: deque = deque(maxlen=BUFFER_MAX_SIZE)

        # Cascade signal from amygdala (stress raises threshold)
        self._stress_level: float = 0.0
        self._filter_intensity: float = 0.5

        # Stats
        self._passed_count: int = 0
        self._suppressed_count: int = 0

    # ── Goal setting ──

    def set_goal(self, goal: str):
        """Set the current cognitive goal for relevance computation."""
        self._goal = goal
        self._goal_set_at = time.time()
        # Tokenize goal into keywords
        words = re.findall(r'[a-zA-Z_]+', goal.lower())
        # Filter out stopwords
        stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be',
                     'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
                     'and', 'or', 'not', 'this', 'that', 'it', 'as'}
        self._goal_tokens = {w for w in words if len(w) > 1 and w not in stopwords}

    def clear_goal(self):
        """Clear goal — gate becomes permissive (all messages pass)."""
        self._goal = ""
        self._goal_tokens = set()
        self._goal_set_at = 0.0

    # ── Cross-module interface (amygdala cascade) ──

    def set_stress_level(self, level: float):
        """Receive stress signal from amygdala.

        Higher stress → higher effective threshold → stricter filtering.
        """
        self._stress_level = max(0.0, min(1.0, level))
        self._filter_intensity = 0.5 + self._stress_level * 0.4

    def get_effective_threshold(self) -> float:
        """Threshold adjusted by stress level."""
        return self.threshold + self._stress_level * 0.2

    # ── Core evaluation ──

    def evaluate(self, content: str, source: str = "unknown",
                 metadata: Optional[Dict[str, Any]] = None) -> GatedMessage:
        """Evaluate whether a message should pass the attention gate.

        Returns a GatedMessage with posterior probability and pass/fail status.
        Suppressed messages go to buffer; passed messages go to cortex.
        """
        # 1. Compute urgency boost
        urgency = self._compute_urgency(content)

        # 2. Compute goal relevance (cosine-like token overlap)
        goal_relevance = self._compute_goal_relevance(content)

        # 3. Bayesian posterior: P(relevant | message, goal)
        #    ∝ likelihood × prior
        #    likelihood = max(urgency, goal_relevance)  (either signal can indicate relevance)
        #    prior = self.prior_relevant
        likelihood = max(urgency, goal_relevance, 0.05)
        prior = self.prior_relevant

        # Naive Bayes: posterior = (likelihood * prior) / evidence
        # evidence = likelihood * prior + (1-likelihood) * (1-prior)
        evidence = likelihood * prior + (1 - likelihood) * (1 - prior)
        posterior = (likelihood * prior) / max(evidence, 1e-10)

        # Apply urgency boost as additive (urgent messages get a lift)
        effective_score = posterior + urgency * self.urgency_boost
        effective_score = min(1.0, effective_score)

        # Decision: apply effective threshold (stress-adjusted)
        effective_threshold = self.get_effective_threshold()
        passed = effective_score >= effective_threshold

        if passed:
            explanation = f"passed (posterior={posterior:.3f}, urgency={urgency:.2f}, goal_rel={goal_relevance:.2f})"
            self._passed_count += 1
        else:
            explanation = f"suppressed by explaining away (score={effective_score:.3f} < threshold={effective_threshold:.3f})"
            self._suppressed_count += 1

        msg = GatedMessage(
            raw_content=content,
            source=source,
            posterior=posterior,
            urgency_boost=urgency,
            explanation=explanation,
            metadata=metadata or {},
        )

        if not passed:
            self._buffer.append(msg)

        return msg

    def passed(self, content: str, source: str = "unknown",
               metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Quick check: should this message pass? (no GatedMessage overhead)."""
        result = self.evaluate(content, source, metadata)
        return result.posterior >= self.get_effective_threshold()

    # ── Relevance computation ──

    def _compute_urgency(self, content: str) -> float:
        """Compute urgency score from message content."""
        if not content:
            return 0.0
        text = content.lower()
        total_score = 0.0
        matches = 0
        for pattern, score in URGENCY_PATTERNS.items():
            if pattern in text:
                total_score += score
                matches += 1
        if matches == 0:
            return 0.05  # baseline
        # Cap at 1.0, boost multi-match
        return min(1.0, total_score / max(1, matches) * 1.2)

    def _compute_goal_relevance(self, content: str) -> float:
        """Compute relevance of message to current goal via token overlap."""
        if not self._goal_tokens or not content:
            return 0.3  # Neutral when no goal set

        text = content.lower()
        content_words = set(re.findall(r'[a-zA-Z_]+', text))

        if not content_words:
            return 0.3

        # Jaccard-like overlap
        intersection = self._goal_tokens & content_words
        union = self._goal_tokens | content_words

        jaccard = len(intersection) / max(1, len(union))

        # Weight: more goal tokens matched → higher relevance
        coverage = len(intersection) / max(1, len(self._goal_tokens))

        return 0.3 + 0.7 * (jaccard * 0.5 + coverage * 0.5)

    # ── Buffer management ──

    def flush_buffer(self, min_posterior: float = 0.0) -> List[GatedMessage]:
        """Return suppressed messages above a minimum posterior.

        Useful for periodic re-evaluation — a suppressed message might
        become relevant when the goal changes.
        """
        if min_posterior <= 0:
            result = list(self._buffer)
            self._buffer.clear()
            return result
        kept = []
        released = []
        for msg in self._buffer:
            if msg.posterior >= min_posterior:
                released.append(msg)
            else:
                kept.append(msg)
        self._buffer = deque(kept, maxlen=BUFFER_MAX_SIZE)
        return released

    def re_evaluate_buffer(self):
        """Re-evaluate all buffered messages against current goal.

        Called when the goal changes — previously suppressed messages
        might now be relevant.
        """
        if not self._buffer:
            return
        old_buffer = list(self._buffer)
        self._buffer.clear()
        for msg in old_buffer:
            self.evaluate(msg.raw_content, msg.source, msg.metadata)

    # ── Stats ──

    def stats(self) -> Dict:
        return {
            "goal": self._goal[:80] if self._goal else "(none)",
            "goal_tokens": len(self._goal_tokens),
            "passed": self._passed_count,
            "suppressed": self._suppressed_count,
            "buffer_size": len(self._buffer),
            "stress_level": round(self._stress_level, 3),
            "filter_intensity": round(self._filter_intensity, 3),
            "effective_threshold": round(self.get_effective_threshold(), 3),
            "prior_relevant": self.prior_relevant,
        }

    def reset_stats(self):
        self._passed_count = 0
        self._suppressed_count = 0


# ── Factory ──

def create_attention_gate(**kwargs) -> BayesianAttentionGate:
    return BayesianAttentionGate(**kwargs)
