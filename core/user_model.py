"""
core/user_model.py — Dynamic User Modeling & Theory-of-Mind

Builds and maintains a dynamic model of the user's personality, preferences,
implicit goals, and interaction style. Unlike entity_state (which stores
explicit facts), this infers latent traits from interaction patterns.

Features:
- Preference inference from observed behavior (not explicit settings)
- Implicit goal detection (user says X but really wants Y)
- Communication style adaptation (verbosity, formality, humor tolerance)
- Trust calibration based on approval/rejection patterns
- Expertise estimation in various domains

All inference is heuristic + statistics — no external ML dependencies.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("ww.user_model")

UM_DIR = os.path.expanduser("~/.ww/user_models")


@dataclass
class CommunicationStyle:
    """Inferred communication preferences."""
    preferred_language: str = "unknown"      # "zh", "en", "zh-en"
    verbosity: float = 0.5                   # 0=minimal, 1=verbose
    formality: float = 0.5                   # 0=casual, 1=formal
    humor_tolerance: float = 0.5             # 0=serious, 1=playful
    directness_preference: float = 0.7       # 0=diplomatic, 1=direct
    code_preferred: bool = False             # Prefers code blocks to prose
    emoji_usage: float = 0.3                 # 0=never, 1=frequent
    samples: int = 0

    def update(self, traits: Dict[str, float]):
        """Exponential moving average update."""
        alpha = 0.3
        for key, val in traits.items():
            if hasattr(self, key) and isinstance(val, (int, float)):
                current = getattr(self, key)
                if not isinstance(current, bool):
                    setattr(self, key, current * (1 - alpha) + val * alpha)
        self.samples += 1

    def to_dict(self) -> dict:
        return {
            "preferred_language": self.preferred_language,
            "verbosity": round(self.verbosity, 3),
            "formality": round(self.formality, 3),
            "humor_tolerance": round(self.humor_tolerance, 3),
            "directness_preference": round(self.directness_preference, 3),
            "code_preferred": self.code_preferred,
            "emoji_usage": round(self.emoji_usage, 3),
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CommunicationStyle":
        return cls(**{k: d.get(k, getattr(cls, k)) for k in [
            "preferred_language", "verbosity", "formality", "humor_tolerance",
            "directness_preference", "code_preferred", "emoji_usage", "samples",
        ]})


@dataclass
class DomainExpertise:
    """Estimated user expertise in different domains."""
    domain: str
    level: float = 0.5          # 0=novice, 1=expert
    confidence: float = 0.0     # How confident we are in this estimate
    observations: int = 0
    last_observed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "level": round(self.level, 3),
            "confidence": round(self.confidence, 3),
            "observations": self.observations,
        }


@dataclass
class ImplicitGoal:
    """A detected but unstated goal."""
    goal_id: str
    description: str               # Inferred goal
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.0
    detected_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    status: str = "active"         # active | achieved | abandoned
    priority: float = 0.5

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "description": self.description,
            "evidence": self.evidence[-5:],
            "confidence": round(self.confidence, 3),
            "detected_at": self.detected_at,
            "status": self.status,
            "priority": round(self.priority, 3),
        }


class UserModel:
    """Dynamic model of a single user entity.

    Learns from every interaction. No external ML — pure heuristics + statistics.
    """

    def __init__(self, entity_id: str):
        self.entity_id = entity_id
        self.style = CommunicationStyle()
        self.expertise: Dict[str, DomainExpertise] = {}
        self.implicit_goals: List[ImplicitGoal] = []
        self._interaction_times: List[float] = []  # For activity pattern detection
        self._approval_history: List[Dict] = []     # Tool approval/rejection patterns
        self._correction_history: List[Dict] = []   # Times user corrected the agent
        self._created_at = time.time()

    # ── Interaction Analysis ──────────────────────────────────

    def observe_message(self, message: str, platform: str = ""):
        """Analyze user message for style signals."""
        if not message:
            return

        traits = {}

        # Language detection
        cjk_chars = sum(1 for c in message if '\u4e00' <= c <= '\u9fff')
        latin_chars = sum(1 for c in message if c.isascii() and c.isalpha())
        if cjk_chars > latin_chars * 2:
            if self.style.preferred_language == "unknown":
                self.style.preferred_language = "zh"
            traits["verbosity"] = 0.3  # Chinese tends to be concise
        elif latin_chars > cjk_chars * 2:
            if self.style.preferred_language == "unknown":
                self.style.preferred_language = "en"

        # Verbosity from message length
        char_count = len(message)
        if char_count < 20:
            traits["verbosity"] = 0.1
            traits["directness_preference"] = 0.9
        elif char_count < 100:
            traits["verbosity"] = 0.4
            traits["directness_preference"] = 0.7
        elif char_count < 500:
            traits["verbosity"] = 0.6
        else:
            traits["verbosity"] = 0.8

        # Formality signals
        formal_markers = ["please", "could you", "would you", "谢谢", "请", "麻烦",
                           "您好", "regards", "sincerely", "尊敬的"]
        casual_markers = ["hey", "yo", "搞", "弄", "ok", "cool", "thx", "np",
                          "lol", "haha", "哈哈", "嘛", "吧"]
        formal_count = sum(1 for m in formal_markers if m in message.lower())
        casual_count = sum(1 for m in casual_markers if m in message.lower())
        if formal_count > casual_count:
            traits["formality"] = 0.7
        elif casual_count > formal_count:
            traits["formality"] = 0.2

        # Emoji detection
        emoji_count = sum(1 for c in message if ord(c) > 0x1F000 or (0x2600 <= ord(c) <= 0x27BF))
        if emoji_count > 0:
            traits["emoji_usage"] = min(1.0, emoji_count / 5)

        # Code preference
        if "```" in message or "`" in message:
            traits["code_preferred"] = 0.8

        self.style.update(traits)
        self._interaction_times.append(time.time())
        if len(self._interaction_times) > 200:
            self._interaction_times = self._interaction_times[-200:]

    def observe_response(self, response_text: str, user_feedback: str = ""):
        """Analyze how the user reacted to our response."""
        if not user_feedback:
            return

        # Detect corrections
        correction_signals = [
            "不对", "不对", "not correct", "wrong", "错误", "不行", "no",
            "不是", "搞错了", "错了", "don't", "should be", "应该是",
            "actually", "其实", "不如", "not what I", "不是我要",
        ]
        positive_signals = [
            "good", "thanks", "谢谢", "好的", "nice", "perfect", "对了",
            "很好", "exactly", "正是", "没错", "没错", "great", "awesome",
        ]

        is_correction = any(s in user_feedback.lower() for s in correction_signals)
        is_positive = any(s in user_feedback.lower() for s in positive_signals)

        self._correction_history.append({
            "response": response_text[:100],
            "feedback": user_feedback[:100],
            "is_correction": is_correction,
            "is_positive": is_positive,
            "timestamp": time.time(),
        })

        # Adjust directness: corrections → user wants more precision
        if is_correction:
            self.style.update({"directness_preference": 0.85})
        if is_positive:
            self.style.update({"humor_tolerance": 0.05})  # Slight bump

        if len(self._correction_history) > 100:
            self._correction_history = self._correction_history[-100:]

    def observe_approval(self, tool_name: str, approved: bool, reason: str = ""):
        """Record tool approval/rejection patterns."""
        self._approval_history.append({
            "tool": tool_name,
            "approved": approved,
            "reason": reason[:200],
            "timestamp": time.time(),
        })
        if len(self._approval_history) > 50:
            self._approval_history = self._approval_history[-50:]

    # ── Domain Expertise ──────────────────────────────────────

    def observe_expertise(self, domain: str, signal: float):
        """Record a signal about user expertise in a domain.

        Args:
            domain: e.g. "python", "devops", "ml"
            signal: 0.0 (novice question) to 1.0 (expert-level discussion)
        """
        if domain not in self.expertise:
            self.expertise[domain] = DomainExpertise(domain=domain)
        exp = self.expertise[domain]
        exp.observations += 1
        alpha = 0.3
        exp.level = exp.level * (1 - alpha) + signal * alpha
        exp.confidence = min(1.0, exp.observations / 10)
        exp.last_observed = time.time()

    def get_expertise(self, domain: str) -> float:
        """Get estimated expertise in a domain."""
        exp = self.expertise.get(domain)
        return exp.level if exp and exp.confidence > 0.3 else 0.5

    # ── Activity Pattern ──────────────────────────────────────

    def get_active_hours(self) -> List[int]:
        """Return hours (0-23) when user is most active."""
        if len(self._interaction_times) < 10:
            return list(range(24))  # All hours

        hour_counts = [0] * 24
        for ts in self._interaction_times[-100:]:
            dt = datetime.fromtimestamp(ts)
            hour_counts[dt.hour] += 1

        # Return hours above threshold
        threshold = max(1, sum(hour_counts) / 24 * 0.5)
        return [h for h, c in enumerate(hour_counts) if c >= threshold]

    def is_likely_available(self) -> bool:
        """Guess if user is likely available right now."""
        active_hours = self.get_active_hours()
        current_hour = datetime.now().hour
        return current_hour in active_hours or len(active_hours) > 18

    # ── Implicit Goals ────────────────────────────────────────

    def detect_implicit_goal(self, message: str, context: str = "") -> Optional[str]:
        """Try to detect an unstated goal from user's message.

        Returns goal_id if a new implicit goal was created.
        """
        msg_lower = message.lower()

        # Pattern: "I wish...", "it would be nice if...", "someday..."
        wish_patterns = [
            (["i wish", "i want to", "i'd like to", "如果可以", "要是能",
              "真想", "希望"], 0.3),
            (["it would be nice", "wouldn't it be", "maybe we should",
              "也许应该", "不如", "要是"], 0.4),
            (["eventually", "long term", "长期", "最终目标", "长远"], 0.5),
            (["actually", "其实我想", "其实我要"], 0.6),
        ]

        for patterns, confidence in wish_patterns:
            if any(p in msg_lower for p in patterns):
                import uuid
                goal = ImplicitGoal(
                    goal_id=uuid.uuid4().hex[:8],
                    description=f"Inferred from: {message[:100]}",
                    evidence=[message[:200]],
                    confidence=confidence,
                    priority=0.3 + confidence * 0.3,
                )
                self.implicit_goals.append(goal)
                self._prune_goals()
                log.info(f"🎯 Detected implicit goal: {goal.description[:80]}")
                return goal.goal_id

        return None

    def update_goal_progress(self, goal_id: str, progress_signal: float):
        """Update an implicit goal's status based on task progress."""
        for g in self.implicit_goals:
            if g.goal_id == goal_id:
                g.last_updated = time.time()
                g.confidence += progress_signal * 0.1
                if progress_signal > 0.8:
                    g.status = "achieved"
                elif progress_signal < -0.3:
                    g.priority -= 0.1
                break

    def get_active_goals(self) -> List[ImplicitGoal]:
        """Return active implicit goals sorted by priority."""
        return sorted(
            [g for g in self.implicit_goals if g.status == "active"],
            key=lambda g: g.priority * g.confidence, reverse=True
        )

    def _prune_goals(self):
        """Remove stale/achieved goals."""
        cutoff = time.time() - 30 * 86400  # 30 days
        self.implicit_goals = [
            g for g in self.implicit_goals
            if g.status == "active" or g.last_updated > cutoff
        ][-20:]  # Cap at 20

    # ── Context Injection ─────────────────────────────────────

    def get_context_injection(self) -> str:
        """Build a user-model context block for the LLM prompt."""
        parts = []

        # Communication style
        if self.style.samples > 3:
            style_desc = []
            if self.style.preferred_language == "zh":
                style_desc.append("prefers Chinese")
            elif self.style.preferred_language == "en":
                style_desc.append("prefers English")
            if self.style.verbosity < 0.3:
                style_desc.append("very concise, no fluff")
            elif self.style.verbosity > 0.7:
                style_desc.append("appreciates detailed explanations")
            if self.style.directness_preference > 0.7:
                style_desc.append("direct and to-the-point")
            if self.style.code_preferred:
                style_desc.append("prefers code examples")
            if style_desc:
                parts.append("Communication style: " + ", ".join(style_desc))

        # Expertise
        confident_expertise = [
            (d, e) for d, e in self.expertise.items()
            if e.confidence > 0.4
        ]
        if confident_expertise:
            exp_lines = []
            for domain, exp in sorted(confident_expertise, key=lambda x: x[1].level, reverse=True)[:5]:
                label = "expert" if exp.level > 0.7 else "familiar" if exp.level > 0.4 else "learning"
                exp_lines.append(f"- {domain}: {label}")
            parts.append("Estimated expertise:\n" + "\n".join(exp_lines))

        # Implicit goals
        active_goals = self.get_active_goals()[:3]
        if active_goals:
            goal_lines = [f"- [{g.confidence:.0%}] {g.description[:100]}" for g in active_goals]
            parts.append("Detected implicit goals:\n" + "\n".join(goal_lines))

        # Activity pattern
        active_hours = self.get_active_hours()
        if len(active_hours) < 20:  # Has a pattern
            parts.append(f"Most active hours: {active_hours[:6]}")

        # Correction rate
        if len(self._correction_history) > 5:
            corrections = sum(1 for c in self._correction_history[-20:] if c["is_correction"])
            if corrections > 3:
                parts.append(f"⚠️ High correction rate ({corrections}/20 recent) — be extra accurate")

        # Trust calibration
        approved = sum(1 for a in self._approval_history[-20:] if a["approved"])
        total = len(self._approval_history[-20:])
        if total > 5:
            trust = approved / total if total > 0 else 0.5
            if trust < 0.5:
                parts.append(f"⚠️ Low trust ({trust:.0%} approval rate) — request permission more often")

        return "\n".join(parts) if parts else ""

    # ── Persistence ───────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "style": self.style.to_dict(),
            "expertise": {k: v.to_dict() for k, v in self.expertise.items()},
            "implicit_goals": [g.to_dict() for g in self.implicit_goals],
            "correction_count": len(self._correction_history),
            "approval_count": len(self._approval_history),
            "interaction_count": len(self._interaction_times),
            "created_at": self._created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserModel":
        model = cls(entity_id=d["entity_id"])
        model.style = CommunicationStyle.from_dict(d.get("style", {}))
        model.expertise = {
            k: DomainExpertise(**v) for k, v in d.get("expertise", {}).items()
        }
        model.implicit_goals = [
            ImplicitGoal(**g) for g in d.get("implicit_goals", [])
        ]
        model._created_at = d.get("created_at", time.time())
        return model


