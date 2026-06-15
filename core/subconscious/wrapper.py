"""
ww/core/subconscious/wrapper.py — Translation Layer (Subconscious Wrapper)

The subconscious model only outputs mathematical signals; this Wrapper is responsible for translating the signals into
system instructions, API parameters, or execute interception that the main consciousness can understand.

3 types of intervention methods:
  1. Rule ID Trigger → extract System Prompt snippet from Rule Dictionary
  2. Parameter Tune → directly modify LLM API parameters
  3. Action Code → intercept execute flow (lint, memory recall, etc.)

Throughout the process, the subconscious does not output any text, only outputs IDs and floating-point numbers.
This also naturally defends against Prompt Injection: malicious users cannot hypnotize the subconscious with natural language.
"""

from __future__ import annotations
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .rule_dict import RuleDictionary
from .predictor import TriageVector  # noqa: F401 — legacy alias, actual class is DeepRiskNet
from .features import FeatureExtractor, PADDED_FEATURES, pad_vector, FEATURE_NAMES

logger = logging.getLogger("ww.subconscious.wrapper")

# ── Dynamic hyperparameter presets ──
# Each preset: (temperature, top_p, top_k, description)
# Used when subconscious does not have a trained net for params yet.
PARAM_PRESETS = {
    "precise":     (0.1, 0.9, 40,   "Low temp, conservative — for math, code generation"),
    "balanced":    (0.5, 0.9, 50,   "Default — general purpose"),
    "exploratory": (0.8, 0.95, 60,  "Higher temp — for creative, brainstorming"),
    "diverse":     (1.0, 0.98, 100, "High diversity — for divergent thinking"),
    "strict":      (0.05, 0.8, 20,  "Ultra deterministic — for critical calculations"),
}


@dataclass
class Intervention:
    """
    One translation layer intervention complete record.

    Attributes:
        timestamp: intervention  
        trigger_rule_id: triggered rule ID
        failure_risk: subconscious model failure risk output
        rule_type: ruletype (system_prompt / param_tune / action_code)
        applied_content: actually applied content (text, dict, or string code)
        applied: whether successfully applied
        duration_s: duration 
    """
    timestamp: float = field(default_factory=time.time)
    trigger_rule_id: int = 0
    failure_risk: float = 0.0
    rule_type: str = ""
    applied_content: Any = None
    applied: bool = False
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        content = self.applied_content
        if isinstance(content, str) and len(content) > 200:
            content = content[:200] + "..."
        return {
            "timestamp": self.timestamp,
            "trigger_rule_id": self.trigger_rule_id,
            "failure_risk": round(self.failure_risk, 4),
            "rule_type": self.rule_type,
            "applied": self.applied,
            "duration_s": round(self.duration_s, 3),
        }


# ── Signal → rule matching ──


