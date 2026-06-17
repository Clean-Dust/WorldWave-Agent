"""Wavegate Server — the independent Control Plane daemon.

Wavegate runs as a standalone process that:
1. Hosts the gRPC Gateway service for platform adapters.
2. Manages platform adapter lifecycles (start/stop/health).
3. Normalizes incoming messages into UnifiedMessage format.
4. Routes messages to the WW Agent Runtime via gRPC with queue modes.
5. Streams Agent responses back to the originating platform adapters.

Usage:
    gateway-server --agent-addr localhost:9301 --gateway-port 9302
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from concurrent import futures
from dataclasses import dataclass
from typing import List, Optional

import grpc

# ── Proto-generated code ──────────────────────────────────────
from proto.wavegate.v1 import unified_message_pb2 as um_pb2
from proto.wavegate.v1 import gateway_pb2 as gw_pb2
from proto.wavegate.v1 import gateway_pb2_grpc as gw_grpc
from proto.wavegate.v1 import agent_pb2 as ag_pb2
from proto.wavegate.v1 import agent_pb2_grpc as ag_grpc

# ── Internal modules ──────────────────────────────────────────
from gateway.adapters import AdapterRegistry
from gateway.queue import QueueManager
from gateway.session import SessionManager
from gateway.attention import BayesianAttentionGate

log = logging.getLogger("gateway")


# ════════════════════════════════════════════════════════════════
# Gateway gRPC Service Implementation
# ════════════════════════════════════════════════════════════════

class GatewayServiceImpl(gw_grpc.GatewayServicer):
    """Implements the Gateway gRPC service.

    Platform adapters call these methods to push messages into the system.
    """

    def __init__(
        self,
        session_mgr: SessionManager,
        queue_mgr: QueueManager,
        agent_stub: ag_grpc.AgentStub,
    ):
        self.session_mgr = session_mgr
        self.queue_mgr = queue_mgr
        self.agent_stub = agent_stub
        self._started_at = time.time()

        # ── Thalamus: Bayesian attention gate ──
        self.attention_gate = BayesianAttentionGate()

    def set_goal(self, goal: str):
        """Update the attention gate's current cognitive goal."""
        self.attention_gate.set_goal(goal)

    # ── Ingest ─────────────────────────────────────────────────

    async def Ingest(self, request, context) -> gw_pb2.IngestResponse:
        """Push a single message into the system."""
        msg = request.message
        event_id = msg.event_id or str(uuid.uuid4())

        # ── Thalamus: attention gate filtering ──
        content = ""
        if msg.content.text:
            content = msg.content.text
        if content and self.attention_gate._goal:
            gated = self.attention_gate.evaluate(content, source=msg.platform)
            if gated.posterior < self.attention_gate.get_effective_threshold():
                # Suppress but still buffer (not discarded)
                log.debug("Attention gate suppressed: %s (score=%.3f)", event_id[:8], gated.posterior)
                # Still queue with low priority flag
                msg.routing.priority = max(0, msg.routing.priority - 5)

        # Register/update session
        session = self.session_mgr.get_or_create(msg.session_key, msg.sender, msg.platform)

        if request.immediate:
            # Bypass queue — send directly to agent
            self.queue_mgr.enqueue_immediate(session.session_key, msg)
            return gw_pb2.IngestResponse(
                event_id=event_id,
                queued=False,
                queue_position=0,
            )

        # Route through queue manager
        position = self.queue_mgr.enqueue(session.session_key, msg, hints=msg.routing)
        log.info("Ingest: %s session=%s queue_pos=%d", event_id[:8], msg.session_key, position)
        return gw_pb2.IngestResponse(
            event_id=event_id,
            queued=True,
            queue_position=position,
        )

    # ── StreamIngest ───────────────────────────────────────────

    async def StreamIngest(self, request_iterator, context):
        """Bidirectional stream: adapters push messages, receive responses."""
        session_key = None
        async for msg in request_iterator:
            session_key = msg.session_key
            session = self.session_mgr.get_or_create(session_key, msg.sender, msg.platform)
            self.queue_mgr.enqueue(session_key, msg, hints=msg.routing)

            # Stream back any pending responses
            responses = self.queue_mgr.drain_responses(session_key)
            for resp in responses:
                yield resp

    # ── Queue State ────────────────────────────────────────────

    async def GetQueueState(self, request, context) -> gw_pb2.QueueStateResponse:
        state = self.queue_mgr.get_state(request.session_key)
        session = self.session_mgr.get(request.session_key)
        return gw_pb2.QueueStateResponse(
            session_key=request.session_key,
            queued_messages=state.queued_count,
            current_status=state.status,
            current_goal=session.current_goal if session else "",
        )

    # ── Interrupt ──────────────────────────────────────────────

    async def Interrupt(self, request, context) -> gw_pb2.InterruptResponse:
        success = self.queue_mgr.interrupt(request.session_key, request.reason)
        return gw_pb2.InterruptResponse(
            accepted=success,
            message="Interrupt sent" if success else "No active task to interrupt",
        )

    # ── Health ─────────────────────────────────────────────────

    async def Health(self, request, context) -> gw_pb2.HealthResponse:
        return gw_pb2.HealthResponse(
            healthy=True,
            version="0.1.0",
            active_sessions=self.session_mgr.count_active(),
            queued_messages=self.queue_mgr.total_queued(),
            connected_platforms=self._connected_platforms(),
        )

    def _connected_platforms(self) -> List[str]:
        return AdapterRegistry.list_running()


