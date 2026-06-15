"""ww/contacts/protocol.py — Agent Communication Protocol

Standard message format for agent-to-agent communication over any transport.
Based on JSON-RPC 2.0 with extensions for agent-specific semantics.

Message types:
- request:  A question, task, or action invocation
- response: Reply to a request (success or error)
- notify:   One-way notification (no response expected)

Fields:
- from_did:     Sender's DID
- to_did:       Recipient's DID (or empty for broadcast)
- intent:       High-level purpose (chat, task, query, forward)
- body:         The payload (text, structured data, or tool call)
- id:           Request ID for correlating responses
- timestamp:    ISO 8601 UTC
- signature:    Sender's signature for verification
"""

from __future__ import annotations
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

# ── Intent types ──

INTENT_CHAT = "chat"            # Free-form conversation
INTENT_TASK = "task"            # Delegate a task to execute
INTENT_QUERY = "query"          # Request information
INTENT_STATUS = "status"        # Query or report agent status
INTENT_FORWARD = "forward"      # Forward message to another agent
INTENT_HANDSHAKE = "handshake"  # Friendship handshake protocol
INTENT_PING = "ping"            # Keep-alive / liveness check
INTENT_ERROR = "error"          # Error notification

VALID_INTENTS = {
    INTENT_CHAT, INTENT_TASK, INTENT_QUERY, INTENT_STATUS,
    INTENT_FORWARD, INTENT_HANDSHAKE, INTENT_PING, INTENT_ERROR,
}


# ── AgentMessage ──


class AgentMessage:
    """A structured message between two WW agents."""

    def __init__(
        self,
        from_did: str,
        to_did: str = "",
        intent: str = INTENT_CHAT,
        body: Any = None,
        msg_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        timestamp: Optional[str] = None,
        signature: str = "",
        metadata: Optional[Dict] = None,
    ):
        self.from_did = from_did
        self.to_did = to_did
        self.intent = intent if intent in VALID_INTENTS else INTENT_CHAT
        self.body = body or ""
        self.msg_id = msg_id or uuid.uuid4().hex[:16]
        self.in_reply_to = in_reply_to
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        self.signature = signature
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "from_did": self.from_did,
            "to_did": self.to_did,
            "intent": self.intent,
            "body": self.body,
            "id": self.msg_id,
            "in_reply_to": self.in_reply_to,
            "timestamp": self.timestamp,
            "signature": self.signature,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentMessage":
        return cls(
            from_did=data.get("from_did", ""),
            to_did=data.get("to_did", ""),
            intent=data.get("intent", INTENT_CHAT),
            body=data.get("body", ""),
            msg_id=data.get("id"),
            in_reply_to=data.get("in_reply_to"),
            timestamp=data.get("timestamp"),
            signature=data.get("signature", ""),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, raw: str) -> "AgentMessage":
        return cls.from_dict(json.loads(raw))

    # ── Convenience constructors ──

    @classmethod
    def handshake_request(cls, from_did: str, to_did: str, public_key_hex: str) -> "AgentMessage":
        """Create a friendship invitation."""
        return cls(
            from_did=from_did,
            to_did=to_did,
            intent=INTENT_HANDSHAKE,
            body={
                "action": "request",
                "public_key_hex": public_key_hex,
                "agent_info": {},
            },
        )

    @classmethod
    def handshake_accept(cls, from_did: str, to_did: str, public_key_hex: str) -> "AgentMessage":
        """Accept a friendship invitation."""
        return cls(
            from_did=from_did,
            to_did=to_did,
            intent=INTENT_HANDSHAKE,
            body={
                "action": "accept",
                "public_key_hex": public_key_hex,
                "agent_info": {},
            },
        )

    @classmethod
    def handshake_reject(cls, from_did: str, to_did: str, reason: str = "") -> "AgentMessage":
        """Reject a friendship invitation."""
        return cls(
            from_did=from_did,
            to_did=to_did,
            intent=INTENT_HANDSHAKE,
            body={"action": "reject", "reason": reason or "Declined"},
        )

    @classmethod
    def task(cls, from_did: str, to_did: str, task_description: str) -> "AgentMessage":
        """Delegate a task."""
        return cls(
            from_did=from_did,
            to_did=to_did,
            intent=INTENT_TASK,
            body={"description": task_description, "status": "pending"},
        )

    @classmethod
    def ping(cls, from_did: str, to_did: str = "") -> "AgentMessage":
        """Ping (keep-alive)."""
        return cls(from_did=from_did, to_did=to_did, intent=INTENT_PING, body={"ts": time.time()})


# ── Message builder helpers ──


def sign_message(
    msg: AgentMessage, sign_fn, public_key_hex: str
) -> AgentMessage:
    """Sign a message using the agent's identity.

    Args:
        msg: The message to sign
        sign_fn: Function that signs bytes and returns bytes
        public_key_hex: The agent's public key (added to metadata)

    Returns:
        The same message with signature field populated
    """
    body_bytes = json.dumps(msg.body, ensure_ascii=False).encode() if not isinstance(msg.body, str) else msg.body.encode()
    to_sign = f"{msg.msg_id}:{msg.intent}:{msg.timestamp}".encode() + body_bytes
    sig = sign_fn(to_sign)
    if sig:
        msg.signature = sig.hex()
    msg.metadata["pub_key"] = public_key_hex
    return msg


def verify_message(msg: AgentMessage, verify_fn) -> bool:
    """Verify a signed message.

    Args:
        msg: The message to verify
        verify_fn: Function that takes (message_bytes, signature_hex, public_key_hex) and returns bool

    Returns:
        True if signature is valid
    """
    if not msg.signature:
        return False
    pub_key_hex = msg.metadata.get("pub_key", "")
    if not pub_key_hex:
        return False
    body_bytes = (
        json.dumps(msg.body, ensure_ascii=False).encode()
        if not isinstance(msg.body, str)
        else msg.body.encode()
    )
    signed_content = f"{msg.msg_id}:{msg.intent}:{msg.timestamp}".encode() + body_bytes
    return verify_fn(signed_content, msg.signature, pub_key_hex)