class SignalMatcher:
    """
    will map subconscious mathematical signals to Rule ID.

    strategy：
    1. failure_risk > high_threshold → high risk, query rule
    2. based on specific dimension value of feature vector, select the best matching rule
    3. no match → id=0 (noop)
    """

    # mapping relationship between feature vector dimension and rule
    # (feature_idx, min_val, max_val, rule_id)
    FEATURE_RULES = [
        # dimension 0: tool_error_rate → high error rate → network/crawler rule
        (0, 0.6, 1.0, 1),    # network anti-block
        (0, 0.8, 1.0, 4),    # extremely high error rate → lint first
        # dimension 1: loop_detected → loop → clean output
        (1, 0.5, 1.0, 10),   # output repetition
        # dimension 2: empty_response_rate → empty response → precise mode
        (2, 0.3, 1.0, 2),    # reduce hallucinations
        # dimension 5: context_utilization → high utilization → compress
        (5, 0.8, 1.0, 5),    # compresscontext
        # dimension 6: task_complexity → complex task → precise mode
        (6, 0.7, 1.0, 2),    # reduce hallucinations
        # dimension 7: api_latency → high latency → frequency reduction
        (7, 0.6, 1.0, 8),    # API frequency reduction
        # dimension 8: memory_pressure → memory pressure → secure mode
        (8, 0.7, 1.0, 7),    # memorysecure
        # dimension 12: api_provider_openrouter → OpenRouter → network anti-block
        (12, 0.5, 1.0, 1),   # network anti-block
    ]

    def __init__(self, rule_dict: RuleDictionary):
        self.rule_dict = rule_dict
        self._high_threshold = 0.6   # high risk trigger threshold
        self._medium_threshold = 0.4  # medium risk trigger threshold

    def match(
        self,
        features: List[float],
        failure_risk: float,
    ) -> List[int]:
        """
        match rules based on feature vector and failure risk.

        Args:
            features: 32-dimensional feature vector
            failure_risk: model output (0.0-1.0)

        Returns:
            Rule ID list (sorted by priority)
        """
        matched = set()

        # 1. high risk → match feature rule
        if failure_risk >= self._high_threshold:
            for feat_idx, min_val, max_val, rule_id in self.FEATURE_RULES:
                if feat_idx < len(features):
                    val = features[feat_idx]
                    if min_val <= val <= max_val:
                        matched.add(rule_id)

        # 2. medium risk + extreme feature value
        if failure_risk >= self._medium_threshold:
            for feat_idx, min_val, max_val, rule_id in self.FEATURE_RULES:
                if feat_idx < len(features):
                    val = features[feat_idx]
                    if min_val <= val <= max_val:
                        matched.add(rule_id)

        # 3. if no match but risk is high, use universal rule
        if not matched and failure_risk >= self._high_threshold:
            matched.add(2)  # precise mode (secure default)

        # sort: descending by risk level + ascending by rule ID
        return sorted(matched)

    def best_rule(self, features: List[float], failure_risk: float) -> int:
        """
        return the best single rule ID.

        when multiple rules match, select the one with the highest priority.
        """
        matched = self.match(features, failure_risk)
        if not matched:
            return 0  # noop
        # Priority: system_prompt > action_code > param_tune
        priority = {"system_prompt": 0, "action_code": 1, "param_tune": 2}
        best = matched[0]
        best_prio = 3
        for rid in matched:
            rule = self.rule_dict.get(rid)
            if rule:
                p = priority.get(rule.get("type", ""), 3)
                if p < best_prio:
                    best_prio = p
                    best = rid
        return best


# ── Translation Layer ──


