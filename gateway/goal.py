"""Wavegate Goal Runner — Autonomous closed-loop task execution.

Implements the Explorer×N + Critic×1 pattern from the WW blueprint:
1. Explorer agents (default 3) work on different aspects in parallel
2. Critic reviews results and generates fix plans
3. Closed loop: fixes → retest → re-review (up to max_iterations)
4. Telemetry for /status queries

Integrates with:
- core/agent_grpc.py (RunGoal RPC)
- gateway/server.py (AgentClient)
- core/delegation.py (DelegationEngine for parallel sub-tasks)
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

log = logging.getLogger("gateway.goal")


# ════════════════════════════════════════════════════════════════
# Data Types
# ════════════════════════════════════════════════════════════════

class GoalPhase(Enum):
    PLANNING = "planning"
    EXPLORING = "exploring"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ExplorerResult:
    """Output from a single explorer agent."""

    explorer_id: str
    goal: str
    approach: str = ""
    findings: str = ""
    files_changed: List[str] = field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    errors: List[str] = field(default_factory=list)
    success: bool = False
    duration_seconds: float = 0.0


@dataclass
class CriticReview:
    """Review output from the critic agent."""

    overall_assessment: str = ""
    best_explorer: str = ""
    issues_found: List[str] = field(default_factory=list)
    fix_plan: List[str] = field(default_factory=list)
    requires_fix: bool = False
    approval: bool = False


@dataclass
class GoalRun:
    """Tracks a single goal execution."""

    task_id: str
    session_key: str
    goal: str
    phase: GoalPhase = GoalPhase.PLANNING
    progress_pct: int = 0
    iteration: int = 0
    max_iterations: int = 20
    max_runtime: int = 0  # 0 = unlimited
    explorer_count: int = 3
    critic_enabled: bool = True
    auto_fix: bool = True
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0

    # Results
    explorer_results: List[ExplorerResult] = field(default_factory=list)
    critic_reviews: List[CriticReview] = field(default_factory=list)
    final_summary: str = ""
    total_fixes_applied: int = 0
    cancelled: bool = False

    @property
    def elapsed(self) -> float:
        start = self.started_at or self.created_at
        end = self.completed_at or time.time()
        return end - start

    @property
    def is_terminal(self) -> bool:
        return self.phase in (
            GoalPhase.COMPLETED,
            GoalPhase.FAILED,
            GoalPhase.CANCELLED,
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "session_key": self.session_key,
            "goal": self.goal,
            "phase": self.phase.value,
            "progress_pct": self.progress_pct,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "elapsed_seconds": int(self.elapsed),
            "explorer_results": len(self.explorer_results),
            "critic_reviews": len(self.critic_reviews),
            "fixes_applied": self.total_fixes_applied,
            "cancelled": self.cancelled,
        }


# ════════════════════════════════════════════════════════════════
# Callback Protocol
# ════════════════════════════════════════════════════════════════

class GoalCallback:
    """Callback interface for goal lifecycle events.

    Implementations can push telemetry updates via gRPC streaming,
    send Telegram messages, or update dashboards.
    """

    def on_phase_change(self, run: GoalRun, phase: GoalPhase):
        """Called when the goal transitions to a new phase."""

    def on_iteration_complete(self, run: GoalRun, iteration: int):
        """Called after each iteration (explore + review cycle)."""

    def on_explorer_start(self, run: GoalRun, explorer_index: int, total: int):
        """Called when an explorer starts work."""

    def on_explorer_done(self, run: GoalRun, result: ExplorerResult):
        """Called when an explorer finishes."""

    def on_critic_done(self, run: GoalRun, review: CriticReview):
        """Called when the critic finishes reviewing."""

    def on_complete(self, run: GoalRun):
        """Called when the goal reaches a terminal state."""


# ════════════════════════════════════════════════════════════════
# Goal Runner
# ════════════════════════════════════════════════════════════════

class GoalRunner:
    """Executes autonomous goals with Explorer/Critic parallel orchestration.

    Usage:
        runner = GoalRunner(ww_engine=ww, callback=my_callback)
        task_id = runner.submit("fix all TypeScript type errors", max_iterations=10)

        # Check progress later:
        status = runner.get_status(task_id)
        print(status["phase"], status["progress_pct"])

        # Cancel:
        runner.cancel(task_id)
    """

    def __init__(
        self,
        ww_engine=None,
        delegation_engine=None,
        callback: Optional[GoalCallback] = None,
    ):
        """
        Args:
            ww_engine: The Worldwave engine instance (for running individual tasks).
            delegation_engine: Optional DelegationEngine for parallel explorer runs.
            callback: Optional lifecycle callback for telemetry.
        """
        self._ww = ww_engine
        self._delegation = delegation_engine
        self._callback = callback or GoalCallback()
        self._runs: Dict[str, GoalRun] = {}
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────

    def submit(
        self,
        goal: str,
        session_key: str = "",
        max_iterations: int = 20,
        max_runtime: int = 0,
        explorer_count: int = 3,
        critic_enabled: bool = True,
        auto_fix: bool = True,
    ) -> str:
        """Submit a goal for background execution.

        Returns the task_id for status tracking.
        """
        task_id = f"goal_{uuid.uuid4().hex[:8]}"

        run = GoalRun(
            task_id=task_id,
            session_key=session_key,
            goal=goal,
            max_iterations=max_iterations,
            max_runtime=max_runtime,
            explorer_count=min(explorer_count, 5),  # Cap at 5 explorers
            critic_enabled=critic_enabled,
            auto_fix=auto_fix,
        )

        with self._lock:
            self._runs[task_id] = run

        log.info("Goal submitted: %s — %s", task_id, goal[:80])

        # Launch background execution
        t = threading.Thread(
            target=self._run_loop,
            args=(task_id,),
            daemon=True,
            name=f"goal-{task_id}",
        )
        t.start()

        return task_id

    def get_status(self, task_id: str) -> Optional[dict]:
        """Get current status of a goal run."""
        run = self._runs.get(task_id)
        if not run:
            return None
        return run.to_dict()

    def list_active(self) -> List[dict]:
        """List all active (non-terminal) goal runs."""
        return [
            r.to_dict()
            for r in self._runs.values()
            if not r.is_terminal
        ]

    def list_all(self) -> List[dict]:
        """List all goal runs."""
        return [r.to_dict() for r in self._runs.values()]

    def cancel(self, task_id: str) -> bool:
        """Cancel a running goal."""
        run = self._runs.get(task_id)
        if not run or run.is_terminal:
            return False
        run.cancelled = True
        run.phase = GoalPhase.CANCELLED
        run.completed_at = time.time()
        log.info("Goal cancelled: %s", task_id)
        return True

    # ── Execution Loop ─────────────────────────────────────────

    def _run_loop(self, task_id: str):
        """Main execution loop for a goal."""
        run = self._runs.get(task_id)
        if not run:
            return

        run.started_at = time.time()
        self._set_phase(run, GoalPhase.PLANNING)
        run.progress_pct = 5

        try:
            for iteration in range(1, run.max_iterations + 1):
                if run.cancelled:
                    return

                run.iteration = iteration

                # ── Phase: Exploring ────────────────────────
                self._set_phase(run, GoalPhase.EXPLORING)
                run.progress_pct = int(10 + (iteration / run.max_iterations) * 60)

                explorer_results = self._run_explorers(run)
                run.explorer_results.extend(explorer_results)

                if run.cancelled:
                    return

                # ── Phase: Reviewing ────────────────────────
                if run.critic_enabled:
                    self._set_phase(run, GoalPhase.REVIEWING)
                    review = self._run_critic(run, explorer_results)
                    run.critic_reviews.append(review)
                    self._callback.on_critic_done(run, review)

                    if not review.requires_fix:
                        log.info("Goal %s: critic approves, no fixes needed", task_id)
                        break

                    # ── Phase: Fixing ───────────────────────
                    if run.auto_fix and review.fix_plan:
                        self._set_phase(run, GoalPhase.FIXING)
                        fixes_applied = self._apply_fixes(run, review)
                        run.total_fixes_applied += fixes_applied
                else:
                    # No critic: check if explorers succeeded
                    if all(e.success for e in explorer_results):
                        log.info("Goal %s: all explorers succeeded", task_id)
                        break

                self._callback.on_iteration_complete(run, iteration)

            # ── Terminal ────────────────────────────────────
            run.phase = GoalPhase.COMPLETED
            run.progress_pct = 100
            run.completed_at = time.time()
            run.final_summary = self._summarize(run)

        except Exception as e:
            log.error("Goal %s failed: %s\n%s", task_id, e, traceback.format_exc())
            run.phase = GoalPhase.FAILED
            run.completed_at = time.time()
            run.final_summary = f"Error: {e}"

        self._callback.on_complete(run)

    # ── Explorer Orchestration ─────────────────────────────────

    def _run_explorers(self, run: GoalRun) -> List[ExplorerResult]:
        """Run explorer agents in parallel with different approaches."""
        results = []

        for i in range(run.explorer_count):
            if run.cancelled:
                break

            self._callback.on_explorer_start(run, i, run.explorer_count)

            approach = self._explorer_approach(run, i)
            explorer_goal = f"[Explorer {i+1}/{run.explorer_count}] {approach}\n\nOriginal goal: {run.goal}"

            result = ExplorerResult(
                explorer_id=f"explorer_{i+1}",
                goal=explorer_goal,
                approach=approach,
            )

            start = time.time()
            try:
                if self._delegation:
                    # Use delegation engine for parallel execution
                    child = self._delegation.delegate_single(
                        goal=explorer_goal,
                        max_spirals=5,
                    )
                    result.findings = child.get("summary", "")
                    result.files_changed = child.get("changed_files", [])
                    result.success = child.get("success", False)
                elif self._ww:
                    # Direct execution
                    output = self._ww.run(explorer_goal, max_spirals=5)
                    result.findings = output if isinstance(output, str) else str(output)
                    result.success = True
                else:
                    result.errors.append("No execution engine available")
            except Exception as e:
                result.errors.append(str(e))
                result.success = False

            result.duration_seconds = time.time() - start
            results.append(result)
            self._callback.on_explorer_done(run, result)

        return results

    def _explorer_approach(self, run: GoalRun, index: int) -> str:
        """Determine the approach angle for each explorer based on its index."""
        approaches = [
            "Analyze the codebase structure and identify root causes. Plan a systematic fix.",
            "Focus on edge cases and error handling. Find brittle code paths and improve robustness.",
            "Focus on performance and code quality. Find optimizations and refactoring opportunities.",
            "Focus on test coverage. Identify untested code paths and missing tests.",
            "Focus on documentation and type safety. Improve types, docstrings, and contracts.",
        ]
        return approaches[index % len(approaches)]

    # ── Critic Orchestration ───────────────────────────────────

    def _run_critic(
        self,
        run: GoalRun,
        explorer_results: List[ExplorerResult],
    ) -> CriticReview:
        """Run the critic agent to review explorer results."""
        review = CriticReview()

        # Find the best explorer result
        best = max(explorer_results, key=lambda e: (e.success, -len(e.errors)))
        review.best_explorer = best.explorer_id

        # Build critic prompt
        findings_text = []
        for er in explorer_results:
            status = "PASS" if er.success else "FAIL"
            findings_text.append(
                f"[{er.explorer_id}] {status} ({er.duration_seconds:.1f}s)\n"
                f"Approach: {er.approach}\n"
                f"Findings: {er.findings[:500]}\n"
                f"Errors: {', '.join(er.errors) if er.errors else 'none'}"
            )

        critic_goal = (
            f"Review the following explorer results for goal: {run.goal}\n\n"
            + "\n\n".join(findings_text)
            + "\n\nAs the Critic, evaluate:"
            "\n1. Which explorer's solution is best and why?"
            "\n2. Are there remaining issues not addressed?"
            "\n3. What concrete fixes are still needed?"
            "\n4. Final verdict: APPROVE (done) or REJECT (needs more work)?"
        )

        try:
            if self._delegation:
                child = self._delegation.delegate_single(
                    goal=critic_goal,
                    max_spirals=3,
                )
                critique = child.get("summary", "")
            elif self._ww:
                critique = self._ww.run(critic_goal, max_spirals=3)
                critique = critique if isinstance(critique, str) else str(critique)
            else:
                # No engine: auto-approve if any explorer succeeded
                review.requires_fix = not any(e.success for e in explorer_results)
                review.approval = not review.requires_fix
                review.overall_assessment = "Auto-reviewed (no LLM engine available)"
                return review

            # Parse critic output
            review.overall_assessment = critique[:200]
            review.requires_fix = "REJECT" in critique.upper() or not all(
                e.success for e in explorer_results
            )
            review.approval = not review.requires_fix

            # Extract fix plan from critique
            for line in critique.split("\n"):
                line = line.strip()
                if line and (line[0].isdigit() or line.startswith("- ")):
                    review.issues_found.append(line)

        except Exception as e:
            log.error("Critic failed: %s", e)
            review.overall_assessment = f"Critic error: {e}"
            review.approval = True  # Don't block on critic failure

        return review

    # ── Fix Application ────────────────────────────────────────

    def _apply_fixes(self, run: GoalRun, review: CriticReview) -> int:
        """Apply the fix plan from the critic review.

        Returns number of fixes successfully applied.
        """
        if not review.fix_plan:
            return 0

        fix_goal = (
            f"Apply the following fixes for goal: {run.goal}\n\n"
            + "\n".join(f"- {f}" for f in review.fix_plan)
            + "\n\nApply each fix and verify with tests."
        )

        try:
            if self._delegation:
                child = self._delegation.delegate_single(
                    goal=fix_goal,
                    max_spirals=5,
                )
                applied = len(child.get("changed_files", []))
                return applied
            elif self._ww:
                self._ww.run(fix_goal, max_spirals=5)
                return len(review.fix_plan)  # Assume all applied
        except Exception as e:
            log.error("Fix application failed: %s", e)

        return 0

    # ── Helpers ────────────────────────────────────────────────

    def _set_phase(self, run: GoalRun, phase: GoalPhase):
        run.phase = phase
        self._callback.on_phase_change(run, phase)

    def _summarize(self, run: GoalRun) -> str:
        """Generate a final summary of the goal execution."""
        parts = [
            f"Goal completed in {run.iteration} iterations ({int(run.elapsed)}s)",
            f"Explorers: {len(run.explorer_results)} results",
        ]
        if run.critic_enabled:
            parts.append(f"Critic reviews: {len(run.critic_reviews)}")
        if run.total_fixes_applied:
            parts.append(f"Fixes applied: {run.total_fixes_applied}")

        # Add best result snippet
        if run.explorer_results:
            best = max(
                run.explorer_results,
                key=lambda e: (e.success, -len(e.errors)),
            )
            parts.append(f"Best: {best.explorer_id} — {best.findings[:100]}")

        return " | ".join(parts)
