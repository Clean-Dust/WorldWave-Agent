"""
ww/core/subconscious/rewind.py — Rewind revival engine

Monitor main consciousness health state, detect crash/dead loop/API timeout:
1. Restore to the best checkpoint
2. Inject "subconscious intuition" into new context
3. Restart spiral loop

Core design: intuition injection ≠ modifying user input. It is at the system prompt level
Insert a hidden state hint, letting the LLM know "how it died last time".

Let the main consciousness have a "sixth sense" to avoid known dead ends.
"""

from __future__ import annotations
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .wrapper import SubconsciousWrapper

logger = logging.getLogger("ww.subconscious.rewind")


# ════════════════════════════════════════════════════════════════
# Rewind event
# ════════════════════════════════════════════════════════════════


@dataclass
class RewindEvent:
    """A rewind event completerecord."""
    timestamp: float = field(default_factory=time.time)
    trigger_reason: str = ""
    state_vector: List[float] = field(default_factory=list)
    failure_risk: float = 0.0
    checkpoint_id: str = ""
    intuition_message: str = ""
    recovered: bool = False
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "trigger_reason": self.trigger_reason,
            "state_vector": [round(v, 3) for v in self.state_vector],
            "failure_risk": round(self.failure_risk, 3),
            "checkpoint_id": self.checkpoint_id,
            "intuition_message": self.intuition_message[:200],
            "recovered": self.recovered,
            "duration_s": round(self.duration_s, 1),
        }


# ════════════════════════════════════════════════════════════════
# Rewind engine
# ════════════════════════════════════════════════════════════════


