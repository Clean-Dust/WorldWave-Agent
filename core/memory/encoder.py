"""
ww/core/memory/encoder.py — encode layer

will encode raw experience / dialogue / tool results into memory atoms,
contains : 
- entity extraction (delegated to EntityResolver)
- emotion quantization mapping (text → numeric emotion score)
- importance estimation
- typeclassification (episodic / semantic / procedural) 
"""

from __future__ import annotations
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from .atom import EntityResolver, MemoryAtom

logger = logging.getLogger("ww.memory.encoder")


# ── emotion quantization mapping ──


class EmotionMapper:
    """
    emotion quantization mapping.

    will map text affect signals to a numeric score in [-1.0, 1.0].
      - negative value: negative emotion (error, frustration, warning)
      - positive value: positive emotion (success, confirmation, pleasure)
      - 0.0: neutral

    Lexicon divided into three intensity levels: strong (±1.0), moderate (±0.5), weak (±0.2)
    """

    # ── positive lexicon ──
    POSITIVE_STRONG: Dict[str, float] = {
        "success": 1.0, "succeeded": 1.0, "successfully": 1.0,
        "perfect": 1.0, "excellent": 1.0, "brilliant": 1.0,
        "amazing": 1.0, "wonderful": 1.0, "fantastic": 1.0,
        "great": 0.9, "awesome": 1.0, "breakthrough": 1.0,
        "exceptionally": 0.9, "outstanding": 1.0,
        "breakthrough": 0.9, "impressive": 0.8,
        # Chinese text
        "success": 0.8, "complete": 0.5, "fix": 0.5, "resolve": 0.5,
        "achieve": 0.6, "optimize": 0.6, "breakthrough": 0.9, "perfect": 1.0,
        "excellent": 0.8, "very good": 0.6, "good": 0.5, "correct": 0.3,
        "correct": 0.5, "pass": 0.5, "confirm": 0.4,
    }
    POSITIVE_MODERATE: Dict[str, float] = {
        "good": 0.5, "better": 0.6, "best": 0.7,
        "correct": 0.5, "completed": 0.5, "done": 0.4,
        "fixed": 0.5, "resolved": 0.5, "solved": 0.5,
        "improved": 0.6, "optimized": 0.6, "working": 0.4,
        "progress": 0.5, "achieve": 0.6, "achieved": 0.6,
        "passed": 0.5, "approve": 0.5, "approved": 0.5,
        "thank": 0.5, "thanks": 0.5, "nice": 0.5,
        "clean": 0.4, "efficient": 0.5, "fast": 0.4,
        "happy": 0.6, "glad": 0.5, "love": 0.7,
        "reward": 0.6, "rewarded": 0.6, "benefit": 0.5,
        # Chinese text
        "progress": 0.5, "improve": 0.5, "increase": 0.3, "newly added": 0.4,
        "available": 0.3, "stable": 0.4, "secure": 0.4, "support": 0.3,
    }
    POSITIVE_WEAK: Dict[str, float] = {
        "ok": 0.2, "okay": 0.2, "fine": 0.2,
        "enough": 0.2, "yes": 0.3, "yep": 0.3,
        "sure": 0.2, "alright": 0.2, "acceptable": 0.2,
        # Chinese text
        "can ": 0.2, "ok": 0.2, "still ok": 0.2, "try": 0.1,
    }

    # ── negative lexicon ──
    NEGATIVE_STRONG: Dict[str, float] = {
        "error": -1.0, "error": -1.0, "failed": -1.0, "failure": -1.0,
        "fatal": -1.0, "crash": -1.0, "crashed": -1.0,
        "broken": -1.0, "corrupt": -1.0, "corrupted": -1.0,
        "critical": -0.9, "disaster": -1.0, "terrible": -1.0,
        "deadlock": -1.0, "panic": -1.0, "exception": -0.9,
        "catastrophic": -1.0, "vulnerability": -0.9,
        # Chinese text
        "error": -1.0, "failed": -1.0, "crash": -1.0, "fatal": -1.0,
        "exception": -0.8, "damage": -0.9, "dangerous": -0.8,
    }
    NEGATIVE_MODERATE: Dict[str, float] = {
        "bug": -0.6, "warning": -0.5, "warn": -0.5,
        "problem": -0.5, "issue": -0.4, "trouble": -0.5,
        "wrong": -0.5, "bad": -0.5, "poor": -0.5,
        "slow": -0.4, "lost": -0.5, "missing": -0.4,
        "conflict": -0.5, "conflicting": -0.5, "stuck": -0.5,
        "interrupt": -0.4, "interrupted": -0.4, "abort": -0.6,
        "aborted": -0.6, "cancel": -0.4, "cancelled": -0.4,
        "timeout": -0.5, "rejected": -0.5, "denied": -0.5,
        "invalid": -0.5, "illegal": -0.6, "unexpected": -0.5,
        "regret": -0.5, "sorry": -0.4, "difficult": -0.5,
        "confusing": -0.4, "messy": -0.4,
        # Chinese text
        "problem": -0.4, "warning": -0.5, "timeout": -0.5, "reject": -0.5,
        "invalid": -0.5, "conflict": -0.5, "disconnect": -0.4, "cancel": -0.4,
        "lost": -0.5, "missing": -0.4, "invalid": -0.5, "waste": -0.4,
    }
    NEGATIVE_WEAK: Dict[str, float] = {
        "no": -0.3, "nope": -0.3, "not": -0.2,
        "can't": -0.3, "cannot": -0.3, "won't": -0.3,
        "never": -0.3, "none": -0.2, "nothing": -0.2,
        "maybe": -0.2, "perhaps": -0.2, "try": -0.1,
        "redo": -0.3, "retry": -0.3, "careful": -0.2,
        # Chinese text
        "not": -0.2, "no": -0.2, "don't": -0.3, "don't want": -0.3,
        "not ok": -0.3, "unable": -0.3, "cannot": -0.3, "no need": -0.2,
    }

    # ── negation words (invert emotion) ──
    NEGATORS: Dict[str, bool] = {
        "not": True, "no": True, "never": True,
        "don't": True, "doesn't": True, "didn't": True,
        "can't": True, "couldn't": True, "won't": True,
        "wouldn't": True, "shouldn't": True, "isn't": True,
        "aren't": True, "wasn't": True, "weren't": True,
        "haven't": True, "hasn't": True, "hadn't": True,
        "without": True, "nobody": True, "nothing": True,
    }

    # ── urgency lexicon ──
    URGENCY_KEYWORDS: Dict[str, float] = {
        "urgent": 1.0, "asap": 1.0, "immediately": 1.0,
        "critical": 1.0, "deadline": 0.8, "emergency": 1.0,
        "hotfix": 0.9, "p0": 1.0, "p1": 0.8,
        "blocker": 0.9, "blocking": 0.8,
        "soon": 0.4, "important": 0.5, "priority": 0.6,
    }

    # ── LLM emotion_tag weight mapping ──
    # Main consciousness can use emotion_tag to assist EmotionMapper in determining emotion,
    # solving the blind spot where lexicon method cannot process concessive clauses.
    # weight: multiplicative adjustment factor on the original emotion_score
    TAG_WEIGHTS: Dict[str, float] = {
        "strong_positive": 2.0,
        "positive": 1.5,
        "weak_positive": 1.2,
        "neutral": 1.0,
        "mixed": 0.8,
        "weak_negative": -1.2,
        "negative": -1.5,
        "strong_negative": -2.0,
    }

    def __init__(self):
        # pre-build unified dict and weight
        self._positives: Dict[str, float] = {}
        self._negatives: Dict[str, float] = {}
        for d, w in [(self.POSITIVE_STRONG, 1.0), (self.POSITIVE_MODERATE, 0.6),
                     (self.POSITIVE_WEAK, 0.3)]:
            self._positives.update(d)
        for d, w in [(self.NEGATIVE_STRONG, 1.0), (self.NEGATIVE_MODERATE, 0.6),
                     (self.NEGATIVE_WEAK, 0.3)]:
            self._negatives.update(d)

    def score(self, text: str, urgency: float = 0.0) -> float:
        """
        Calculate text emotion score, range [-1.0, 1.0].

        Args:
            text: type text
            urgency: pre-calculated urgency [0,1]

        Returns:
            emotion score: positive=positive, negative=negative
        """
        if not text or not text.strip():
            return 0.0

        # simultaneously match English words and Chinese text elements
        en_words = re.findall(r"[a-zA-Z']+", text.lower())
        # match text character by character (Chinese dictionary key is single or multiple Chinese characters)
        cn_chars = list(text)
        all_tokens = en_words + cn_chars

        # also check multi-character Chinese words
        text_cn = text
        for word_length in [4, 3, 2]:
            i = 0
            while i <= len(text_cn) - word_length:
                chunk = text_cn[i:i + word_length]
                all_tokens.append(chunk)
                i += 1

        emotion_sum = 0.0
        matched_count = 0
        negate = False

        for token in all_tokens:
            token_lower = token.lower().strip()
            if not token_lower:
                continue

            if token_lower in self.NEGATORS:
                negate = True
                continue

            val = 0.0
            if token_lower in self._positives:
                val = self._positives[token_lower]
            elif token_lower in self._negatives:
                val = self._negatives[token_lower]

            if negate and val != 0:
                val = -val * 0.5
                negate = False

            if val != 0:
                emotion_sum += val
                matched_count += 1

        if matched_count == 0:
            return 0.0

        # average + urgency adjustment
        base = emotion_sum / matched_count
        # urgency: if positive + urgent = stronger positive; if negative + urgent = stronger negative
        if urgency > 0:
            base += (abs(base) * 0.3 * urgency) if base != 0 else 0

        return max(-1.0, min(1.0, base))

    def apply_tag(self, base_score: float, tag: str) -> float:
        """Apply LLM-provided emotion_tag to adjust base emotion score.

        Args:
            base_score: EmotionMapper.score() original result
            tag: tag provided by main consciousness (strong_positive / positive / negative etc.)

        Returns:
            Adjust emotion score [-1.0, 1.0]
        """
        weight = self.TAG_WEIGHTS.get(tag, 1.0)
        if weight >= 0:
            # positive tag: boost (if base is positive then amplify, if base is negative then weaken negative)
            return max(-1.0, min(1.0, base_score * weight))
        else:
            # negative tag: directly replace with negative value (LLM judges as negative, lexicon result no longer matters)
            return max(-1.0, min(1.0, weight * 0.7))


