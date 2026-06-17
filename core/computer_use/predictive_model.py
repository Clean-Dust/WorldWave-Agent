"""
ww/core/computer_use/predictive_model.py — Cerebellar Internal Predictive Model v0.1

Biomimetic internal model for action outcome prediction.

The cerebellum maintains an "internal model" that predicts the expected
result of any action BEFORE execution. When the prediction mismatches
reality, the cerebellum triggers error correction.

This extends beyond visual pixel diff (Tier 4) to non-visual domains:
- Shell command output prediction
- File operation outcome prediction
- API call response prediction
- System state transition prediction

Architecture:
    Action → Forward Model → Predicted State
    Action → Real Execution → Actual State
    Delta = Predicted - Actual
    If |delta| > threshold → trigger correction

Pure Python, zero external dependencies.
"""

from __future__ import annotations
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ── Prediction domains ──

@dataclass
class PredictedOutcome:
    """A prediction of what should happen when an action is executed."""
    domain: str                         # "shell", "file", "api", "system"
    action_summary: str                 # Short description of the action
    expected_success: bool              # Should it succeed?
    expected_output_pattern: str        # Regex or substring expected in output
    expected_exit_code: int             # Expected exit code (shell commands)
    expected_state_change: Dict[str, Any]  # Expected state mutations
    confidence: float                   # How confident is the prediction [0, 1]
    timestamp: float = field(default_factory=time.time)


@dataclass
class OutcomeDelta:
    """Difference between prediction and reality."""
    prediction: PredictedOutcome
    actual_success: bool
    actual_output: str
    actual_exit_code: int
    mismatch_type: str           # "success", "output", "exit_code", "state"
    delta_magnitude: float       # How far off [0, 1]
    correctable: bool            # Can this be auto-corrected?
    correction_action: str       # Suggested correction