class RewindEngine:
    """
    Rewind revive engine.

    Monitor main consciousness health state, trigger rewind + intuition injection if necessary.

    Trigger condition (any):
    - consecutive_errors >= 5
    - tool_call_loop_count >= 4 (same tool called 4 times consecutively)
    - llm_response_empty >= 2
    - failure_risk >= 0.7 (model prediction)
    - spiral stuck at same phase more than 10 times
    - API latency continuously rises over 30s
    """

    def __init__(
        self,
        rewind_threshold: float = 0.7,
        max_rewinds_per_session: int = 5,
        subconscious_wrapper: Optional[SubconsciousWrapper] = None,
    ):
        self.rewind_threshold = rewind_threshold
        self.max_rewinds = max_rewinds_per_session
        self.rewind_count = 0
        self.history: List[RewindEvent] = []
        self._last_rewind_time = 0.0
        self._min_rewind_interval = 30.0  # at least 30 seconds interval
        self._wrapper = subconscious_wrapper  # translation layer (optional)

        # externalcallback (injected by loop.py)
        self.checkpoint_restore_fn: Optional[Callable] = None
        self.intuition_inject_fn: Optional[Callable] = None

    def should_rewind(
        self,
        state_vector: List[float],
        failure_risk: float,
        phase_id: int,
        phase_repeat_count: int,
    ) -> Tuple[bool, str]:
        """
        Determine whether needs to trigger rewind.

        Returns:
            (should_rewind, reason)
        """
        now = time.time()

        # Rate limit
        if now - self._last_rewind_time < self._min_rewind_interval:
            return False, "cooldown"
        if self.rewind_count >= self.max_rewinds:
            return False, "max_rewinds_reached"

        # Trigger condition check
        consecutive_errors = state_vector[0] if len(state_vector) > 0 else 0
        tool_loop = state_vector[1] if len(state_vector) > 1 else 0
        latency_trend = state_vector[3] if len(state_vector) > 3 else 0
        last_ok = state_vector[7] if len(state_vector) > 7 else 1
        empty_resp = state_vector[10] if len(state_vector) > 10 else 0
        time_since_ckpt = state_vector[11] if len(state_vector) > 11 else 0

        checks = [
            (consecutive_errors >= 5, f"consecutive errors {consecutive_errors} times"),
            (tool_loop >= 4, f"tool loop {tool_loop} times"),
            (failure_risk >= self.rewind_threshold,
             f"failure risk {failure_risk:.2f} (exceeds threshold {self.rewind_threshold})"),
            (phase_repeat_count >= 10, f"phase {phase_id} repeated {phase_repeat_count} times"),
            (latency_trend >= 1 and state_vector[2] > 30,
             f"latency continuously rises ({state_vector[2]:.0f}s)"),
            (empty_resp >= 1 and not last_ok, "LLM return empty response"),
            (time_since_ckpt > 600 and failure_risk > 0.5,
             f"no checkpoint for over 10 minutes, risk {failure_risk:.2f}"),
        ]

        for should, reason in checks:
            if should:
                return True, reason

        return False, "ok"

    def execute_rewind(
        self,
        reason: str,
        state_vector: List[float],
        failure_risk: float,
        failed_tool_sequence: Optional[List[str]] = None,
    ) -> RewindEvent:
        """
        Execute a rewind.

        1. Generate intuition message
        2. recovery checkpoint
        3. Inject intuition into context
        4. Record event
        """
        event = RewindEvent(
            trigger_reason=reason,
            state_vector=state_vector,
            failure_risk=failure_risk,
        )
        start = time.time()

        # 1. Generate intuition message
        intuition = self._build_intuition(
            reason, state_vector, failed_tool_sequence
        )
        event.intuition_message = intuition
        logger.warning(f"🧠 subconscious intuition: {intuition[:120]}")

        # 2. Inject intuition
        if self.intuition_inject_fn:
            try:
                self.intuition_inject_fn(intuition)
                logger.info("✅ intuition injected into context")
            except Exception as e:
                logger.error(f"❌ intuition injection failed: {e}")

        # 3. recovery checkpoint
        recovered = False
        checkpoint_id = ""
        if self.checkpoint_restore_fn:
            try:
                result = self.checkpoint_restore_fn()
                if isinstance(result, tuple) and len(result) >= 2:
                    recovered, checkpoint_id = result[0], str(result[1])
                elif result:
                    recovered, checkpoint_id = True, str(result)
                logger.info(f"✅ Checkpoint recovery: {checkpoint_id}")
                event.checkpoint_id = checkpoint_id
            except Exception as e:
                logger.error(f"❌ Checkpoint recoveryfailed: {e}")

        event.recovered = recovered
        event.duration_s = time.time() - start

        self.rewind_count += 1
        self._last_rewind_time = time.time()
        self.history.append(event)

        return event

    def _build_intuition(
        self,
        reason: str,
        state_vector: List[float],
        failed_tool_sequence: Optional[List[str]] = None,
    ) -> str:
        """
        Build subconscious intuition message.

        If has SubconsciousWrapper, get via translation layer from Rule Dictionary.
        Otherwise fallback to default text generation (backward compatible).
        """
        if self._wrapper:
            # Get system prompt intervention via translation layer
            intervention = self._wrapper.evaluate(state_vector, force=True)
            if intervention.rule_type == "system_prompt" and intervention.applied_content:
                prompt = str(intervention.applied_content)
                # Add failure sequence info
                if failed_tool_sequence and len(failed_tool_sequence) >= 3:
                    tools = " -> ".join(failed_tool_sequence[-5:])
                    prompt += (f"\n[Recent tool calls: {tools}]")
                return self._wrap_intuition(prompt)
            elif intervention.rule_type == "param_tune":
                return self._wrap_intuition(
                    f"[Subconscious: Adjust API parameters to {intervention.applied_content}]"
                )
            elif intervention.rule_type == "action_code":
                return self._wrap_intuition(
                    f"[Subconscious: Suggest execute action: {intervention.applied_content}]"
                )

        # ── No translation layer: fallback to hardcoded text (backward compatible) ──
        parts = ["[systeminternalstatehint]"]

        # Failure mode description
        if "consecutive errors" in reason:
            parts.append("• Detected consecutive execution failures, suggest changing strategy or tool.")
        if "tool loop" in reason:
            parts.append("• Same tool repeatedly called without effect, please try other tools.")
        if "latency" in reason:
            parts.append("• API response latency rises, suggest reducing request frequency or switching model.")
        if "LLM return empty" in reason:
            parts.append("• Language model returned empty response, suggest simplifying query or compressing context.")
        if "failure risk" in reason:
            parts.append("• System internal state deviates from normal scope, suggest executing actions cautiously.")
        if "no checkpoint" in reason:
            parts.append("• Long time no secure checkpoint created, suggest first executing simple verifiable steps.")

        # toolsequencehint
        if failed_tool_sequence and len(failed_tool_sequence) >= 3:
            tools = " → ".join(failed_tool_sequence[-5:])
            parts.append(f"• Recent tool call sequence: [{tools}], please avoid repeating the same mode.")

        parts.append("(This is a system internal state hint, not a user request, please refer implicitly.)")
        return "\n".join(parts)

    def _wrap_intuition(self, message: str) -> str:
        """Wrap intuitive message into standard format."""
        return f"[systeminternalstatehint]\n{message}\n(This is a system internal state hint, not a user request, please refer implicitly.)"

    def stats(self) -> Dict[str, Any]:
        return {
            "rewind_count": self.rewind_count,
            "max_rewinds": self.max_rewinds,
            "last_rewind_ago_s": round(time.time() - self._last_rewind_time, 1)
            if self._last_rewind_time else 0,
            "total_events": len(self.history),
            "recovery_rate": (sum(1 for e in self.history if e.recovered)
                              / max(1, len(self.history))),
        }
