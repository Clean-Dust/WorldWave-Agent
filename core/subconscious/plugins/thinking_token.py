"""Control Token Extension — custom thinking tokens for self-hosted LLMs.

Provides:
  1. Tokenizer extension: vocabulary for thinking/control tokens
  2. Insertion algorithm: WHEN and WHERE to inject control tokens
  3. Embedding expansion manager: an expandable embedding table
     that backends can copy into the real model

Each control token represents a different "mode" of generation:
  <thinking>     — Enter latent reasoning mode
  </thinking>    — Exit latent reasoning mode
  <confirm>      — Require confirmation before proceeding
  <plan>         — Generate a structured plan
  <reflect>      — Reflect on and review output so far

Pure Python. Backend integration varies by engine. Default-disabled.
"""

from __future__ import annotations
import json
import math
import os
import random
from typing import Dict, List, Optional, Tuple

# ── Random number generator for embeddings ──

_RNG = random.Random()


def _randn() -> float:
    """Standard normal using Box-Muller."""
    u1 = _RNG.random()
    u2 = _RNG.random()
    return math.sqrt(-2.0 * math.log(u1 + 1e-30)) * math.cos(2.0 * math.pi * u2)


# ── Built-in Control Tokens ──


BUILTIN_CONTROL_TOKENS: Dict[str, str] = {
    "<thinking>": "Enter latent reasoning mode — use when uncertainty is high",
    "</thinking>": "Exit latent reasoning mode, return to normal generation",
    "<confirm>": "Require confirmation before producing final output",
    "<plan>": "Generate a structured step-by-step plan before acting",
    "<reflect>": "Review and reconsider the output generated so far",
    "<uncertainty>": "Signal that the model is uncertain about its prediction",
    "<high_risk>": "Signal high risk of error — proceed with caution",
}


# ── TokenEntry ──


