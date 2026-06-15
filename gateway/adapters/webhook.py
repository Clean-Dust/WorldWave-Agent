"""Generic Webhook Platform Adapter for WW Gateway.

Blueprint ref:
  "Webhook — catch-all adapter for custom integrations. Any external
   system can POST JSON to the WW webhook endpoint to inject messages."

Architecture:
  - Runs an HTTP server on configurable port (default 9305)
  - Accepts POST / with JSON payload: {user, text, platform?}
  - Supports optional HMAC signature verification
  - Immediately returns 200 OK, background processing
  - Auto-registers if WEBHOOK_WW_ENABLED=true or WEBHOOK_WW_PORT is set
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

from google.protobuf.timestamp_pb2 import Timestamp

from proto.wavegate.v1.unified_message_pb2 import (
    UnifiedMessage, Sender, Content, TextContent, RoutingHints,
)
from gateway.adapters import BaseAdapter, AdapterRegistry

log = logging.getLogger("gateway.webhook")


class WebhookAdapter(BaseAdapter):
    """Generic webhook adapter for custom integrations.

    Accepts JSON payloads:
      {
        "user": "alice",          // required: sender identifier
        "text": "do something",   // required: message text
        "platform": "myapp",      // optional: override platform name
        "channel": "general",     // optional: channel/room context
        "priority": 0             // optional: 0=normal, 1=high, 2=urgent
      }

    Optional HMAC signature: set WEBHOOK_WW_SECRET to enable.
    Signature header: X-WW-Signature (hex-encoded HMAC-SHA256 of body).
    """

    platform = "webhook"

    def __init__(
        self,
        port: int = 0,
        secret: str = "",
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._port = port or int(os.environ.get("WEBHOOK_WW_PORT", "9305"))
        self._secret = secret or os.environ.get("WEBHOOK_WW_SECRET", "")
        self._on_message = on_message
        self._session_mgr = session_mgr
        self._whitelist = whitelist or set()

        self._running = False
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(
            max_workers=int(os.environ.get("WEBHOOK_WW_MAX_WORKERS", "16")),
            thread_name_prefix="webhook-worker",
        )

    # ── Adapter interface ──────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return

        adapter_ref = self

        class WebhookHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                try:
                    length = int(handler.headers.get("Content-Length", 0))
                    body = handler.rfile.read(length)

                    # Verify HMAC if secret is configured
                    if adapter_ref._secret:
                        sig = handler.headers.get("X-WW-Signature", "")
                        expected = hmac.new(
                            adapter_ref._secret.encode(), body, hashlib.sha256
                        ).hexdigest()
                        if not hmac.compare_digest(sig, expected):
                            handler.send_response(401)
                            handler.end_headers()
                            handler.wfile.write(b"invalid signature")
                            return

                    data = json.loads(body.decode("utf-8"))
                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/json")
                    handler.end_headers()
                    handler.wfile.write(b'{"status":"accepted"}')

                    adapter_ref._executor.submit(
                        adapter_ref._process, data
                    )
                except Exception as e:
                    log.debug("Webhook error: %s", e)
                    try:
                        handler.send_response(400)
                        handler.end_headers()
                        handler.wfile.write(json.dumps({"error": str(e)}).encode())
                    except Exception:
                        pass

            def do_GET(handler):
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.end_headers()
                handler.wfile.write(json.dumps({
                    "status": "ok",
                    "platform": "webhook",
                    "version": "1.0",
                }).encode())

            def log_message(self, format, *args):
                pass

        try:
            self._server = HTTPServer(("0.0.0.0", self._port), WebhookHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="webhook-adapter"
            )
            self._thread.start()
            self._running = True
            log.info("Webhook adapter listening on :%s", self._port)
        except OSError as e:
            log.warning("Webhook adapter port %s in use: %s", self._port, e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
        self._executor.shutdown(wait=False, cancel_futures=True)
        log.info("Webhook adapter stopped")

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        """Webhook is inbound-only. Outbound goes to callback URL if set."""
        callback = os.environ.get("WEBHOOK_WW_CALLBACK", "")
        if not callback:
            log.debug("Webhook: no callback URL, dropping outbound message")
            return False
        try:
            from urllib.request import Request, urlopen
            payload = json.dumps({"user": chat_id, "text": text}).encode()
            req = Request(callback, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            log.debug("Webhook callback failed: %s", e)
            return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        text = chunk if isinstance(chunk, str) else str(chunk)
        return self.send_message(chat_id, text)

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        enabled = os.environ.get("WEBHOOK_WW_ENABLED", "").lower() in ("1", "true", "yes")
        port = os.environ.get("WEBHOOK_WW_PORT", "")
        if enabled or port:
            adapter = cls(
                port=int(port) if port else 0,
                on_message=on_message, session_mgr=session_mgr,
                whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    # ── Processing ─────────────────────────────────────────────

    def _process(self, data: dict):
        user = data.get("user", "unknown")
        text = data.get("text", data.get("message", ""))
        platform_override = data.get("platform", "webhook")
        channel = data.get("channel", "default")
        priority = data.get("priority", 0)

        if not text:
            return

        if self._whitelist and user not in self._whitelist:
            log.debug("Webhook: non-whitelisted sender %s dropped", user)
            return

        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform=platform_override,
            session_key=f"{platform_override}:{user}:{channel}",
            received_at=now,
            sender=Sender(
                platform_id=user,
                display_name=user,
                role="operator",
            ),
            content=Content(
                text=TextContent(
                    body=text,
                    command_prefix="/goal" if text.startswith("/goal") else "",
                    clean_text=text,
                ),
            ),
            routing=RoutingHints(queue_mode="steer", priority=priority),
        )

        if self._on_message:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._on_message(unified), loop
                    )
                else:
                    loop.run_until_complete(self._on_message(unified))
            except RuntimeError:
                asyncio.run(self._on_message(unified))
