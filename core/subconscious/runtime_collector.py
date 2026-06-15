"""
ww/core/subconscious/runtime_collector.py — Runtime Collector

Hooks into WW spiral loop, automatically collects training signals from four dimensions.

Hook points (called at spiral loop 'evaluate' phase):
  1. after_action — each tool call → environment feedback
  2. on_user_interrupt — user intervention triggered → user intervention signal
  3. after_task — task completion → efficiency + reflection trace
  4. periodic training — trigger contrastive learning every N steps

Does not change any existing interface, only acts as a Subconscious plugin module.
"""

from __future__ import annotations
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from core.features import FeatureExtractor
from core.predictor import RandomForest  # noqa: F401 — legacy alias, actual class is DeepRiskNet
from .signal_pipeline import SignalCollector, TrainingTriple, SignalSource
from .contrastive import ContrastiveEngine

logger = logging.getLogger("ww.subconscious.runtime")


class RuntimeCollector:
    """
    Runtime collector.

    Hooks into spiral loop lifecycle, automatically:
    - record each action/action feature vector
    - record user intervention
    - record efficiency metric
    - manage self-reflection trace
    - periodically trigger contrastive learning

    usage (called at Subconscious observe_action / should_intervene):
      collector.after_action(state_before, tool_name, success, exit_code, latency, tokens)
      collector.on_user_interrupt(state_before, ctrl_c=True)
      collector.after_task(task_id, state_after, success)
      collector.tick()  # periodically check and trigger
    """

    def __init__(
        self,
        feature_extractor: FeatureExtractor,
        predictor: RandomForest,
        signal_collector: Optional[SignalCollector] = None,
        contrastive_engine: Optional[ContrastiveEngine] = None,
        auto_train_interval: int = 30,  # trigger contrastive learning every N actions
        task_trajectory_enabled: bool = True,
        efficiency_baseline_enabled: bool = True,
    ):
        self.fe = feature_extractor
        self.predictor = predictor
        self.signal = signal_collector or SignalCollector()
        self.contrastive = contrastive_engine or ContrastiveEngine()
        self.auto_train_interval = auto_train_interval
        self.task_trajectory_enabled = task_trajectory_enabled
        self.efficiency_baseline_enabled = efficiency_baseline_enabled

        # internalstate
        self._action_count = 0
        self._training_count = 0
        self._last_action_time = time.time()
        self._last_state_before: Optional[List[float]] = None
        self._current_task_id: str = ""
        self._recent_tokens: List[int] = []
        self._recent_latencies: List[float] = []
        self._intervention_count = 0

    # ── Hook Points ──

    def after_action(
        self,
        tool_name: str,
        success: bool,
        exit_code: int = 0,
        latency: float = 0.0,
        tokens: int = 0,
        state_before: Optional[List[float]] = None,
        spirals_completed: int = 0,
        current_phase_id: int = 0,
        llm_empty: bool = False,
    ):
        """
        Called on each tool invocation.

        record: 
        1. environment feedback triplet (using state_before + exit_code/success)
        2. update into FE observe_action
        3. update trace (if there is an ongoing task)
        """
        self._action_count += 1
        self._record_last_state(state_before, spirals_completed, current_phase_id, llm_empty)

        # environment feedback signal
        if state_before and len(state_before) >= 12:
            self.signal.record_environment(
                state_vector=state_before[:12],
                exit_code=exit_code,
                success=success,
                latency=latency,
                token_count=tokens,
                tool_name=tool_name,
            )

        # update efficiency history
        if latency > 0:
            self._recent_latencies.append(latency)
            if len(self._recent_latencies) > 50:
                self._recent_latencies.pop(0)
        if tokens > 0:
            self._recent_tokens.append(tokens)
            if len(self._recent_tokens) > 50:
                self._recent_tokens.pop(0)

        # update trajectory
        if self._current_task_id and state_before:
            self.signal.record_trajectory_step(
                task_id=self._current_task_id,
                state_before=state_before[:12],
                action=tool_name,
                success=success,
                tokens=tokens,
                latency=latency,
            )

    def on_user_interrupt(
        self,
        state_before: Optional[List[float]] = None,
        ctrl_c: bool = False,
        edit_ratio: float = 0.0,
        follow_up_count: int = 0,
        spirals_completed: int = 0,
        current_phase_id: int = 0,
        llm_empty: bool = False,
    ):
        """
        user intervention call.

        Three intervention types auto-detect:
        - ctrl_c=True → Ctrl+C force break
        - edit_ratio > 0.5 → user significantly modified output
        - follow_up_count > 2 → user continuous follow-up/correction
        """
        self._intervention_count += 1
        self._record_last_state(state_before, spirals_completed, current_phase_id, llm_empty)

        if state_before is None:
            state_before = self._last_state_before
        if state_before is None or len(state_before) < 12:
            return

        if ctrl_c:
            # Ctrl+C = strongest negative signal
            self.signal.record_user_intervention(
                state_vector=state_before[:12],
                intervention_type="ctrl_c",
                severity=1.0,
            )

        if edit_ratio > 0.5:
            # user significantly modified → indicates main consciousness direction correct but details have errors
            self.signal.record_user_intervention(
                state_vector=state_before[:12],
                intervention_type="edit",
                severity=min(1.0, edit_ratio),
            )

        if follow_up_count > 2:
            # user continuous follow-up → main consciousness missed context
            severity = min(1.0, follow_up_count * 0.15)
            self.signal.record_user_intervention(
                state_vector=state_before[:12],
                intervention_type="follow_up",
                severity=severity,
            )

    def on_task_start(
        self,
        task_id: str,
        initial_state: List[float],
    ):
        """task start call (record S₀)."""
        self._current_task_id = task_id
        if self.task_trajectory_enabled and len(initial_state) >= 12:
            self.signal.start_task_trajectory(task_id, initial_state[:12])

    def on_task_end(
        self,
        task_id: str,
        final_state: List[float],
        success: bool,
        phase_id: int = 0,
        spirals: int = 0,
        task_type: str = "",
        tokens_used: int = 0,
        latency_seconds: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """
        task completion call.

        Do three things:
        1. Complete trajectory → S₀→S_final shortcut learning
        2. efficiencymetric (e.g. has  task_type) 
        3. Environment feedback (final result)

        Returns:
            generate shortcut learning result (or None)
        """
        result = None
        self._current_task_id = ""

        # trajectory shortcut learning
        if self.task_trajectory_enabled and len(final_state) >= 12:
            trajectory_result = self.signal.finish_task_trajectory(
                task_id=task_id,
                final_state=final_state[:12],
                success=success,
            )
            if trajectory_result:
                result = {
                    "trajectory": True,
                    "source": "self_correction",
                    "triple_id": trajectory_result.triple_id,
                }

        # efficiency metric
        if self.efficiency_baseline_enabled and task_type and tokens_used > 0:
            baseline = self.signal.get_efficiency_baseline(task_type)
            self.signal.record_efficiency(
                state_vector=final_state[:12] if len(final_state) >= 12 else [0]*12,
                task_type=task_type,
                tokens_used=tokens_used,
                latency_seconds=latency_seconds,
                baseline_tokens=int(baseline.get("avg_tokens", 0)) if baseline.get("avg_tokens") else None,
                baseline_latency=baseline.get("avg_latency"),
            )

        # environment feedback (final result)
        if len(final_state) >= 12:
            self.signal.record_environment(
                state_vector=final_state[:12],
                exit_code=0 if success else 1,
                success=success,
                tool_name="_task_complete",
            )

        # fixed trigger
        self._maybe_train()

        return result

    def tick(self) -> Dict[str, Any]:
        """
        fixed tick (at spiral loop evaluate phase call).

        check if needs trigger contrastive learning.
        """
        return self._maybe_train()

    # ── internal ──

    def _record_last_state(
        self,
        state_vector: Optional[List[float]] = None,
        spirals: int = 0,
        phase_id: int = 0,
        llm_empty: bool = False,
    ):
        """record when state as "next state" (for user intervention no provide state_before case)."""
        if state_vector and len(state_vector) >= 12:
            self._last_state_before = state_vector[:12]
        else:
            vec = self.fe.extract(
                spirals_completed=spirals,
                current_phase_id=phase_id,
                llm_returned_empty=llm_empty,
            )
            self._last_state_before = vec

    def _maybe_train(self) -> Dict[str, Any]:
        """fixed check and trigger contrastive learning."""
        if self._action_count % self.auto_train_interval != 0:
            return {"trained": False, "reason": "not due yet"}

        return self.train()

    def train(self) -> Dict[str, Any]:
        """
        executecontrastive learningtraining. 

        Process:
          1. drain buffer triplets
          2. construct contrastive pairs
          3. execute contrastive_update
          4. recordstatistics
        """
        batch = self.signal.drain()
        if not batch:
            return {"trained": False, "reason": "no signals"}

        if self.predictor.empty():
            return {"trained": False, "reason": "predictor not initialized"}

        # construct contrastive pairs
        pairs = self.signal.get_contrast_pairs(batch)
        if not pairs:
            return {"trained": False, "reason": "could not form contrast pairs"}

        # executecontrastive learning
        result = self.contrastive.contrastive_update(self.predictor, pairs)
        result["signals_consumed"] = len(batch)
        result["pairs_formed"] = len(pairs)

        if result.get("updated"):
            self._training_count += 1
            logger.info(
                f"🧪 contrastive learning #{self._training_count}: "
                f"{result.get('leaves_pushed', 0)} leaves pushed, "
                f"{result.get('margin_resolved', 0)} margins resolved"
            )

        return result

    def stats(self) -> Dict[str, Any]:
        return {
            "actions_collected": self._action_count,
            "trainings_run": self._training_count,
            "interventions_detected": self._intervention_count,
            "signal_pipeline": self.signal.stats(),
            "contrastive_engine": self.contrastive.estimate(),
            "auto_train_interval": self.auto_train_interval,
        }
