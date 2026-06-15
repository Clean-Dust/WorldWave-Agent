"""Discord Platform Adapter for WW Gateway.

Blueprint ref:
  "Discord / WebSocket — high-concurrency WS server with heartbeat
   and JSON Schema validation to maintain connection liveness and security."

Architecture:
  - Connects to Discord Gateway WebSocket v10 for receiving events
  - Uses Discord REST API (httpx) for sending messages
  - Handles: MESSAGE_CREATE, INTERACTION_CREATE (slash commands)
  - Heartbeat every heartbeat_interval ms, reconnect with resume
  - Auto-registers if DISCORD_BOT_TOKEN env var is set
  - Supports HITL via Discord message components (buttons)
  - Streaming via message editing (editMessage)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Callable, Optional

import httpx

from google.protobuf.timestamp_pb2 import Timestamp

from proto.wavegate.v1.unified_message_pb2 import (
    UnifiedMessage, Sender, Content, TextContent, RoutingHints,
)
from gateway.adapters import BaseAdapter, AdapterRegistry

log = logging.getLogger("gateway.discord")

DISCORD_API = "https://discord.com/api/v10"
DISCORD_GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"

# Gateway opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Intents: GUILDS + GUILD_MESSAGES + MESSAGE_CONTENT
DEFAULT_INTENTS = (1 << 0) | (1 << 9) | (1 << 15)


class DiscordAdapter(BaseAdapter):
    """Discord platform adapter for WW Gateway.

    Features:
    - WebSocket gateway with heartbeat + resume
    - REST API for outbound messages
    - @mention detection and slash command support
    - HITL via message components (buttons)
    - Streaming via editMessage
    - Whitelist-aware: drops unauthorized users silently
    """

    platform = "discord"

    def __init__(
        self,
        token: str = "",
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._token = token or os.environ.get("DISCORD_BOT_TOKEN", "")
        self._on_message = on_message
        self._session_mgr = session_mgr
        self._whitelist = whitelist or set()

        self._running = False
        self._gateway_task: Optional[asyncio.Task] = None
        self._ws = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._sequence: Optional[int] = None
        self._session_id: Optional[str] = None
        self._bot_user_id: str = ""
        self._heartbeat_interval: float = 41.25
        self._http: Optional[httpx.AsyncClient] = None

        # Streaming state
        self._streaming: dict = {}

    # ── Adapter interface ──────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        if not self._token:
            log.info("Discord adapter: no token, skipping")
            return

        self._http = httpx.AsyncClient(
            base_url=DISCORD_API,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        )
        self._gateway_task = asyncio.ensure_future(self._gateway_loop())
        self._running = True
        log.info("Discord adapter started")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._gateway_task:
            self._gateway_task.cancel()
        if self._ws:
            try:
                asyncio.ensure_future(self._ws.close())
            except Exception:
                pass
        if self._http:
            try:
                asyncio.ensure_future(self._http.aclose())
            except Exception:
                pass
        log.info("Discord adapter stopped")

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        if not self._http:
            return False
        try:
            payload = {"content": text[:2000]}
            components = kwargs.get("components")
            if components:
                payload["components"] = components

            async def _send():
                resp = await self._http.post(
                    f"/channels/{chat_id}/messages", json=payload
                )
                return resp.status_code == 200

            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_send(), loop)
                return future.result(timeout=10)
            return loop.run_until_complete(_send())
        except RuntimeError:
            return asyncio.run(_send())
        except Exception as e:
            log.debug("Discord send failed: %s", e)
            return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        text = chunk if isinstance(chunk, str) else str(chunk)
        stream = self._streaming.get(chat_id)
        now = time.time()

        if stream and stream.get("message_id"):
            stream["buffer"] = (stream.get("buffer", "") + text)[:1990]
            if now - stream["last_edit"] < 1.8:
                return True
            return self._edit_stream_message(chat_id)
        else:
            self._streaming[chat_id] = {"buffer": text[:1990], "last_edit": now}
            return self.send_message(chat_id, text[:1990])

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if token:
            adapter = cls(
                token=token, on_message=on_message,
                session_mgr=session_mgr, whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    # ── Gateway loop ───────────────────────────────────────────

    async def _gateway_loop(self):
        try:
            import websockets
        except ImportError:
            log.warning("Discord adapter: websockets not installed. "
                        "Run: pip install worldwave[websockets]")
            self._running = False
            return

        backoff = 1
        should_resume = bool(self._session_id)

        while self._running:
            try:
                async with websockets.connect(DISCORD_GATEWAY, max_size=2**22) as ws:
                    self._ws = ws

                    hello = json.loads(await ws.recv())
                    if hello.get("op") == OP_HELLO:
                        self._heartbeat_interval = (
                            hello["d"]["heartbeat_interval"] / 1000.0
                        )
                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat(ws)
                        )

                    if should_resume and self._session_id:
                        await ws.send(json.dumps({
                            "op": OP_RESUME,
                            "d": {
                                "token": self._token,
                                "session_id": self._session_id,
                                "seq": self._sequence,
                            },
                        }))
                    else:
                        await ws.send(json.dumps({
                            "op": OP_IDENTIFY,
                            "d": {
                                "token": self._token,
                                "intents": DEFAULT_INTENTS,
                                "properties": {
                                    "os": "linux",
                                    "browser": "worldwave",
                                    "device": "worldwave",
                                },
                            },
                        }))

                    await self._process_events(ws)
                    should_resume = True

            except websockets.ConnectionClosed as e:
                log.warning("Discord gateway closed: %s (code %s)", e.reason, e.code)
                if e.code in (4004, 4010, 4011, 4012, 4013, 4014):
                    log.error("Discord: non-recoverable close %s, stopping", e.code)
                    self._running = False
                    return
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
            except Exception as e:
                log.warning("Discord gateway error: %s", e)
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                should_resume = True

    async def _heartbeat(self, ws):
        interval = self._heartbeat_interval * 0.75
        while self._running:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._sequence}))
            except Exception:
                break

    async def _process_events(self, ws):
        async for message in ws:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            op = data.get("op")
            seq = data.get("s")
            if seq is not None:
                self._sequence = seq

            if op == OP_DISPATCH:
                event_type = data.get("t", "")
                await self._handle_dispatch(event_type, data.get("d", {}))
            elif op == OP_HEARTBEAT:
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._sequence}))
            elif op == OP_RECONNECT:
                return
            elif op == OP_INVALID_SESSION:
                if not data.get("d", False):
                    self._session_id = None
                    self._sequence = None
                return

    async def _handle_dispatch(self, event_type: str, data: dict):
        if event_type == "READY":
            self._session_id = data.get("session_id", "")
            user = data.get("user", {})
            self._bot_user_id = user.get("id", "")
            log.info("Discord ready: %s", user.get("username", "?"))
        elif event_type == "RESUMED":
            log.info("Discord session resumed")
        elif event_type == "MESSAGE_CREATE":
            await self._handle_message(data)
        elif event_type == "INTERACTION_CREATE":
            await self._handle_interaction(data)

    async def _handle_message(self, data: dict):
        author = data.get("author", {})
        if author.get("bot", False):
            return

        user_id = author.get("id", "")
        channel_id = data.get("channel_id", "")
        content = data.get("content", "")
        guild_id = data.get("guild_id", "")

        if self._whitelist and user_id not in self._whitelist:
            log.debug("Discord: non-whitelisted user %s dropped", user_id)
            return

        bot_mention = f"<@{self._bot_user_id}>"
        bot_mention_nick = f"<@!{self._bot_user_id}>"
        cleaned = content.replace(bot_mention, "").replace(bot_mention_nick, "").strip()

        is_dm = not guild_id
        if not is_dm and bot_mention not in content and bot_mention_nick not in content:
            return

        now = Timestamp()
        now.GetCurrentTime()
        session_key = f"discord:{user_id}:{channel_id}"
        if guild_id:
            session_key += f":{guild_id}"

        display_name = author.get("global_name") or author.get("username", user_id)

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="discord",
            session_key=session_key,
            received_at=now,
            sender=Sender(
                platform_id=user_id,
                display_name=display_name,
                role="operator",
            ),
            content=Content(
                text=TextContent(
                    body=cleaned or content,
                    command_prefix="/goal" if cleaned.startswith("/goal") else "",
                    clean_text=cleaned or content,
                ),
            ),
            routing=RoutingHints(queue_mode="steer", priority=0),
        )

        if self._on_message:
            await self._on_message(unified)

    async def _handle_interaction(self, data: dict):
        interaction_type = data.get("type", 0)
        interaction_id = data.get("id", "")
        interaction_token = data.get("token", "")

        user = data.get("user", data.get("member", {}).get("user", {}))
        user_id = user.get("id", "")
        channel_id = data.get("channel_id", "")

        if interaction_type == 2:  # APPLICATION_COMMAND
            cmd_data = data.get("data", {})
            opts = cmd_data.get("options", [])
            sub = opts[0] if opts else {}
            prompt = (sub.get("options", [{}])[0].get("value", "")
                      if sub.get("options") else "")

            if prompt:
                now = Timestamp()
                now.GetCurrentTime()

                unified = UnifiedMessage(
                    event_id=str(uuid.uuid4()),
                    platform="discord",
                    session_key=f"discord:{user_id}:{channel_id}",
                    received_at=now,
                    sender=Sender(
                        platform_id=user_id,
                        display_name=user.get("global_name", user_id),
                        role="operator",
                    ),
                    content=Content(
                        text=TextContent(
                            body=prompt, command_prefix=f"/{sub.get('name', 'ask')}",
                            clean_text=prompt,
                        ),
                    ),
                    routing=RoutingHints(queue_mode="steer", priority=0),
                )

                await self._ack_interaction(interaction_id, interaction_token)
                if self._on_message:
                    await self._on_message(unified)

        elif interaction_type == 3:  # MESSAGE_COMPONENT (HITL)
            await self._ack_interaction(interaction_id, interaction_token)

    async def _ack_interaction(self, interaction_id: str, token: str):
        try:
            await self._http.post(
                f"/interactions/{interaction_id}/{token}/callback",
                json={"type": 5},
            )
        except Exception:
            pass

    # ── Streaming ───────────────────────────────────────────────

    def _edit_stream_message(self, chat_id: str) -> bool:
        stream = self._streaming.get(chat_id)
        if not stream or not stream.get("message_id"):
            return False
        try:
            async def _edit():
                resp = await self._http.patch(
                    f"/channels/{chat_id}/messages/{stream['message_id']}",
                    json={"content": stream.get("buffer", "")},
                )
                return resp.status_code == 200

            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_edit(), loop)
                ok = future.result(timeout=10)
            else:
                ok = loop.run_until_complete(_edit())
        except RuntimeError:
            ok = asyncio.run(_edit())
        except Exception:
            ok = False

        if ok:
            stream["last_edit"] = time.time()
        return ok
