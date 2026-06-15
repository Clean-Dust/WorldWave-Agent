"""Wavegate v1 proto re-exports.

Import this module to access all proto-generated types for Wavegate v1.
"""

from proto.wavegate.v1.unified_message_pb2 import (  # noqa: F401
    UnifiedMessage, Sender, Content, TextContent, MediaContent,
    ToolResult, Command, RoutingHints,
    AgentResponse, ResponsePayload, StreamChunk, ToolCall,
    StatusUpdate, ErrorInfo,
)
from proto.wavegate.v1.unified_message_pb2_grpc import *  # noqa: F401, F403

from proto.wavegate.v1.gateway_pb2 import (  # noqa: F401
    IngestRequest, IngestResponse,
    QueueStateRequest, QueueStateResponse,
    InterruptRequest, InterruptResponse,
    HealthResponse,
)
from proto.wavegate.v1.gateway_pb2_grpc import *  # noqa: F401, F403

from proto.wavegate.v1.agent_pb2 import (  # noqa: F401
    RunTaskRequest, RunGoalRequest, RunGoalResponse, GoalConfig,
    WatchGoalRequest, CancelGoalRequest, CancelGoalResponse,
    SteerTaskRequest, SteerTaskResponse,
    AbortTaskRequest, AbortTaskResponse,
    AgentInfoResponse,
)
from proto.wavegate.v1.agent_pb2_grpc import *  # noqa: F401, F403