# ════════════════════════════════════════════════════════════════
# Agent Client — connects Wavegate to WW Core
# ════════════════════════════════════════════════════════════════

@dataclass
class AgentClient:
    """Wraps the gRPC Agent stub with connection management."""

    address: str
    _channel: Optional[grpc.aio.Channel] = None
    _stub: Optional[ag_grpc.AgentStub] = None

    async def connect(self):
        self._channel = grpc.aio.insecure_channel(self.address)
        self._stub = ag_grpc.AgentStub(self._channel)
        log.info("AgentClient connected to %s", self.address)

    async def close(self):
        if self._channel:
            await self._channel.close()

    @property
    def stub(self) -> ag_grpc.AgentStub:
        if self._stub is None:
            raise RuntimeError("AgentClient not connected. Call connect() first.")
        return self._stub

    async def run_task(self, msg: um_pb2.UnifiedMessage, max_spirals: int = 10):
        """Submit a task and collect the response stream."""
        request = ag_pb2.RunTaskRequest(
            session_key=msg.session_key,
            goal=msg.content.text.body if msg.content.HasField("text") else "",
            sender=msg.sender,
            max_spirals=max_spirals,
            platform=msg.platform,
        )
        responses = []
        async for resp in self._stub.RunTask(request):
            responses.append(resp)
        return responses

    async def run_goal(
        self, msg: um_pb2.UnifiedMessage, max_iterations: int = 20
    ) -> ag_pb2.RunGoalResponse:
        """Submit a goal for autonomous background execution."""
        request = ag_pb2.RunGoalRequest(
            session_key=msg.session_key,
            goal=msg.content.text.body if msg.content.HasField("text") else "",
            sender=msg.sender,
            max_iterations=max_iterations,
            config=ag_pb2.GoalConfig(explorer_count=3, critic_enabled=True, auto_fix=True),
        )
        return await self._stub.RunGoal(request)

    async def steer_task(self, session_key: str, context: str) -> ag_pb2.SteerTaskResponse:
        return await self._stub.SteerTask(ag_pb2.SteerTaskRequest(
            session_key=session_key, context=context,
        ))

    async def abort_task(self, session_key: str, reason: str) -> ag_pb2.AbortTaskResponse:
        return await self._stub.AbortTask(ag_pb2.AbortTaskRequest(
            session_key=session_key, reason=reason,
        ))


# ════════════════════════════════════════════════════════════════
# Wavegate Server
# ════════════════════════════════════════════════════════════════

@dataclass
class WavegateConfig:
    """Configuration for the Wavegate server."""

    agent_addr: str = "localhost:9301"  # WW Core gRPC address
    gateway_port: int = 9302            # Wavegate gRPC listen port
    max_workers: int = 10               # Thread pool size

    @classmethod
    def from_env(cls) -> "WavegateConfig":
        return cls(
            agent_addr=os.environ.get("WAVEGATE_AGENT_ADDR", "localhost:9301"),
            gateway_port=int(os.environ.get("WAVEGATE_PORT", "9302")),
            max_workers=int(os.environ.get("WAVEGATE_MAX_WORKERS", "10")),
        )


