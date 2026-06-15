"""Wavegate Queue Manager.

Implements the four queue modes from the blueprint:
- Steer: inject new context into current reasoning
- Followup: FIFO queue after current task
- Collect: debounce window, merge fragments
- Interrupt: abort current and start fresh

Backends:
- In-memory (default, zero dependencies)
- NATS JetStream (persistent, at-least-once delivery)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger("gateway.queue")


class QueueMode(Enum):
    STEER = "steer"          # Inject into current reasoning
    FOLLOWUP = "followup"    # FIFO after current task
    COLLECT = "collect"      # Debounce and merge fragments
    INTERRUPT = "interrupt"  # Abort current, start fresh


@dataclass
class QueueState:
    """Snapshot of a session's queue state."""
    session_key: str = ""
    mode: QueueMode = QueueMode.STEER
    queued_count: int = 0
    status: str = "idle"  # idle, processing, waiting_approval
    current_goal: str = ""
    _queue: deque = field(default_factory=deque)
    _response_queue: deque = field(default_factory=deque)
    _current_msg: Any = None
    _collect_buffer: List[Any] = field(default_factory=list)
    _collect_deadline: float = 0.0
    _interrupt_pending: bool = False

    COLLECT_WINDOW = 2.5  # seconds


class QueueManager:
    """Manages per-session message queues with four queue modes.

    Supports two backends:
    - In-memory (default): fast, ephemeral, lost on restart
    - NATS JetStream: persistent, at-least-once delivery, survives restarts

    Usage with NATS:
        nats = NatsLayer()
        await nats.connect()
        qm = QueueManager(nats=nats)
        qm.enqueue("sess1", msg)  # Persisted to JetStream
    """

    def __init__(self, nats=None):
        self._sessions: Dict[str, QueueState] = {}
        self._lock = asyncio.Lock()
        self._nats = nats  # Optional NatsLayer for persistence

    def get_state(self, session_key: str) -> QueueState:
        """Get or create queue state for a session."""
        if session_key not in self._sessions:
            self._sessions[session_key] = QueueState(session_key=session_key)
        state = self._sessions[session_key]
        # Refresh status
        if state._current_msg is not None and not state._interrupt_pending:
            state.status = "processing"
        elif state.queued_count > 0:
            state.status = "queued"
        else:
            state.status = "idle"
        return state

    def enqueue(self, session_key: str, msg, hints=None) -> int:
        """Enqueue a message with queue mode routing.

        Returns the queue position (0 = processing now, 1+ = queued).
        """
        state = self.get_state(session_key)

        # Determine mode from hints or default
        mode = QueueMode.STEER
        if hints:
            if hints.queue_mode == "followup":
                mode = QueueMode.FOLLOWUP
            elif hints.queue_mode == "collect":
                mode = QueueMode.COLLECT
            elif hints.queue_mode == "interrupt":
                mode = QueueMode.INTERRUPT

        if mode == QueueMode.INTERRUPT:
            state._interrupt_pending = True
            state._queue.clear()
            state._queue.appendleft(msg)
            state.queued_count = 1
            log.info("Queue: INTERRUPT session=%s", session_key)
            # Persist interrupt signal via NATS control channel
            if self._nats and self._nats.is_connected:
                asyncio.create_task(self._nats.send_interrupt(session_key, "interrupt mode"))
            return 0

        elif mode == QueueMode.COLLECT:
            now = time.time()
            if now >= state._collect_deadline:
                if state._collect_buffer:
                    merged = self._merge_collect(state._collect_buffer)
                    state._queue.append(merged)
                    # Persist merged message
                    self._persist_task(session_key, merged)
                state._collect_buffer = [msg]
                state._collect_deadline = now + QueueState.COLLECT_WINDOW
                state.queued_count = len(state._queue) + 1
            else:
                state._collect_buffer.append(msg)
            log.info("Queue: COLLECT session=%s buffer=%d", session_key, len(state._collect_buffer))
            return len(state._queue)

        elif mode == QueueMode.FOLLOWUP:
            state._queue.append(msg)
            state.queued_count = len(state._queue)
            self._persist_task(session_key, msg)
            log.info("Queue: FOLLOWUP session=%s pos=%d", session_key, state.queued_count)
            return state.queued_count

        else:  # STEER (default)
            if state._current_msg is not None:
                state._queue.appendleft(msg)
                log.info("Queue: STEER inject session=%s", session_key)
            else:
                state._queue.append(msg)
            state.queued_count = len(state._queue)
            self._persist_task(session_key, msg)
            return state.queued_count

    def _persist_task(self, session_key: str, msg):
        """Persist a task to NATS JetStream if available."""
        if self._nats and self._nats.is_connected:
            try:
                # Serialize the protobuf message to bytes
                payload = msg.SerializeToString()
                asyncio.create_task(self._nats.push_task(session_key, payload))
            except Exception as e:
                log.debug("NATS persist failed (non-fatal): %s", e)

    def enqueue_immediate(self, session_key: str, msg):
        """Bypass queue entirely — process immediately."""
        state = self.get_state(session_key)
        state._queue.appendleft(msg)
        state.queued_count = 1

    def dequeue(self, session_key: str):
        """Get the next message to process. Flushes collect buffers if expired."""
        state = self.get_state(session_key)

        # Flush collect buffer if deadline passed
        if state._collect_buffer and time.time() >= state._collect_deadline:
            merged = self._merge_collect(state._collect_buffer)
            state._queue.append(merged)
            state._collect_buffer = []
            state.queued_count = len(state._queue)

        if not state._queue:
            return None

        msg = state._queue.popleft()
        state._current_msg = msg
        state.queued_count = len(state._queue)
        state.status = "processing"
        return msg

    def enqueue_response(self, session_key: str, response):
        """Store a response for streaming back to the adapter."""
        state = self.get_state(session_key)
        state._response_queue.append(response)

    def drain_responses(self, session_key: str) -> list:
        """Drain pending responses for a session."""
        state = self.get_state(session_key)
        responses = list(state._response_queue)
        state._response_queue.clear()

        # If final response, clear current message
        if responses and responses[-1].is_final:
            state._current_msg = None
            state.status = "idle"
            state._interrupt_pending = False

        return responses

    def interrupt(self, session_key: str, reason: str = "") -> bool:
        """Send an interrupt signal for a session."""
        state = self.get_state(session_key)
        if state._current_msg is not None:
            state._interrupt_pending = True
            state._queue.clear()
            state._current_msg = None
            state.status = "idle"
            log.info("Queue: interrupted session=%s reason=%s", session_key, reason)
            return True
        return False

    def drain_all(self):
        """Clear all queues (used during shutdown)."""
        for state in self._sessions.values():
            state._queue.clear()
            state._response_queue.clear()
            state._collect_buffer.clear()

    def total_queued(self) -> int:
        return sum(s.queued_count for s in self._sessions.values())

    def _merge_collect(self, messages: list):
        """Merge a collect buffer into a single message."""
        if len(messages) == 1:
            return messages[0]
        # Merge text content
        primary = messages[0]
        texts = []
        for m in messages:
            if m.content.HasField("text"):
                texts.append(m.content.text.body)
        if texts and primary.content.HasField("text"):
            primary.content.text.body = "\n".join(texts)
        return primary