class SubconsciousWrapper:
    """
    subconscious translation layer.

    Connect subconscious model (RandomForest) and main consciousness (LLM call/toolexecute).
    Each spiral loop call, at LLM request intervention.

    usage：
        wrapper = SubconsciousWrapper(predictor, rule_dict)
        intervention = wrapper.evaluate(features)
        if intervention.rule_type == "system_prompt":
            # will content inject into system prompt
        elif intervention.rule_type == "param_tune":
            # setting LLM API parameters
        elif intervention.rule_type == "action_code":
            # execute interception action
    """

    def __init__(
        self,
        predictor: Optional["DeepRiskNet"] = None,
        rule_dict: Optional[RuleDictionary] = None,
        feature_extractor: Optional[FeatureExtractor] = None,
        high_threshold: float = 0.6,
        medium_threshold: float = 0.4,
    ):
        self.predictor = predictor
        self.rule_dict = rule_dict or RuleDictionary()
        self.feature_extractor = feature_extractor
        self.matcher = SignalMatcher(self.rule_dict)
        self.high_threshold = high_threshold
        self.medium_threshold = medium_threshold

        # Intervention history (last 50 entries)
        self._history: List[Intervention] = []

        # when active system prompt fragments (multiple rules can be stacked)
        self._active_prompts: List[str] = []
        # when active parameters adjustment
        self._active_params: Dict[str, Any] = {}

        # callback (main consciousness register)
        self._prompt_inject_fn: Optional[Callable[[str], None]] = None
        self._param_tune_fn: Optional[Callable[[dict], None]] = None
        self._action_dispatch_fn: Optional[Callable[[str], None]] = None

    def set_callbacks(
        self,
        on_prompt_inject: Optional[Callable[[str], None]] = None,
        on_param_tune: Optional[Callable[[dict], None]] = None,
        on_action_dispatch: Optional[Callable[[str], None]] = None,
    ):
        """registercallback, let main consciousness know when to intervene."""
        self._prompt_inject_fn = on_prompt_inject
        self._param_tune_fn = on_param_tune
        self._action_dispatch_fn = on_action_dispatch

    # ── core evaluate method ──

    def evaluate(
        self,
        features: List[float],
        force: bool = False,
    ) -> Intervention:
        """
        evaluate when state and decide whether to intervene.

        Args:
            features: 32-dimensional state vector
            force: force evaluate (skip threshold check)

        Returns:
            Intervention record
        """
        start = time.time()

        # 1. Model prediction — now returns TriageVector (4 signals)
        triage = TriageVector()
        if self.predictor:
            triage = self.predictor.predict(features)
        failure_risk = triage.crash_risk

        # 2. Match rule
        params_recommendation = None
        if failure_risk < self.medium_threshold and not force:
            # Low risk → noop
            intervention = Intervention(
                trigger_rule_id=0,
                failure_risk=failure_risk,
                rule_type="noop",
                applied=True,
                duration_s=time.time() - start,
            )
            self._history.append(intervention)
            return intervention

        rule_id = self.matcher.best_rule(features, failure_risk)
        rule = self.rule_dict.get(rule_id) if rule_id > 0 else None

        if rule_id == 0 or rule is None:
            intervention = Intervention(
                trigger_rule_id=0,
                failure_risk=failure_risk,
                rule_type="noop",
                applied=True,
                duration_s=time.time() - start,
            )
            self._history.append(intervention)
            return intervention

        # 3. Apply rule
        rule_type = rule.get("type", "")
        content = rule.get("content")

        applied = False
        if rule_type == "system_prompt" and isinstance(content, str):
            applied = self._apply_system_prompt(content)
        elif rule_type == "param_tune" and isinstance(content, dict):
            applied = self._apply_param_tune(content)
        elif rule_type == "action_code" and isinstance(content, str):
            applied = self._apply_action_code(content)

        intervention = Intervention(
            trigger_rule_id=rule_id,
            failure_risk=failure_risk,
            rule_type=rule_type,
            applied_content=content,
            applied=applied,
            duration_s=time.time() - start,
        )

        self._history.append(intervention)
        if len(self._history) > 50:
            self._history.pop(0)

        if applied:
            logger.info(
                f"🧠 Subconscious intervention: rule#{rule_id} "
                f"({rule_type}) risk={failure_risk:.3f}"
            )

        # ── Always compute dynamic parameter recommendations ──
        params_recommendation = self.recommend_params(features, failure_risk)
        if self._param_tune_fn and params_recommendation:
            try:
                self._param_tune_fn(params_recommendation)
            except Exception:
                logger.exception("dynamic param_tune failed")

        return intervention

    # ── Dynamic hyperparameter recommendation ──

    def recommend_params(
        self,
        features: List[float],
        failure_risk: float,
    ) -> Dict[str, Any]:
        """
        Compute recommended LLM hyperparameters based on current state.

        Uses heuristics derived from the feature vector to dynamically
        adjust temperature, top_p, and top_k.  This can be replaced with
        a learned multi-output head once the model supports it.

        Args:
            features: 32-dimensional feature vector
            failure_risk: current failure risk (0.0-1.0)

        Returns:
            dict with 'temperature', 'top_p', 'top_k', 'preset_name'
        """
        if not features:
            return {"temperature": 0.5, "top_p": 0.9, "top_k": 50, "preset_name": "balanced"}

        # Normalise indices that matter
        consecutive_errors = features[0] if len(features) > 0 else 0.0
        tool_loop = features[1] if len(features) > 1 else 0.0
        latency = features[2] if len(features) > 2 else 0.0
        token_rate = features[4] if len(features) > 4 else 0.0
        last_ok = features[7] if len(features) > 7 else 1.0
        empty_resp = features[10] if len(features) > 10 else 0.0
        cpu_load = features[15] if len(features) > 15 else 0.0
        mem_free = features[16] if len(features) > 16 else 0.8
        ctx_pressure = features[17] if len(features) > 17 else 0.0

        # ── Decide preset ──
        # Risk-based: high risk → go conservative
        if failure_risk > 0.7:
            base = "strict" if consecutive_errors > 3 else "precise"
        elif failure_risk > 0.4:
            # Medium risk — check other signals
            if empty_resp > 0.5 or last_ok < 0.5:
                base = "precise"
            elif tool_loop > 3:
                # Stuck in a loop — try exploring to break out
                base = "exploratory"
            else:
                base = "balanced"
        else:
            # Low risk — normal operation
            if consecutive_errors == 0 and last_ok > 0.5:
                base = "exploratory" if token_rate < 0.3 else "balanced"
            else:
                base = "balanced"

        # ── Continuous adjustments ──
        temp, top_p, top_k, _ = PARAM_PRESETS[base]

        # System resource pressure → more conservative
        if cpu_load > 0.8 or mem_free < 0.2:
            temp = max(0.05, temp - 0.15)
            top_p = max(0.5, top_p - 0.1)

        # Context pressure → more precise (save tokens)
        if ctx_pressure > 0.7:
            temp = max(0.05, temp - 0.1)
            top_k = max(10, top_k - 10)

        # High latency → conserve budget, be deterministic
        if latency > 10.0:
            temp = max(0.05, temp - 0.1)
            top_p = max(0.5, top_p - 0.05)

        return {
            "temperature": round(temp, 3),
            "top_p": round(top_p, 3),
            "top_k": int(top_k),
            "preset_name": base,
        }

    # ── internal application method ──

    def _apply_system_prompt(self, prompt_fragment: str) -> bool:
        """will inject system prompt fragments into main consciousness."""
        if self._prompt_inject_fn:
            self._active_prompts.append(prompt_fragment)
            try:
                self._prompt_inject_fn(prompt_fragment)
                return True
            except Exception:
                logger.exception("prompt_inject_fn failed")
                return False
        return False

    def _apply_param_tune(self, params: dict) -> bool:
        """Modify LLM API parameters."""
        if self._param_tune_fn:
            self._active_params.update(params)
            try:
                self._param_tune_fn(self._active_params)
                return True
            except Exception:
                logger.exception("param_tune_fn failed")
                return False
        return False

    def _apply_action_code(self, action: str) -> bool:
        """Dispatch action code to main consciousness execute flow."""
        if self._action_dispatch_fn:
            try:
                self._action_dispatch_fn(action)
                return True
            except Exception:
                logger.exception("action_dispatch_fn failed")
                return False
        return False

    # ── statemanagement ──

    def clear_active_prompts(self):
        """Clear applied system prompt fragments."""
        self._active_prompts.clear()

    def clear_active_params(self):
        """Reset parameters adjustment."""
        self._active_params.clear()

    def active_prompts(self) -> List[str]:
        return list(self._active_prompts)

    def active_params(self) -> dict:
        return dict(self._active_params)

    def recent_interventions(self, n: int = 10) -> List[Intervention]:
        return self._history[-n:]

    def stats(self) -> dict:
        total = len(self._history)
        by_type = {}
        for h in self._history:
            by_type[h.rule_type] = by_type.get(h.rule_type, 0) + 1
        return {
            "total_interventions": total,
            "by_type": by_type,
            "active_prompts": len(self._active_prompts),
            "active_params": len(self._active_params),
        }