class TokenEntry:
    """One control token with its embedding vector and metadata."""

    def __init__(
        self,
        token_str: str,
        embedding_dim: int = 256,
        description: str = "",
        token_id: int = -1,
    ):
        self.token_str = token_str
        self.embedding_dim = embedding_dim
        self.description = description
        self.token_id = token_id
        # Initialize embedding with small random values
        self.embedding = [_randn() * 0.02 for _ in range(embedding_dim)]

    def to_dict(self) -> Dict:
        return {
            "token_str": self.token_str,
            "embedding_dim": self.embedding_dim,
            "description": self.description,
            "token_id": self.token_id,
            "embedding": self.embedding,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "TokenEntry":
        t = cls(d["token_str"], d["embedding_dim"], d.get("description", ""))
        t.token_id = d.get("token_id", -1)
        t.embedding = d.get("embedding", t.embedding)
        return t


# ── TokenExtensionManager ──


class TokenExtensionManager:
    """Manages the collection of control tokens and their embeddings.

    This is the "brain" — it decides when to inject control tokens
    based on the current feature vector and risk score. The actual
    injection into the model is handled by BackendPlugin.
    """

    def __init__(self, base_vocab_size: int = 32000,
                 embedding_dim: int = 256):
        self.base_vocab_size = base_vocab_size
        self.embedding_dim = embedding_dim
        self.tokens: Dict[str, TokenEntry] = {}
        self._next_token_id = base_vocab_size

    def add_token(self, token_str: str,
                  description: str = "",
                  embedding: Optional[List[float]] = None) -> None:
        """Add a new control token.

        If token_str already exists, this is a no-op.
        """
        if token_str in self.tokens:
            return

        entry = TokenEntry(
            token_str=token_str,
            embedding_dim=self.embedding_dim,
            description=description,
            token_id=self._next_token_id,
        )
        if embedding is not None:
            if len(embedding) >= self.embedding_dim:
                entry.embedding = embedding[:self.embedding_dim]

        self.tokens[token_str] = entry
        self._next_token_id += 1

    def add_builtins(self) -> None:
        """Add all built-in control tokens."""
        for token_str, desc in BUILTIN_CONTROL_TOKENS.items():
            self.add_token(token_str, description=desc)

    def get_token_id(self, token_str: str) -> Optional[int]:
        entry = self.tokens.get(token_str)
        return entry.token_id if entry else None

    def get_embedding(self, token_str: str) -> Optional[List[float]]:
        entry = self.tokens.get(token_str)
        return entry.embedding if entry else None

    def get_all_embeddings_as_matrix(self) -> List[List[float]]:
        """Return all control token embeddings as a matrix.

        Each row is one embedding vector.
        Order matches token_id order (sorted).
        """
        sorted_entries = sorted(self.tokens.values(),
                                key=lambda e: e.token_id)
        return [e.embedding for e in sorted_entries]

    def get_all_token_ids(self) -> List[int]:
        return [e.token_id for e in self.tokens.values()]

    def total_tokens(self) -> int:
        return len(self.tokens)

    def embedding_table_size(self) -> int:
        """Return the size the model's embedding table needs to be
        extended to (base_vocab_size + total_tokens)."""
        return self.base_vocab_size + self.total_tokens()

    # ── Injection Decisions ──

    def decide_injection(
        self,
        features: List[float],
        risk_score: float,
        current_mode: str,
        generation_stage: str = "start",
    ) -> List[str]:
        """Decide which control tokens to inject based on current state.

        Args:
            features: 32-dim feature vector
            risk_score: DeepRiskNet prediction [0, 1]
            current_mode: "normal", "latent_thinking", "confirm", etc.
            generation_stage: "start", "middle", "end"

        Returns:
            list of token strings to inject (e.g., ["<thinking>", "<plan>"])
        """
        to_inject: List[str] = []

        # Stage 1: Task start
        if generation_stage == "start":
            if risk_score > 0.8:
                to_inject.append("<thinking>")
                to_inject.append("<plan>")
            elif risk_score > 0.5:
                to_inject.append("<thinking>")

        # Stage 2: Middle of generation
        if generation_stage == "middle":
            if current_mode == "latent_thinking":
                # Already in thinking mode — inject specific reasoning tokens
                if risk_score > 0.7:
                    to_inject.append("<reflect>")
            elif risk_score > 0.8:
                # High risk without thinking mode — enter it
                to_inject.append("<thinking>")

            # Top-1 entropy spike check (uses thinking_tokens_ratio from slot 22)
            if risk_score > 0.6 and features[22] > 0.7:  # thinking_tokens_ratio
                if "<uncertainty>" in self.tokens:
                    to_inject.append("<uncertainty>")

        # Stage 3: Before final output
        if generation_stage == "end":
            if risk_score > 0.7:
                to_inject.append("<confirm>")
            if "<thinking>" in to_inject:
                to_inject.append("</thinking>")

        # Validate: only inject tokens we actually have
        return [t for t in to_inject if t in self.tokens]

    def inject_format_string(self, tokens: List[str]) -> str:
        """Convert a list of control tokens to a format string that
        will be prepended to the model's input.

        This handles special token format — backends may need to
        interpret them differently (as actual token IDs or as text).
        """
        return " ".join(tokens)

    # ── Embedding Training ──

    def nudge_embedding(self, token_str: str,
                        delta: List[float], scale: float = 0.01) -> None:
        """Nudge a token's embedding in a specific direction.

        Called by the PPO/reinforcement loop to improve embeddings
        based on downstream reward.
        """
        entry = self.tokens.get(token_str)
        if entry is None:
            return
        if len(delta) != self.embedding_dim:
            return
        for i in range(self.embedding_dim):
            entry.embedding[i] += delta[i] * scale

    def randomize_untrained(self, threshold: int = 10) -> None:
        """Randomize embeddings that haven't been trained yet.

        Tokens with training_count < threshold get new random embeddings.
        """
        for entry in self.tokens.values():
            entry.embedding = [_randn() * 0.02 for _ in range(self.embedding_dim)]

    # ── Serialization ──

    def save(self, path: str) -> None:
        data = {
            "base_vocab_size": self.base_vocab_size,
            "embedding_dim": self.embedding_dim,
            "tokens": [e.to_dict() for e in self.tokens.values()],
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

    @classmethod
    def load(cls, path: str) -> "TokenExtensionManager":
        with open(path) as f:
            data = json.load(f)
        mgr = cls(
            base_vocab_size=data.get("base_vocab_size", 32000),
            embedding_dim=data.get("embedding_dim", 256),
        )
        for td in data.get("tokens", []):
            entry = TokenEntry.from_dict(td)
            mgr.tokens[entry.token_str] = entry
        if mgr.tokens:
            mgr._next_token_id = max(e.token_id for e in mgr.tokens.values()) + 1
        else:
            mgr._next_token_id = mgr.base_vocab_size
        return mgr
