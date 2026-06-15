"""Wavegate v1 — WW Control Plane

Wavegate is Worldwave's independent multi-platform messaging gateway.
It handles all external communication (Telegram, Discord, etc.) and
routes normalized messages to the WW Agent Runtime via gRPC.

Architecture:
    External Platforms → Adapters → UnifiedMessage → gRPC → Agent Runtime
                                                              ↓
    External Platforms ← Adapters ← AgentResponse ← gRPC ←───┘
"""

from pathlib import Path

# Re-export proto-generated types for convenience
from proto.wavegate.v1.unified_message_pb2 import (
    UnifiedMessage,
    Sender,
    Content,
    TextContent,
    MediaContent,
    ToolResult,
    Command,
    RoutingHints,
    AgentResponse,
    ResponsePayload,
    StreamChunk,
    ToolCall,
    StatusUpdate,
    ErrorInfo,
)
from proto.wavegate.v1.gateway_pb2 import (
    IngestRequest,
    IngestResponse,
    QueueStateRequest,
    QueueStateResponse,
    InterruptRequest,
    InterruptResponse,
    HealthResponse,
)
from proto.wavegate.v1.agent_pb2 import (
    RunTaskRequest,
    RunGoalRequest,
    RunGoalResponse,
    GoalConfig,
    WatchGoalRequest,
    CancelGoalRequest,
    CancelGoalResponse,
    SteerTaskRequest,
    SteerTaskResponse,
    AbortTaskRequest,
    AbortTaskResponse,
    AgentInfoResponse,
)

# Wavegate modules
from gateway.goal import GoalRunner, GoalRun, GoalPhase, GoalCallback
from gateway.pairing import PairingManager
from gateway.tenant import TenantManager

# Bridge: server.py compat (wraps TelegramAdapter)
from gateway.bridge import GatewayManager, TelegramGateway

__version__ = "0.1.0"
