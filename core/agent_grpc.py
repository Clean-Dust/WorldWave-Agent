"""WW Core Agent gRPC Service.

Implements the Agent gRPC service defined in proto/wavegate/v1/agent.proto.
This is the Execution Plane's interface — Wavegate calls this to run tasks.

Delegates to the Worldwave spiral loop engine for actual task execution.
Goal mode uses gateway/goal.py for Explorer×N + Critic×1 orchestration.

All service methods are synchronous (not async) for compatibility with
grpc.server(ThreadPoolExecutor).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Dict

import grpc

from proto.wavegate.v1 import agent_pb2 as ag_pb2
from proto.wavegate.v1 import agent_pb2_grpc as ag_grpc
from proto.wavegate.v1 import unified_message_pb2 as um_pb2

log = logging.getLogger("ww.agent")


class AgentServiceImpl(ag_grpc.AgentServicer):
    """Implements the Agent gRPC service (sync methods).

    Connects Wavegate to the Worldwave spiral loop engine.
    """

    def __init__(self, ww=None):
        self._ww = ww
        self._active_goals: Dict[str, dict] = {}
        self._goal_counter = 0
        self._lock = threading.Lock()
        self._goal_runner = None  # Lazy-init from gateway.goal

    def set_ww(self, ww):
        """Inject the Worldwave instance after construction."""
        self._ww = ww

    @property
    def ww(self):
        if self._ww is None:
            from core.loop import Worldwave
            self._ww = Worldwave()
        return self._ww

    # ── RunTask ───────────────────────────────────────────────

    def RunTask(self, request, context):
        """Execute a single task with server-side streaming progress updates.

        Spawns the spiral loop in a background thread and yields
        AgentResponse chunks as each cognitive phase completes.
        """
        log.info("RunTask: session=%s goal=%s", request.session_key, request.goal[:80])

        q = queue.Queue()
        stream_seq = [0]

        def on_progress(phase: str, message: str, progress_pct: int):
            chunk = um_pb2.StreamChunk(
                delta=f"[{phase}] {message}",
                seq=stream_seq[0],
                stream_type="thinking",
            )
            q.put(um_pb2.AgentResponse(
                correlation_id=str(uuid.uuid4()),
                session_key=request.session_key,
                payload=um_pb2.ResponsePayload(stream_chunk=chunk),
                is_final=False,
                stream_seq=stream_seq[0],
            ))
            stream_seq[0] += 1

        def runner():
            try:
                result = self.ww.run(
                    request.goal,
                    max_spirals=request.max_spirals,
                    on_spiral_progress=on_progress,
                )
                q.put(result)
            except Exception as e:
                q.put(e)

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        while True:
            item = q.get()
            if isinstance(item, Exception):
                log.error("RunTask error: %s", item)
                yield um_pb2.AgentResponse(
                    correlation_id=str(uuid.uuid4()),
                    session_key=request.session_key,
                    payload=um_pb2.ResponsePayload(
                        error=um_pb2.ErrorInfo(code="TASK_ERROR", message=str(item)),
                    ),
                    is_final=True,
                )
                return
            if isinstance(item, dict):
                yield um_pb2.AgentResponse(
                    correlation_id=str(uuid.uuid4()),
                    session_key=request.session_key,
                    payload=um_pb2.ResponsePayload(text=str(item)),
                    is_final=True,
                    stream_seq=stream_seq[0],
                )
                return
            yield item

    # ── RunGoal ───────────────────────────────────────────────

    def RunGoal(self, request, context):
        """Submit a goal for autonomous background execution.

        Uses Explorer×N + Critic×1 pattern via GoalRunner when available.
        Falls back to sequential loop if GoalRunner cannot be imported.
        """
        # Try GoalRunner first
        runner = self._get_goal_runner()
        if runner:
            config = request.config
            task_id = runner.submit(
                goal=request.goal,
                session_key=request.session_key,
                max_iterations=request.max_iterations or 20,
                max_runtime=request.max_runtime_seconds or 0,
                explorer_count=config.explorer_count if config else 3,
                critic_enabled=config.critic_enabled if config else True,
                auto_fix=config.auto_fix if config else True,
            )

            # Mirror to active_goals for WatchGoal compatibility
            with self._lock:
                self._active_goals[task_id] = {
                    "task_id": task_id,
                    "session_key": request.session_key,
                    "goal": request.goal,
                    "phase": "planning",
                    "progress_pct": 0,
                    "iteration": 0,
                    "created_at": time.time(),
                }
                # Start a thread to sync status from GoalRunner → active_goals
                t = threading.Thread(
                    target=self._sync_goal_status,
                    args=(task_id, runner),
                    daemon=True,
                )
                t.start()

            log.info("Goal started (GoalRunner): %s", task_id)
            return ag_pb2.RunGoalResponse(task_id=task_id, accepted=True)

        # Fallback: sequential loop
        with self._lock:
            self._goal_counter += 1
            task_id = f"goal_{self._goal_counter}_{int(time.time())}"

            self._active_goals[task_id] = {
                "task_id": task_id,
                "session_key": request.session_key,
                "goal": request.goal,
                "phase": "planning",
                "progress_pct": 0,
                "iteration": 0,
                "spirals_used": 0,
                "created_at": time.time(),
            }

        t = threading.Thread(target=self._execute_goal, args=(task_id, request), daemon=True)
        t.start()

        log.info("Goal started (fallback): %s", task_id)
        return ag_pb2.RunGoalResponse(task_id=task_id, accepted=True)

    def _execute_goal(self, task_id: str, request):
        """Background goal execution loop."""
        goal_info = self._active_goals.get(task_id)
        if not goal_info:
            return

        max_iterations = request.max_iterations or 20

        for iteration in range(1, max_iterations + 1):
            if task_id not in self._active_goals:
                break

            goal_info["iteration"] = iteration
            goal_info["phase"] = "executing"
            goal_info["progress_pct"] = int(iteration / max_iterations * 80)

            try:
                result = self.ww.run(
                    f"[Goal iteration {iteration}/{max_iterations}] {request.goal}",
                    max_spirals=5,
                )
                goal_info["spirals_used"] += 5
            except Exception as e:
                log.error("Goal iteration %d failed: %s", iteration, e)

        goal_info["phase"] = "completed"
        goal_info["progress_pct"] = 100
        log.info("Goal completed: %s", task_id)

    # ── WatchGoal ─────────────────────────────────────────────

    def WatchGoal(self, request, context):
        """Stream status updates for a running goal."""
        task_id = request.task_id
        last_phase = ""
        while task_id in self._active_goals:
            goal = self._active_goals[task_id]
            if goal["phase"] != last_phase:
                last_phase = goal["phase"]
                yield um_pb2.StatusUpdate(
                    phase=goal["phase"],
                    message=f"Iteration {goal['iteration']}: {goal['phase']}",
                    progress_pct=goal["progress_pct"],
                    iteration=goal["iteration"],
                    spirals_used=goal["spirals_used"],
                )
            if goal["phase"] == "completed":
                break
            time.sleep(2)

    # ── CancelGoal ────────────────────────────────────────────

    def CancelGoal(self, request, context):
        task_id = request.task_id
        # Try GoalRunner first
        runner = self._get_goal_runner()
        if runner and runner.cancel(task_id):
            with self._lock:
                self._active_goals.pop(task_id, None)
            log.info("Goal cancelled (GoalRunner): %s", task_id)
            return ag_pb2.CancelGoalResponse(cancelled=True, message="Goal cancelled")

        with self._lock:
            if task_id in self._active_goals:
                del self._active_goals[task_id]
                log.info("Goal cancelled: %s", task_id)
                return ag_pb2.CancelGoalResponse(cancelled=True, message="Goal cancelled")
        return ag_pb2.CancelGoalResponse(cancelled=False, message="Goal not found")

    # ── SteerTask ─────────────────────────────────────────────

    def SteerTask(self, request, context):
        log.info("Steer: session=%s", request.session_key)
        return ag_pb2.SteerTaskResponse(
            accepted=True,
            message="Context will be injected at next prompt refresh",
        )

    # ── AbortTask ─────────────────────────────────────────────

    def AbortTask(self, request, context):
        log.info("Abort: session=%s reason=%s", request.session_key, request.reason)
        return ag_pb2.AbortTaskResponse(aborted=True, message="Task aborted")

    # ── GoalRunner Integration ────────────────────────────────

    def _get_goal_runner(self):
        """Lazy-load the GoalRunner from gateway.goal."""
        if self._goal_runner is not None:
            return self._goal_runner
        try:
            from gateway.goal import GoalRunner
            self._goal_runner = GoalRunner(
                ww_engine=self.ww,
            )
            log.info("GoalRunner initialized")
            return self._goal_runner
        except ImportError:
            log.debug("GoalRunner not available — using fallback")
            return None

    def _sync_goal_status(self, task_id: str, runner):
        """Periodically sync GoalRunner status to active_goals for WatchGoal."""
        while True:
            status = runner.get_status(task_id)
            if not status:
                break

            with self._lock:
                if task_id in self._active_goals:
                    self._active_goals[task_id].update({
                        "phase": status["phase"],
                        "progress_pct": status["progress_pct"],
                        "iteration": status["iteration"],
                    })

            if status["phase"] in ("completed", "failed", "cancelled"):
                break

            time.sleep(3)

    # ── AgentInfo ─────────────────────────────────────────────

    def AgentInfo(self, request, context):
        return ag_pb2.AgentInfoResponse(
            version="0.5.0",
            active_sessions=len(self._active_goals),
            active_goals=len(self._active_goals),
            available_tools=[t.name for t in self.ww.tools.list_tools()] if self.ww else [],
            supported_models=["deepseek-v4-flash", "deepseek-v4-pro"],
        )


def serve_agent(ww=None, port: int = 9301, max_workers: int = 10):
    """Start the Agent gRPC server in a background thread.

    Returns (server, service) for lifecycle management.
    """
    from concurrent import futures

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))

    service = AgentServiceImpl(ww=ww)
    ag_grpc.add_AgentServicer_to_server(service, server)

    addr = f"[::]:{port}"
    server.add_insecure_port(addr)
    server.start()
    log.info("Agent gRPC server listening on %s", addr)
    return server, service
