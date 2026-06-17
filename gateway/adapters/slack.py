"""Slack Platform Adapter for WW Gateway.

Blueprint ref:
  "Slack/WeCom — Webhook callback mode requires immediate 200 OK within
   3-5 seconds. WW adapter must instantly ACK and push the actual LLM
   task into a background queue."

Architecture:
  - Runs a lightweight HTTP server to receive Slack Events API callbacks
  - Immediately returns HTTP 200, then processes via on_message queue
  - Uses Slack Web API (chat.postMessage) for outbound messages
  - Auto-registers if SLACK_BOT_TOKEN env var is set
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Callable, Optional
from urllib.request import Request, urlopen
from http.server import HTTPServer, BaseHTTPRequestHandler

from google.protobuf.timestamp_pb2 import Timestamp

from proto.wavegate.v1.unified_message_pb2 import (
    UnifiedMessage, Sender, Content, TextContent, RoutingHints,
)
from gateway.adapters import BaseAdapter, AdapterRegistry

log = logging.getLogger("gateway.slack")

SLACK_API = "https://slack.com/api"


class SlackAdapter(BaseAdapter):
    """Slack platform adapter for WW Gateway.

    Features:
    - Events API callback at SLACK_WW_PORT (default 9303)
    - Immediate 200 ACK, background processing
    - @mention detection in channels
    - Normalization to UnifiedMessage
    """

    platform = "slack"

    def __init__(
        self,
        token: str = "",
        signing_secret: str = "",
        port: int = 0,
        on_message: Optional[Callable] = None,
        session_mgr=None,
    ):
        self._token = token or os.environ.get("SLACK_BOT_TOKEN", "")
        self._signing_secret = signing_secret or os.environ.get("SLACK_SIGNING_SECRET", "")
        self._port = port or int(os.environ.get("SLACK_WW_PORT", "9303"))
        self._on_message = on_message
        self._session_mgr = session_mgr

        self._bot_user_id: str = ""
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    # ── Adapter interface ──────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        if not self._token:
            log.info("Slack adapter: no token, skipping")
            return

        self._resolve_bot_id()
        self._stop_event.clear()

        # Start the HTTP server for Slack events
        adapter_ref = self
        class SlackHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                try:
                    length = int(handler.headers.get("Content-Length", 0))
                    body = handler.rfile.read(length).decode("utf-8")
                    data = json.loads(body)

                    # Slack URL verification challenge
                    if data.get("type") == "url_verification":
                        handler.send_response(200)
                        handler.send_header("Content-Type", "text/plain")
                        handler.end_headers()
                        handler.wfile.write(data["challenge"].encode())
                        return

                    # Immediately ACK
                    handler.send_response(200)
                    handler.end_headers()

                    # Process event in background
                    event = data.get("event", {})
                    if event.get("type") == "app_mention":
                        adapter_ref._handle_mention(event)

                except Exception as e:
                    log.debug("Slack event error: %s", e)
                    try:
                        handler.send_response(200)
                        handler.end_headers()
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass  # Suppress HTTP server logs

        try:
            self._server = HTTPServer(("0.0.0.0", self._port), SlackHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="slack-adapter"
            )
            self._thread.start()
            self._running = True
            log.info("Slack adapter started on port %s", self._port)
        except OSError as e:
            log.warning("Slack adapter port %s in use: %s", self._port, e)

    def stop(self):
        if not self._running:
            return
        self._stop_event.set()
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        """Send a message to a Slack channel."""
        return self._slack_api("chat.postMessage", {
            "channel": chat_id,
            "text": text[:4000],
            "mrkdwn": True,
        })

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        """Slack doesn't support editMessageText — send as new message."""
        return self.send_message(chat_id, chunk if isinstance(chunk, str) else str(chunk))

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None):
        """Auto-register if token is configured."""
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if token:
            adapter = cls(token=token, on_message=on_message, session_mgr=session_mgr)
            AdapterRegistry.register(adapter)

    # ── Internal ────────────────────────────────────────────────

    def _resolve_bot_id(self):
        """Get the bot's user ID from Slack."""
        result = self._slack_api("auth.test")
        if result.get("ok"):
            self._bot_user_id = result.get("user_id", "")
            log.info("Slack bot user: %s", self._bot_user_id)

    def _slack_api(self, method: str, data: dict = None) -> dict:
        """Call Slack Web API."""
        try:
            url = f"{SLACK_API}/{method}"
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            }
            req = Request(url, data=json.dumps(data or {}).encode(), headers=headers)
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.debug("Slack API call failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _handle_mention(self, event: dict):
        """Process an app_mention event."""
        user_id = event.get("user", "")
        channel = event.get("channel", "")
        text = event.get("text", "")

        # Strip @bot mention
        if self._bot_user_id:
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()

        # Build UnifiedMessage
        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="slack",
            session_key=f"slack:{user_id}:{channel}",
            received_at=now,
            sender=Sender(
                platform_id=user_id,
                display_name=user_id,
                role="operator",
            ),
            content=Content(
                text=TextContent(
                    body=text,
                    command_prefix="",
                    clean_text=text,
                ),
            ),
            routing=RoutingHints(queue_mode="steer", priority=0),
        )

        if self._on_message:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._on_message(unified), loop,
                    )
                else:
                    loop.run_until_complete(self._on_message(unified))
            except RuntimeError:
                asyncio.run(self._on_message(unified))
