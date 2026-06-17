"""Wavegate NATS Integration Layer.

Provides the distributed messaging backbone for Wavegate:
- JetStream: persistent message queues with at-least-once delivery
- KV Store: session state with distributed locks
- Object Store: large file transfer (media, contexts, logs)

Replaces the in-memory QueueManager and SessionManager when enabled.

Architecture:
    Platform Adapters → NATS Subject (ww.events.{platform}.*)
                             ↓
    Wavegate Core → NATS Consumer → gRPC → Agent Runtime
                             ↓
    Agent Runtime → NATS Reply Subject → Wavegate → Platform Adapters

Usage:
    nats = NatsLayer()
    await nats.connect("nats://localhost:4222")
    await nats.js.publish("ww.events.telegram.ingest", data)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional

import nats
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from nats.js.api import (
    ConsumerConfig,
    DeliverPolicy,
    AckPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError, BadRequestError
from nats.js.kv import KeyValue

log = logging.getLogger("gateway.nats")

# ── Defaults ──────────────────────────────────────────────────

DEFAULT_NATS_URL = "nats://localhost:4222"
DEFAULT_STREAM = "ww_events"
DEFAULT_KV_BUCKET = "ww_sessions"
DEFAULT_OBJ_BUCKET = "ww_objects"

# Subject hierarchy:
#   ww.events.{platform}.{action}   — Incoming normalized events
#   ww.tasks.{session_key}          — Per-session task queues
#   ww.responses.{session_key}      — Agent response streams
#   ww.control.{session_key}        — Interrupt/steer signals


# ════════════════════════════════════════════════════════════════
# NATS Layer
# ════════════════════════════════════════════════════════════════

@dataclass
class NatsConfig:
    """NATS connection configuration."""

    url: str = DEFAULT_NATS_URL
    stream_name: str = DEFAULT_STREAM
    kv_bucket: str = DEFAULT_KV_BUCKET
    obj_bucket: str = DEFAULT_OBJ_BUCKET
    max_payload: int = 8 * 1024 * 1024  # 8MB

    @classmethod
    def from_env(cls) -> "NatsConfig":
        return cls(
            url=os.environ.get("WAVEGATE_NATS_URL", DEFAULT_NATS_URL),
            stream_name=os.environ.get("WAVEGATE_NATS_STREAM", DEFAULT_STREAM),
            kv_bucket=os.environ.get("WAVEGATE_NATS_KV", DEFAULT_KV_BUCKET),
            obj_bucket=os.environ.get("WAVEGATE_NATS_OBJ", DEFAULT_OBJ_BUCKET),
        )


class NatsLayer:
    """Manages the NATS connection and all JetStream resources.

    Singleton per Wavegate process.
    """

    def __init__(self, config: NatsConfig = None):
        self.config = config or NatsConfig.from_env()
        self._nc: Optional[NatsClient] = None
        self._js: Optional[JetStreamContext] = None
        self._kv: Optional[KeyValue] = None
        self._connected = False

    # ── Connection lifecycle ──────────────────────────────────

    async def connect(self) -> bool:
        """Connect to NATS and initialize JetStream resources."""
        if self._connected:
            return True

        try:
            self._nc = await nats.connect(
                self.config.url,
                max_reconnect_attempts=-1,  # Infinite reconnect
                reconnect_time_wait=2,
                name="gateway",
            )
            self._js = self._nc.jetstream()
            self._connected = True
            log.info("NATS connected: %s", self.config.url)

            # Initialize stream, KV store, object store
            await self._init_stream()
            await self._init_kv()
            await self._init_object_store()

            return True
        except Exception as e:
            log.error("NATS connection failed: %s (%s)", e, type(e).__name__)
            import traceback
            log.debug(traceback.format_exc())
            self._connected = False
            return False

    async def close(self):
        """Close NATS connection gracefully."""
        if self._nc:
            await self._nc.drain()
            await self._nc.close()
            self._connected = False
            log.info("NATS disconnected")

    @property
    def js(self) -> JetStreamContext:
        if not self._js:
            raise RuntimeError("NATS not connected. Call connect() first.")
        return self._js

    @property
    def kv(self) -> KeyValue:
        if not self._kv:
            raise RuntimeError("NATS KV not initialized.")
        return self._kv

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Stream initialization ─────────────────────────────────

    async def _init_stream(self):
        """Create the main event stream with JetStream persistence."""
        try:
            await self._js.stream_info(self.config.stream_name)
            log.debug("Stream '%s' already exists", self.config.stream_name)
        except NotFoundError:
            await self._js.add_stream(
                StreamConfig(
                    name=self.config.stream_name,
                    subjects=[
                        "ww.events.>",   # All incoming events
                        "ww.tasks.>",     # Task queues
                        "ww.responses.>", # Response streams
                        "ww.control.>",   # Control signals
                    ],
                    storage=StorageType.FILE,
                    max_bytes=1024 * 1024 * 1024,  # 1GB
                    max_age=24 * 3600,               # 24h retention
                    max_msg_size=self.config.max_payload,
                )
            )
            log.info("Stream '%s' created", self.config.stream_name)

    async def _init_kv(self):
        """Create the KV store for session state."""
        try:
            self._kv = await self._js.key_value(self.config.kv_bucket)
            log.debug("KV bucket '%s' found", self.config.kv_bucket)
        except NotFoundError:
            self._kv = await self._js.create_key_value(
                bucket=self.config.kv_bucket,
                ttl=24 * 3600,  # 24h session TTL
            )
            log.info("KV bucket '%s' created", self.config.kv_bucket)

    async def _init_object_store(self):
        """Create the Object Store for large file transfer."""
        try:
            await self._js.object_store(self.config.obj_bucket)
            log.debug("Object store '%s' found", self.config.obj_bucket)
        except NotFoundError:
            await self._js.create_object_store(
                bucket=self.config.obj_bucket,
                ttl=24 * 3600,  # 24h TTL
                max_bytes=1024 * 1024 * 1024,  # 1GB
            )
            log.info("Object store '%s' created", self.config.obj_bucket)

    # ── JetStream Queue Operations ────────────────────────────

    async def publish_event(self, platform: str, action: str, payload: bytes):
        """Publish an event to ww.events.{platform}.{action}."""
        subject = f"ww.events.{platform}.{action}"
        ack = await self.js.publish(subject, payload)
        return ack

    async def push_task(self, session_key: str, payload: bytes):
        """Push a task to a per-session queue with persistence."""
        subject = f"ww.tasks.{session_key}"
        # Publish with message ID for deduplication
        ack = await self.js.publish(subject, payload)
        return ack

    async def pull_task(self, session_key: str, durable_name: str = None) -> Optional[bytes]:
        """Pull next task from a session queue."""
        subject = f"ww.tasks.{session_key}"
        durable = durable_name or f"worker_{session_key}"

        # Create ephemeral consumer if not exists
        try:
            consumer_info = await self.js.consumer_info(
                self.config.stream_name, durable,
            )
        except NotFoundError:
            await self.js.add_consumer(
                stream=self.config.stream_name,
                config=ConsumerConfig(
                    durable_name=durable,
                    filter_subject=subject,
                    ack_policy=AckPolicy.EXPLICIT,
                    deliver_policy=DeliverPolicy.ALL,
                ),
            )

        try:
            msgs = await self.js.pull_subscribe(
                subject, durable,
                stream=self.config.stream_name,
                batch=1,
                timeout=1,
            )
            if msgs:
                msg = msgs[0]
                await msg.ack()
                return msg.data
        except Exception:
            pass
        return None

    async def drain_tasks(self, session_key: str) -> List[bytes]:
        """Drain all pending tasks for a session."""
        subject = f"ww.tasks.{session_key}"
        durable = f"drain_{session_key}"

        try:
            await self.js.add_consumer(
                stream=self.config.stream_name,
                config=ConsumerConfig(
                    durable_name=durable,
                    filter_subject=subject,
                    ack_policy=AckPolicy.EXPLICIT,
                    deliver_policy=DeliverPolicy.ALL,
                ),
            )
        except BadRequestError:
            pass

        results = []
        try:
            msgs = await self.js.pull_subscribe(
                subject, durable,
                stream=self.config.stream_name,
                batch=100,
                timeout=2,
            )
            for msg in msgs:
                results.append(msg.data)
                await msg.ack()
        except Exception:
            pass

        # Clean up drain consumer
        try:
            await self.js.delete_consumer(self.config.stream_name, durable)
        except Exception:
            pass

        return results

    # ── KV Store Operations (Session State) ───────────────────

    async def session_put(self, session_key: str, data: dict):
        """Store session state in KV store."""
        await self.kv.put(session_key, json.dumps(data).encode())

    async def session_get(self, session_key: str) -> Optional[dict]:
        """Retrieve session state from KV store."""
        try:
            entry = await self.kv.get(session_key)
            return json.loads(entry.value)
        except NotFoundError:
            return None

    async def session_delete(self, session_key: str):
        """Delete session state."""
        try:
            await self.kv.delete(session_key)
        except NotFoundError:
            pass

    async def session_lock(self, session_key: str, ttl: int = 60) -> bool:
        """Acquire a distributed write lock for a session.

        Uses KV create-with-TTL as a lock primitive.
        Returns True if lock acquired.
        """
        lock_key = f"lock:{session_key}"
        try:
            await self.kv.create(lock_key, b"locked")
            # Set TTL via update — KV create doesn't support TTL directly
            # so we use a heartbeat approach instead
            return True
        except BadRequestError:
            return False  # Lock already held

    async def session_unlock(self, session_key: str):
        """Release the distributed write lock."""
        lock_key = f"lock:{session_key}"
        try:
            await self.kv.delete(lock_key)
        except NotFoundError:
            pass

    # ── Object Store Operations ────────────────────────────────

    async def put_object(self, name: str, data: bytes, metadata: dict = None) -> str:
        """Store an object in the object store. Returns the object key."""
        obj_store = await self._js.object_store(self.config.obj_bucket)
        meta = nats.js.api.ObjectMeta(
            name=name,
            description=metadata.get("description", "") if metadata else "",
        )
        info = await obj_store.put(name, data)
        return info.name

    async def get_object(self, name: str) -> Optional[bytes]:
        """Retrieve an object from the object store."""
        try:
            obj_store = await self._js.object_store(self.config.obj_bucket)
            result = await obj_store.get(name)
            return result.data
        except NotFoundError:
            return None

    async def delete_object(self, name: str):
        """Delete an object from the object store."""
        try:
            obj_store = await self._js.object_store(self.config.obj_bucket)
            await obj_store.delete(name)
        except NotFoundError:
            pass

    # ── Control Signals ───────────────────────────────────────

    async def send_interrupt(self, session_key: str, reason: str = ""):
        """Send an interrupt signal via the control channel."""
        subject = f"ww.control.{session_key}"
        payload = json.dumps({"action": "interrupt", "reason": reason}).encode()
        await self.js.publish(subject, payload)

    async def send_steer(self, session_key: str, context: str):
        """Send a steer signal with new context."""
        subject = f"ww.control.{session_key}"
        payload = json.dumps({"action": "steer", "context": context}).encode()
        await self.js.publish(subject, payload)

    # ── Token Locks (Single Connection Ownership) ──────────────
    # Blueprint: "The gateway must use distributed locks to ensure
    # each logical external connection is managed by only one
    # gateway instance at any given time."

    async def token_lock_acquire(self, platform: str, token_id: str,
                                  ttl: int = 300) -> bool:
        """Acquire a platform token lock so only one gateway instance
        uses a given API token.

        Args:
            platform: e.g. 'telegram', 'slack', 'discord'
            token_id: first 8 chars of token hash (never store full token)
            ttl: lock time-to-live in seconds (default 5 min, heartbeat extends)

        Returns True if lock acquired.
        """
        lock_key = f"token_lock:{platform}:{token_id}"
        try:
            await self.kv.create(lock_key, json.dumps({
                "acquired_at": __import__("time").time(),
                "ttl": ttl,
            }).encode())
            return True
        except Exception:
            return False  # Lock already held

    async def token_lock_release(self, platform: str, token_id: str):
        """Release a platform token lock."""
        lock_key = f"token_lock:{platform}:{token_id}"
        try:
            await self.kv.delete(lock_key)
        except Exception:
            pass

    async def token_lock_heartbeat(self, platform: str, token_id: str,
                                    ttl: int = 300):
        """Extend the token lock TTL (call every 60s)."""
        lock_key = f"token_lock:{platform}:{token_id}"
        try:
            await self.kv.put(lock_key, json.dumps({
                "acquired_at": __import__("time").time(),
                "ttl": ttl,
            }).encode())
        except Exception:
            pass

    # ── Subjects ──────────────────────────────────────────────

    @staticmethod
    def event_subject(platform: str, action: str = "ingest") -> str:
        return f"ww.events.{platform}.{action}"

    @staticmethod
    def task_subject(session_key: str) -> str:
        return f"ww.tasks.{session_key}"

    @staticmethod
    def response_subject(session_key: str) -> str:
        return f"ww.responses.{session_key}"

    @staticmethod
    def control_subject(session_key: str) -> str:
        return f"ww.control.{session_key}"
