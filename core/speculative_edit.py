"""
Speculative Edit Engine — AI-powered next-edit prediction.

Cursor-style tab completion: predicts what code change the user
will make next based on:
  1. Code context (prefix + suffix around cursor)
  2. Recent edit history (what user just changed)
  3. Project patterns (from codebase index)

Usage:
  engine = SpeculativeEditEngine()
  prediction = engine.predict(prefix="def add(", suffix="\n    return", file_path="math_utils.py")
  # Returns the most likely code to insert at cursor position.

Architecture:
  - LLM-based prediction (uses configured model)
  - Falls back to simple pattern matching if LLM unavailable
  - Caches predictions for low-latency retries
  - Integrates with VS Code extension's inline completion provider
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("ww.speculative_edit")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class EditPrediction:
    """A predicted code edit."""
    text: str                      # The predicted text to insert
    confidence: float              # 0.0 - 1.0
    source: str = "llm"            # "llm", "pattern", "lsp", "history"
    position_offset: int = 0       # Cursor offset from prefix end
    replace_range: Optional[Tuple[int, int]] = None  # (start, end) if replacing
    latency_ms: float = 0.0
    timestamp: str = ""

    @property
    def display_text(self) -> str:
        """First line for inline display."""
        return self.text.split("\n")[0][:80]

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "source": self.source,
            "display": self.display_text,
        }


@dataclass
class EditContext:
    """Context for prediction."""
    prefix: str          # Code before cursor
    suffix: str          # Code after cursor
    file_path: str       # Current file
    language: str = ""   # Detected language
    recent_edits: List[Dict] = field(default_factory=list)  # [{type, text, position}]
    cursor_line: int = 0
    cursor_col: int = 0


# ── Engine ───────────────────────────────────────────────────────

class SpeculativeEditEngine:
    """Predicts next code edit using LLM with fallback strategies."""

    def __init__(self, llm_fn: Optional[Callable] = None):
        """
        Args:
            llm_fn: Callable(prompt) → str. If None, uses pattern matching only.
        """
        self._llm_fn = llm_fn
        self._cache: Dict[str, EditPrediction] = {}  # hash → prediction
        self._history: List[EditContext] = []         # Recent edit contexts
        self._max_history = 50
        self._max_cache = 200

    # ── Public API ───────────────────────────────────────────────

    def set_llm_fn(self, fn: Callable[[str], str]):
        """Set the LLM function for high-quality predictions."""
        self._llm_fn = fn

    def predict(
        self,
        prefix: str,
        suffix: str = "",
        file_path: str = "",
        language: str = "",
        cursor_line: int = 0,
        cursor_col: int = 0,
    ) -> Optional[EditPrediction]:
        """Predict what the user will type next.

        Args:
            prefix: Code before the cursor (last ~1000 chars)
            suffix: Code after the cursor (next ~500 chars)
            file_path: Current file path for context
            language: Detected programming language

        Returns:
            EditPrediction or None if no confident prediction.
        """
        start = time.time()

        # Build context
        ctx = EditContext(
            prefix=prefix[-2000:],
            suffix=suffix[:1000],
            file_path=file_path,
            language=language or self._detect_language(file_path, prefix),
            recent_edits=self._get_recent_edits(),
        )

        # Try cache first
        cache_key = self._cache_key(ctx)
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - float(cached.timestamp or 0) < 30:
                return cached

        # Strategy 1: LLM prediction (best quality)
        if self._llm_fn:
            prediction = self._predict_llm(ctx)
            if prediction and prediction.confidence > 0.3:
                self._save_prediction(ctx, prediction)
                prediction.latency_ms = (time.time() - start) * 1000
                return prediction

        # Strategy 2: Pattern matching
        prediction = self._predict_pattern(ctx)
        if prediction and prediction.confidence > 0.4:
            self._save_prediction(ctx, prediction)
            prediction.latency_ms = (time.time() - start) * 1000
            return prediction

        # Strategy 3: Simple heuristics
        prediction = self._predict_heuristic(ctx)
        if prediction:
            self._save_prediction(ctx, prediction)
            prediction.latency_ms = (time.time() - start) * 1000
        return prediction

    def predict_multiple(
        self, prefix: str, suffix: str = "", file_path: str = "",
        top_k: int = 3
    ) -> List[EditPrediction]:
        """Return top-k predictions for a completion popup."""
        primary = self.predict(prefix, suffix, file_path)
        if not primary:
            return []

        results = [primary]

        # Generate variations
        if self._llm_fn:
            variations = self._predict_llm_variations(prefix, suffix, file_path, top_k - 1)
            results.extend(variations)

        return results[:top_k]

    def record_accept(self, prediction: EditPrediction):
        """Record that the user accepted a prediction (reinforcement signal)."""
        log.debug("Accepted prediction: %s", prediction.display_text[:50])
        # Boost confidence of similar future predictions
        self._history.append(EditContext(
            prefix=prediction.text[:100],
            suffix="",
            file_path="",
            recent_edits=[{"type": "accept", "text": prediction.text, "position": 0}],
        ))

    def record_reject(self, prediction: EditPrediction):
        """Record that the user rejected a prediction."""
        log.debug("Rejected prediction: %s", prediction.display_text[:50])
        # Decrease confidence for this pattern
        self._history.append(EditContext(
            prefix=prediction.text[:100],
            suffix="",
            file_path="",
            recent_edits=[{"type": "reject", "text": prediction.text, "position": 0}],
        ))

    # ── Prediction Strategies ────────────────────────────────────

    def _predict_llm(self, ctx: EditContext) -> Optional[EditPrediction]:
        """Use LLM to predict next edit."""
        prompt = self._build_llm_prompt(ctx)
        try:
            response = self._llm_fn(prompt)
            text = self._extract_code(response)
            if text and len(text) > 0:
                confidence = self._estimate_confidence(text, ctx)
                return EditPrediction(
                    text=text,
                    confidence=confidence,
                    source="llm",
                    timestamp=str(time.time()),
                )
        except Exception as e:
            log.debug("LLM prediction failed: %s", e)
        return None

    def _predict_llm_variations(self, prefix: str, suffix: str, file_path: str,
                                 count: int) -> List[EditPrediction]:
        """Generate multiple completion variants."""
        ctx = EditContext(prefix=prefix[-2000:], suffix=suffix[:1000], file_path=file_path)
        prompt = self._build_llm_variations_prompt(ctx, count)
        try:
            response = self._llm_fn(prompt)
            variants = [v.strip() for v in response.split("---") if v.strip()]
            return [
                EditPrediction(text=v, confidence=0.4, source="llm", timestamp=str(time.time()))
                for v in variants[:count]
            ]
        except Exception:
            return []

    def _predict_pattern(self, ctx: EditContext) -> Optional[EditPrediction]:
        """Pattern-based prediction from edit history."""
        # Find similar prefixes in history
        prefix_last_line = ctx.prefix.split("\n")[-1].strip() if ctx.prefix else ""

        for past_ctx in reversed(self._history):
            if not past_ctx.recent_edits:
                continue
            past_last_line = past_ctx.prefix.split("\n")[-1].strip() if past_ctx.prefix else ""
            # Simple fuzzy match
            if self._line_similarity(prefix_last_line, past_last_line) > 0.6:
                edit_text = past_ctx.recent_edits[0].get("text", "")
                if edit_text:
                    return EditPrediction(
                        text=edit_text,
                        confidence=0.5,
                        source="history",
                        timestamp=str(time.time()),
                    )
        return None

    def _predict_heuristic(self, ctx: EditContext) -> Optional[EditPrediction]:
        """Simple heuristic-based prediction."""
        prefix = ctx.prefix.rstrip()
        if not prefix:
            return None

        # Common patterns
        patterns = []

        # Function call: starts with open paren
        if prefix.endswith("("):
            patterns.append((")", 0.7))
        # Dictionary/object: starts with {
        elif prefix.endswith("{"):
            patterns.append(("\n    \n}", 0.5))
        # List: starts with [
        elif prefix.endswith("["):
            patterns.append(("]", 0.8))
        # String: starts with quote
        elif prefix.endswith('"') or prefix.endswith("'"):
            patterns.append((self._close_string(prefix), 0.6))
        # Python: def/class at line start
        elif prefix.rstrip().endswith(":"):
            last_line = prefix.split("\n")[-1] if "\n" in prefix else prefix
            if last_line.strip().startswith(("def ", "class ", "if ", "for ", "while ", "with ", "try:")):
                indent = " " * (len(last_line) - len(last_line.lstrip()) + 4)
                patterns.append((f"\n{indent}pass", 0.4))
        # Import statement
        elif prefix.rstrip().endswith("import "):
            patterns.append(("*", 0.3))

        if patterns:
            text, conf = patterns[0]
            return EditPrediction(
                text=text, confidence=conf, source="pattern",
                timestamp=str(time.time()),
            )
        return None

    # ── Internals ────────────────────────────────────────────────

    def _build_llm_prompt(self, ctx: EditContext) -> str:
        """Build a prompt for the LLM to predict the next edit."""
        parts = [
            "You are a code completion engine. Predict exactly what code should appear "
            "at the cursor position (marked as <CURSOR>). Return ONLY the code to insert, "
            "no explanation, no markdown fencing.",
            "",
            "Context:",
        ]
        if ctx.file_path:
            parts.append(f"File: {ctx.file_path}")
        if ctx.language:
            parts.append(f"Language: {ctx.language}")

        parts.extend([
            "",
            "Code before cursor:",
            "```",
            ctx.prefix[-1500:],
            "<CURSOR>",
        ])

        if ctx.suffix:
            parts.extend([
                "```",
                "",
                "Code after cursor:",
                "```",
                ctx.suffix[:800],
            ])

        parts.extend([
            "```",
            "",
            "Complete the code at <CURSOR>:",
        ])

        return "\n".join(parts)

    def _build_llm_variations_prompt(self, ctx: EditContext, count: int) -> str:
        """Build a prompt for multiple completion variants."""
        base = self._build_llm_prompt(ctx)
        return base + f"\nProvide {count} alternative completions, separated by '---'."

    def _extract_code(self, response: str) -> str:
        """Extract pure code from LLM response (strip markdown, explanations)."""
        text = response.strip()
        # Remove markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```language)
            if len(lines) > 1:
                lines = lines[1:]
            # Remove last line if it's ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        # Remove leading/trailing explanation lines
        # Keep only the code portion
        return text.strip()

    def _estimate_confidence(self, text: str, ctx: EditContext) -> float:
        """Estimate how confident the prediction is."""
        score = 0.5  # Base confidence

        # Longer completions → higher confidence (more specific)
        if len(text) > 50:
            score += 0.15
        elif len(text) < 5:
            score -= 0.1

        # If prediction starts with expected tokens, boost
        prefix_end = ctx.prefix.rstrip()[-20:] if ctx.prefix else ""
        if text and self._is_expected_continuation(prefix_end, text):
            score += 0.2

        return min(1.0, max(0.1, score))

    @staticmethod
    def _is_expected_continuation(prefix: str, completion: str) -> bool:
        """Check if completion naturally follows the prefix."""
        # Paren matching
        if prefix.endswith("(") and completion.startswith((")", '"', "'", "{")):
            return True
        if prefix.endswith("[") and completion.startswith(("]", "{", '"', "'")):
            return True
        # Colon → newline + indent
        if prefix.rstrip().endswith(":") and completion.startswith("\n"):
            return True
        # Comma → space
        if prefix.endswith(",") and completion.startswith((" ", "\n")):
            return True
        return False

    @staticmethod
    def _line_similarity(a: str, b: str) -> float:
        """Simple line similarity for pattern matching."""
        if not a or not b:
            return 0.0
        # Jaccard on words
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union) if union else 0.0

    @staticmethod
    def _close_string(prefix: str) -> str:
        """Close an open string."""
        if prefix.endswith('"""'):
            return '"""'
        if prefix.endswith("'''"):
            return "'''"
        if prefix.endswith('"'):
            return '"'
        if prefix.endswith("'"):
            return "'"
        return ""

    @staticmethod
    def _detect_language(file_path: str, code: str) -> str:
        """Detect language from file extension or code content."""
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        lang_map = {
            "py": "python", "js": "javascript", "jsx": "javascript",
            "ts": "typescript", "tsx": "typescript", "go": "go",
            "rs": "rust", "java": "java", "cpp": "cpp", "c": "c",
            "rb": "ruby", "php": "php", "swift": "swift",
            "kt": "kotlin", "vue": "vue", "svelte": "svelte",
            "html": "html", "css": "css", "scss": "scss",
            "sh": "shell", "sql": "sql",
        }
        return lang_map.get(ext, "")

    def _cache_key(self, ctx: EditContext) -> str:
        """Generate a cache key from context."""
        key_parts = f"{ctx.prefix[-200:]}:{ctx.suffix[:100]}:{ctx.language}"
        return hashlib.md5(key_parts.encode()).hexdigest()[:12]

    def _save_prediction(self, ctx: EditContext, prediction: EditPrediction):
        """Cache a prediction."""
        key = self._cache_key(ctx)
        self._cache[key] = prediction
        # Prune cache
        if len(self._cache) > self._max_cache:
            oldest = sorted(self._cache.keys())[:50]
            for k in oldest:
                del self._cache[k]

    def _get_recent_edits(self) -> List[Dict]:
        """Get recent edit history."""
        edits = []
        for ctx in self._history[-20:]:
            if ctx.recent_edits:
                edits.extend(ctx.recent_edits)
        return edits[-30:]


# ── Singleton ────────────────────────────────────────────────────

_speculative_engine: Optional[SpeculativeEditEngine] = None


def get_speculative_engine() -> SpeculativeEditEngine:
    global _speculative_engine
    if _speculative_engine is None:
        _speculative_engine = SpeculativeEditEngine()
    return _speculative_engine