class UserModelManager:
    """Manages UserModel instances — create, load, persist, unload.

    Thread-safe. Each entity gets its own UserModel stored at ~/.ww/user_models/.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._models: Dict[str, UserModel] = {}
        os.makedirs(UM_DIR, exist_ok=True)

    def get(self, entity_id: str) -> UserModel:
        """Get or create a user model."""
        if not self.enabled:
            return UserModel(entity_id=entity_id)
        if entity_id in self._models:
            return self._models[entity_id]
        model = self._load(entity_id)
        if model is None:
            model = UserModel(entity_id=entity_id)
        self._models[entity_id] = model
        return model

    def save(self, entity_id: str):
        """Persist user model to disk."""
        if not self.enabled:
            return
        model = self._models.get(entity_id)
        if not model:
            return
        path = os.path.join(UM_DIR, f"{entity_id}.json")
        with open(path, "w") as f:
            json.dump(model.to_dict(), f, indent=2, ensure_ascii=False)

    def save_all(self):
        """Persist all loaded models."""
        if not self.enabled:
            return
        for eid in list(self._models.keys()):
            self.save(eid)

    def _load(self, entity_id: str) -> Optional[UserModel]:
        path = os.path.join(UM_DIR, f"{entity_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return UserModel.from_dict(data)
        except Exception as e:
            log.warning(f"User model load failed for {entity_id}: {e}")
            return None

    def stats(self) -> Dict:
        return {
            "models_loaded": len(self._models),
            "entities_with_models": len([
                f for f in os.listdir(UM_DIR) if f.endswith(".json")
            ]) if os.path.exists(UM_DIR) else 0,
        }