class WavegateServer:
    """The Wavegate daemon — Control Plane for Worldwave.

    Lifecycle:
        server = WavegateServer(config)
        await server.start()   # Starts gRPC server, connects to Agent, starts adapters
        # ... running ...
        await server.stop()    # Graceful shutdown
    """

    def __init__(self, config: WavegateConfig = None):
        self.config = config or WavegateConfig.from_env()
        self.session_mgr = SessionManager()
        self.queue_mgr = QueueManager()
        self.agent_client = AgentClient(address=self.config.agent_addr)
        self._server: Optional[grpc.aio.Server] = None
        self._running = False

    async def start(self):
        """Start the Wavegate server."""
        log.info("Wavegate v%s starting...", "0.1.0")

        # Connect to Agent Runtime
        await self.agent_client.connect()

        # Build gRPC server
        self._server = grpc.aio.server(
            futures.ThreadPoolExecutor(max_workers=self.config.max_workers),
        )
        gw_service = GatewayServiceImpl(
            session_mgr=self.session_mgr,
            queue_mgr=self.queue_mgr,
            agent_stub=self.agent_client.stub,
        )
        gw_grpc.add_GatewayServicer_to_server(gw_service, self._server)

        port = self._server.add_insecure_port(f"[::]:{self.config.gateway_port}")
        await self._server.start()
        log.info("Wavegate gRPC server listening on port %d", self.config.gateway_port)

        # Start platform adapters
        AdapterRegistry.start_all(
            on_message=self._on_adapter_message,
            session_mgr=self.session_mgr,
        )

        self._running = True
        log.info("Wavegate started. Platforms: %s", AdapterRegistry.list_running())

    async def stop(self):
        """Gracefully stop the Wavegate server."""
        log.info("Wavegate stopping...")
        self._running = False

        # Stop adapters
        AdapterRegistry.stop_all()

        # Drain queues
        self.queue_mgr.drain_all()

        # Stop gRPC server
        if self._server:
            await self._server.stop(grace=5)
            await self._server.wait_for_termination()

        # Disconnect from Agent
        await self.agent_client.close()

        log.info("Wavegate stopped")

    # ── Internal: adapter message callback ─────────────────────

    async def _on_adapter_message(self, unified_msg: um_pb2.UnifiedMessage):
        """Called by platform adapters when a message arrives."""
        session = self.session_mgr.get_or_create(
            unified_msg.session_key, unified_msg.sender, unified_msg.platform,
        )

        # Check command prefix for special routing
        cmd_prefix = ""
        if unified_msg.content.HasField("text"):
            cmd_prefix = unified_msg.content.text.command_prefix

        if cmd_prefix == "goal":
            # Goal Mode: autonomous background task
            log.info("Goal Mode task from %s: %s", unified_msg.sender.display_name,
                     unified_msg.content.text.body[:100])
            try:
                resp = await self.agent_client.run_goal(unified_msg)
                # Send ACK back via adapter
                AdapterRegistry.send_response(
                    unified_msg.platform, unified_msg.session_key,
                    f"Goal accepted: `{resp.task_id}`. Use `/status` to check progress.",
                )
            except Exception as e:
                log.error("Goal submission failed: %s", e)

        elif cmd_prefix == "status":
            # Query goal status
            state = self.queue_mgr.get_state(unified_msg.session_key)
            AdapterRegistry.send_response(
                unified_msg.platform, unified_msg.session_key,
                f"Status: {state.status}\nQueued: {state.queued_count}\nGoal: {state.current_goal}",
            )

        elif cmd_prefix == "stop":
            # Interrupt current task
            success = self.queue_mgr.interrupt(unified_msg.session_key, "User requested stop")
            AdapterRegistry.send_response(
                unified_msg.platform, unified_msg.session_key,
                "Task interrupted." if success else "No active task to stop.",
            )

        else:
            # Normal task: route through queue manager
            self.queue_mgr.enqueue(
                unified_msg.session_key, unified_msg,
                hints=unified_msg.routing,
            )
            # Trigger processing
            asyncio.create_task(self._process_queue(unified_msg.session_key))

    async def _process_queue(self, session_key: str):
        """Process queued messages for a session."""
        msg = self.queue_mgr.dequeue(session_key)
        if msg is None:
            return

        try:
            responses = await self.agent_client.run_task(msg)
            for resp in responses:
                self.queue_mgr.enqueue_response(session_key, resp)
                if resp.is_final and resp.payload.HasField("text"):
                    AdapterRegistry.send_response(
                        msg.platform, session_key, resp.payload.text,
                    )
                elif resp.payload.HasField("stream_chunk"):
                    # Streaming: adapter handles incremental updates
                    AdapterRegistry.send_stream_chunk(
                        msg.platform, session_key, resp.payload.stream_chunk,
                    )
        except grpc.RpcError as e:
            log.error("Agent RPC error for session %s: %s", session_key, e)
            AdapterRegistry.send_response(
                msg.platform, session_key, f"Error: {e.details()}",
            )
        except Exception as e:
            log.error("Task processing error: %s", e)


# ════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════

async def main():
    """CLI entry point for gateway-server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config = WavegateConfig.from_env()
    server = WavegateServer(config)

    # Handle signals for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))

    await server.start()

    # Keep running until stopped
    try:
        while server._running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