# ── importance estimation ──


def estimate_importance(content: str, source: str = "",
                         emotion_score: float = 0.0) -> float:
    """
    Estimate the importance of a piece of content [0.0, 1.0].

    Factors considered:
    - length (very short text is less important)
    - emotion intensity (strong emotion = important)
    - keywords (error/success/learning etc.)
    - source (tool results are more important than general dialogue)
    """
    score = 0.5  # baseline

    # length factor
    length = len(content.strip())
    if length < 10:
        score -= 0.2
    elif length > 200:
        score += 0.1

    # emotionintensity
    abs_emotion = abs(emotion_score)
    score += abs_emotion * 0.3

    # sourceweight
    source_weights = {
        "tool": 0.15,
        "error": 0.3,
        "system": 0.1,
        "user": 0.05,
        "inference": 0.1,
        "sleep": 0.2,  # sleep produces abstract mode, more important
    }
    score += source_weights.get(source, 0.0)

    return max(0.0, min(1.0, score))


# ── encode layer main class ──


class EncodingLayer:
    """
    Encode layer: raw experience → memory atom.

    Process:
    1. entity extraction (EntityResolver)
    2. emotion quantization (EmotionMapper)
    3. importance estimation
    4. typeclassification
    5. build MemoryAtom
    """

    def __init__(self):
        self.resolver = EntityResolver()
        self.emotion = EmotionMapper()

    def encode(
        self,
        content: str,
        atom_type: str = "",
        source: str = "",
        context_id: str = "",
        tags: Optional[List[str]] = None,
        urgency: float = 0.0,
        emotion_tag: str = "",  # LLM auxiliary emotion tag
    ) -> MemoryAtom:
        """Will encode raw content into a memory atom.

        Args:
            content: raw text content
            atom_type: memory type (auto-detect if left empty)
            source: sourcetag
            context_id: belonging spiral loop ID
            tags: custom tags
            urgency: urgency [0,1]
            emotion_tag: LLM-provided auxiliary emotion tag (strong_positive/positive/neutral/mixed/negative/strong_negative)

        Returns:
            encode   MemoryAtom
        """
        # 1. entity extraction
        raw_entities = self.resolver.extract(content)
        entities = list(set(e["normalized"] for e in raw_entities))

        # 2. emotion quantization (including LLM emotion_tag assistance)
        emotion_score = self.emotion.score(content, urgency=urgency)
        if emotion_tag and emotion_tag in self.emotion.TAG_WEIGHTS:
            emotion_score = self.emotion.apply_tag(emotion_score, emotion_tag)

        # 3. importance
        importance = estimate_importance(content, source, emotion_score)

        # 4. autotype inference
        if not atom_type:
            atom_type = self._infer_type(content, source)

        # 5. Build atom
        atom = MemoryAtom(
            content=content[:500],
            atom_type=atom_type,
            entities=entities,
            emotion=emotion_score,
            importance=importance,
            source=source,
            tags=tags or [],
            context_id=context_id,
        )
        return atom

    def encode_error(self, error_msg: str, context: str = "",
                     context_id: str = "") -> MemoryAtom:
        """Quick encode error experience."""
        content = f"[ERROR] {error_msg}"
        if context:
            content += f" | Context: {context}"
        return self.encode(
            content=content,
            atom_type="episodic",
            source="error",
            context_id=context_id,
            urgency=1.0,
        )

    def encode_success(self, summary: str, context_id: str = "") -> MemoryAtom:
        """Quick encode success experience."""
        return self.encode(
            content=f"[SUCCESS] {summary}",
            atom_type="episodic",
            source="tool",
            context_id=context_id,
        )

    def encode_fact(self, fact: str, entities: List[str],
                    context_id: str = "") -> MemoryAtom:
        """Quick encode semantic fact."""
        return self.encode(
            content=fact,
            atom_type="semantic",
            source="inference",
            tags=entities,
            context_id=context_id,
        )

    def encode_procedure(self, steps: str, context_id: str = "") -> MemoryAtom:
        """Quick encode procedural knowledge."""
        return self.encode(
            content=steps,
            atom_type="procedural",
            source="inference",
            context_id=context_id,
        )

    @staticmethod
    def _infer_type(content: str, source: str) -> str:
        """Infer memory type."""
        if source == "error":
            return "episodic"
        if source == "inference" and len(content) > 100:
            return "semantic"
        keywords = content.lower()
        if any(w in keywords for w in ["how to", "steps", "Process",
                                        "Step", "procedure", "recipe"]):
            return "procedural"
        if any(w in keywords for w in ["learned", "learnt", "learn",
                                        " to ", "Know", "fact", "is a",
                                        "is an", "means"]):
            return "semantic"
        return "episodic"