class PredictiveModel:
    """Cerebellar internal predictive model.

    Learns from past action→outcome pairs to predict future outcomes.
    When predictions mismatch, triggers corrective actions automatically.
    """

    def __init__(
        self,
        prediction_threshold: float = 0.3,   # Delta above this → trigger correction
        history_size: int = 500,              # Max stored predictions
        model_path: str = "",
    ):
        self.threshold = prediction_threshold
        self.history_size = history_size

        # Pattern memory: learned action→outcome patterns
        # Key: (domain, action_pattern) → Value: expected outcome stats
        self._patterns: Dict[str, Dict[str, Any]] = {}

        # Recent predictions for learning
        self._history: deque = deque(maxlen=history_size)

        # Stats
        self._total_predictions = 0
        self._correct_predictions = 0
        self._corrections_applied = 0

        if model_path:
            self._load(model_path)

    # ── Prediction ──

    def predict(
        self,
        domain: str,
        action: str,
        params: Dict[str, Any] = None,
        context: Dict[str, Any] = None,
    ) -> PredictedOutcome:
        """Predict the outcome of an action before execution.

        Args:
            domain: "shell", "file", "api", "system"
            action: Tool/command name or description
            params: Action parameters
            context: Current system context

        Returns:
            PredictedOutcome with expected results
        """
        self._total_predictions += 1

        # Build pattern key
        pattern_key = self._build_key(domain, action, params)

        # Check learned patterns
        if pattern_key in self._patterns:
            p = self._patterns[pattern_key]
            # Use learned pattern with high confidence
            return PredictedOutcome(
                domain=domain,
                action_summary=f"{domain}:{action}",
                expected_success=p.get("success_rate", 0.5) > 0.6,
                expected_output_pattern=p.get("common_output_pattern", ""),
                expected_exit_code=p.get("common_exit_code", 0),
                expected_state_change=p.get("state_changes", {}),
                confidence=p.get("confidence", 0.3),
            )

        # Heuristic prediction based on domain + action type
        return self._heuristic_predict(domain, action, params)

    def _heuristic_predict(
        self,
        domain: str,
        action: str,
        params: Dict[str, Any] = None,
    ) -> PredictedOutcome:
        """Heuristic prediction when no learned pattern exists."""
        action_lower = action.lower()

        # Shell commands
        if domain == "shell":
            if any(w in action_lower for w in ["ls", "cat", "echo", "pwd", "whoami", "date"]):
                return PredictedOutcome(
                    domain=domain,
                    action_summary=f"shell:{action}",
                    expected_success=True,
                    expected_output_pattern="",
                    expected_exit_code=0,
                    expected_state_change={},
                    confidence=0.9,
                )
            if any(w in action_lower for w in ["rm", "delete", "drop", "truncate"]):
                return PredictedOutcome(
                    domain=domain,
                    action_summary=f"shell:{action}",
                    expected_success=True,  # Usually succeeds
                    expected_output_pattern="",
                    expected_exit_code=0,
                    expected_state_change={"files_deleted": True},
                    confidence=0.7,
                )
            if any(w in action_lower for w in ["git push", "git commit"]):
                return PredictedOutcome(
                    domain=domain,
                    action_summary=f"shell:{action}",
                    expected_success=True,
                    expected_output_pattern="",
                    expected_exit_code=0,
                    expected_state_change={"git_state_changed": True},
                    confidence=0.6,
                )
            # Default shell
            return PredictedOutcome(
                domain=domain,
                action_summary=f"shell:{action}",
                expected_success=True,
                expected_output_pattern="",
                expected_exit_code=0,
                expected_state_change={},
                confidence=0.4,
            )

        # File operations
        if domain == "file":
            if "read" in action_lower:
                return PredictedOutcome(
                    domain=domain,
                    action_summary=f"file:{action}",
                    expected_success=True,
                    expected_output_pattern="",
                    expected_exit_code=0,
                    expected_state_change={},
                    confidence=0.85,
                )
            if "write" in action_lower or "patch" in action_lower:
                return PredictedOutcome(
                    domain=domain,
                    action_summary=f"file:{action}",
                    expected_success=True,
                    expected_output_pattern="",
                    expected_exit_code=0,
                    expected_state_change={"file_modified": True},
                    confidence=0.75,
                )
            return PredictedOutcome(
                domain=domain,
                action_summary=f"file:{action}",
                expected_success=True,
                expected_output_pattern="",
                expected_exit_code=0,
                expected_state_change={},
                confidence=0.5,
            )

        # API calls
        if domain == "api":
            get_params = params or {}
            method = str(get_params.get("method", "GET")).upper()
            if method == "GET":
                return PredictedOutcome(
                    domain=domain,
                    action_summary=f"api:{action}",
                    expected_success=True,
                    expected_output_pattern="",
                    expected_exit_code=200,
                    expected_state_change={},
                    confidence=0.7,
                )
            return PredictedOutcome(
                domain=domain,
                action_summary=f"api:{action}",
                expected_success=True,
                expected_output_pattern="",
                expected_exit_code=200,
                expected_state_change={},
                confidence=0.5,
            )

        # Default
        return PredictedOutcome(
            domain=domain,
            action_summary=f"{domain}:{action}",
            expected_success=True,
            expected_output_pattern="",
            expected_exit_code=0,
            expected_state_change={},
            confidence=0.3,
        )

    # ── Verification ──

    def verify(
        self,
        prediction: PredictedOutcome,
        actual_success: bool,
        actual_output: str = "",
        actual_exit_code: int = 0,
        actual_state: Dict[str, Any] = None,
    ) -> Optional[OutcomeDelta]:
        """Verify prediction against actual outcome. Returns delta if mismatch."""

        # Check success match
        if prediction.expected_success != actual_success:
            if not actual_success:
                # Action failed when expected to succeed
                self._history.append({
                    "type": "mismatch",
                    "prediction": prediction,
                    "actual": {"success": False, "output": actual_output[:200]},
                })
                return OutcomeDelta(
                    prediction=prediction,
                    actual_success=actual_success,
                    actual_output=actual_output,
                    actual_exit_code=actual_exit_code,
                    mismatch_type="success",
                    delta_magnitude=0.8,
                    correctable=self._is_correctable(prediction, actual_output),
                    correction_action=self._suggest_correction(prediction, actual_output),
                )

        # Check exit code match
        if actual_exit_code != 0 and prediction.expected_exit_code == 0:
            return OutcomeDelta(
                prediction=prediction,
                actual_success=actual_success,
                actual_output=actual_output,
                actual_exit_code=actual_exit_code,
                mismatch_type="exit_code",
                delta_magnitude=0.5,
                correctable=self._is_correctable(prediction, actual_output),
                correction_action=self._suggest_correction(prediction, actual_output),
            )

        # Successful prediction
        self._correct_predictions += 1
        self._history.append({
            "type": "match",
            "prediction": prediction,
            "actual": {"success": actual_success, "output": actual_output[:200]},
        })
        self._learn_from_success(prediction, actual_output)
        return None

    def _is_correctable(self, prediction: PredictedOutcome,
                        actual_output: str) -> bool:
        """Determine if the mismatch is auto-correctable."""
        output_lower = actual_output.lower()

        # Correctable errors
        if any(w in output_lower for w in [
            "syntax error", "typo", "not found", "cannot find",
            "no such file", "permission denied",
        ]):
            return True

        # Non-correctable errors
        if any(w in output_lower for w in [
            "fatal", "crash", "kernel panic", "out of memory", "killed",
        ]):
            return False

        return False

    def _suggest_correction(self, prediction: PredictedOutcome,
                            actual_output: str) -> str:
        """Suggest a corrective action based on error output."""
        output_lower = actual_output.lower()

        if "syntax error" in output_lower or "typo" in output_lower:
            return "fix_syntax"
        if "not found" in output_lower or "no such file" in output_lower:
            return "check_path"
        if "permission denied" in output_lower:
            return "check_permissions"
        if "connection" in output_lower or "timeout" in output_lower:
            return "retry_with_backoff"
        return "unknown"

    # ── Learning ──

    def _learn_from_success(self, prediction: PredictedOutcome,
                            actual_output: str):
        """Update pattern memory based on successful prediction."""
        key = self._build_key(
            prediction.domain,
            prediction.action_summary,
            {},
        )

        if key not in self._patterns:
            self._patterns[key] = {
                "success_count": 0,
                "total_count": 0,
                "success_rate": 0.0,
                "common_output_pattern": "",
                "common_exit_code": 0,
                "state_changes": {},
                "confidence": 0.0,
            }

        p = self._patterns[key]
        p["total_count"] += 1
        p["success_count"] += 1
        p["success_rate"] = p["success_count"] / p["total_count"]
        p["confidence"] = min(0.95, p["confidence"] + 0.05)

        # Extract common output patterns
        if actual_output and len(actual_output) < 500:
            p["common_output_pattern"] = self._extract_pattern(actual_output)

    def learn_from_failure(self, domain: str, action: str,
                           error_output: str):
        """Learn from failed prediction — decrease confidence."""
        key = self._build_key(domain, action, {})
        if key in self._patterns:
            p = self._patterns[key]
            p["total_count"] += 1
            p["success_rate"] = p["success_count"] / p["total_count"]
            p["confidence"] = max(0.05, p["confidence"] - 0.1)

    def _extract_pattern(self, output: str) -> str:
        """Extract a simplified pattern from output text."""
        # Remove numbers, keep structure
        pattern = re.sub(r'\d+', '{N}', output)
        # Remove UUIDs
        pattern = re.sub(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            '{UUID}', pattern, flags=re.IGNORECASE,
        )
        return pattern[:200]

    # ── Utility ──

    def _build_key(self, domain: str, action: str,
                   params: Dict[str, Any] = None) -> str:
        """Build a pattern key from domain + action."""
        # Normalize action
        action_clean = action.lower().strip()
        # Remove specific parameters (keep the tool name only)
        action_clean = re.sub(r'\s+', '_', action_clean)[:80]
        return f"{domain}:{action_clean}"

    # ── Serialization ──

    def to_dict(self) -> Dict:
        return {
            "patterns": self._patterns,
            "history_size": self.history_size,
            "total_predictions": self._total_predictions,
            "correct_predictions": self._correct_predictions,
            "corrections_applied": self._corrections_applied,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "PredictiveModel":
        model = cls(
            history_size=d.get("history_size", 500),
        )
        model._patterns = d.get("patterns", {})
        model._total_predictions = d.get("total_predictions", 0)
        model._correct_predictions = d.get("correct_predictions", 0)
        model._corrections_applied = d.get("corrections_applied", 0)
        return model

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def _load(self, path: str):
        try:
            with open(path) as f:
                d = json.load(f)
            loaded = PredictiveModel.from_dict(d)
            self._patterns = loaded._patterns
            self._total_predictions = loaded._total_predictions
            self._correct_predictions = loaded._correct_predictions
            self._corrections_applied = loaded._corrections_applied
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # ── Stats ──

    def stats(self) -> Dict:
        accuracy = (
            self._correct_predictions / max(1, self._total_predictions)
        )
        return {
            "patterns_learned": len(self._patterns),
            "total_predictions": self._total_predictions,
            "correct_predictions": self._correct_predictions,
            "accuracy": round(accuracy, 3),
            "corrections_applied": self._corrections_applied,
            "history_size": len(self._history),
        }


# ── Factory ──

def create_predictive_model(**kwargs) -> PredictiveModel:
    return PredictiveModel(**kwargs)
