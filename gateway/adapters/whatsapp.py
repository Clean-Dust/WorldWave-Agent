"""WhatsApp Platform Adapter for WW Gateway.

Architecture:
  - Connects to WhatsApp Cloud API (Meta) via webhook + REST
  - Inbound: Meta webhook callback (POST with signed payload)
  - Outbound: Cloud API /messages endpoint
  - Auto-registers if WHATSAPP_TOKEN and WHATSAPP_PHONE_ID are set
  - Supports text, media attachments, and interactive replies
"""

from __future__ import annotations

import hashlib
import hmac
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

log = logging.getLogger("gateway.whatsapp")

WA_API = "https://graph.facebook.com/v22.0"


class WhatsAppAdapter(BaseAdapter):
    """WhatsApp Cloud API platform adapter for WW Gateway.

    Features:
    - Meta webhook verification + message receiving
    - Cloud API outbound (text, media)
    - HMAC-SHA256 signature verification
    - Whitelist-aware access control
    """

    platform = "whatsapp"

    def __init__(
        self,
        token: str = "",
        phone_id: str = "",
        verify_token: str = "",
        port: int = 0,
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._token = token or os.environ.get("WHATSAPP_TOKEN", "")
        self._phone_id = phone_id or os.environ.get("WHATSAPP_PHONE_ID", "")
        self._verify_token = verify_token or os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self._port = port or int(os.environ.get("WHATSAPP_WW_PORT", "9306"))
        self._on_message = on_message
        self._session_mgr = session_mgr
        self._whitelist = whitelist or set()

        self._running = False
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")

    # ── Adapter interface ──────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        if not self._token or not self._phone_id:
            log.info("WhatsApp adapter: no credentials, skipping")
            return

        adapter_ref = self

        class WAHandler(BaseHTTPRequestHandler):
            def do_GET(handler):
                # Webhook verification
                qs = handler.path.split("?")[-1] if "?" in handler.path else ""
                params = dict(
                    p.split("=") for p in qs.split("&") if "=" in p
                )
                mode = params.get("hub.mode", "")
                token = params.get("hub.verify_token", "")
                challenge = params.get("hub.challenge", "")

                if mode == "subscribe" and token == adapter_ref._verify_token:
                    handler.send_response(200)
                    handler.end_headers()
                    handler.wfile.write(challenge.encode())
                else:
                    handler.send_response(403)
                    handler.end_headers()

            def do_POST(handler):
                try:
                    length = int(handler.headers.get("Content-Length", 0))
                    body = handler.rfile.read(length)

                    # Verify X-Hub-Signature-256
                    if adapter_ref._app_secret:
                        sig = handler.headers.get("X-Hub-Signature-256", "")
                        expected = "sha256=" + hmac.new(
                            adapter_ref._app_secret.encode(), body, hashlib.sha256
                        ).hexdigest()
                        if not hmac.compare_digest(sig, expected):
                            handler.send_response(401)
                            handler.end_headers()
                            return

                    data = json.loads(body.decode("utf-8"))
                    handler.send_response(200)
                    handler.end_headers()

                    threading.Thread(
                        target=adapter_ref._process,
                        args=(data,),
                        daemon=True,
                        name="whatsapp-callback",
                    ).start()
                except Exception as e:
                    log.debug("WhatsApp callback error: %s", e)
                    try:
                        handler.send_response(200)
                        handler.end_headers()
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass

        try:
            self._server = HTTPServer(("0.0.0.0", self._port), WAHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="whatsapp-adapter"
            )
            self._thread.start()
            self._running = True
            log.info("WhatsApp adapter listening on :%s", self._port)
        except OSError as e:
            log.warning("WhatsApp adapter port %s in use: %s", self._port, e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("WhatsApp adapter stopped")

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        if not self._token:
            return False
        try:
            payload = json.dumps({
                "messaging_product": "whatsapp",
                "to": chat_id,
                "type": "text",
                "text": {"body": text[:4000]},
            }).encode()
            url = f"{WA_API}/{self._phone_id}/messages"
            req = Request(url, data=payload, headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            })
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return "error" not in data
        except Exception as e:
            log.debug("WhatsApp send failed: %s", e)
            return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        text = chunk if isinstance(chunk, str) else str(chunk)
        return self.send_message(chat_id, text)

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        token = os.environ.get("WHATSAPP_TOKEN", "")
        phone_id = os.environ.get("WHATSAPP_PHONE_ID", "")
        if token and phone_id:
            adapter = cls(
                token=token, phone_id=phone_id,
                on_message=on_message, session_mgr=session_mgr,
                whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    # ── Processing ─────────────────────────────────────────────

    def _process(self, data: dict):
        """Process Meta webhook payload."""
        entries = data.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                messages = change.get("value", {}).get("messages", [])
                for msg in messages:
                    self._handle_message(msg)

    def _handle_message(self, msg: dict):
        from_number = msg.get("from", "")
        msg_type = msg.get("type", "text")

        if msg_type == "text":
            body = msg.get("text", {}).get("body", "")
        else:
            # Non-text: use type as placeholder, let agent handle later
            body = msg.get(msg_type, {}).get("caption", f"[{msg_type}]")

        if not body or not from_number:
            return

        if self._whitelist and from_number not in self._whitelist:
            log.debug("WhatsApp: non-whitelisted number %s dropped", from_number)
            return

        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="whatsapp",
            session_key=f"whatsapp:{from_number}:{from_number}",
            received_at=now,
            sender=Sender(
                platform_id=from_number,
                display_name=from_number,
                role="operator",
            ),
            content=Content(
                text=TextContent(
                    body=body,
                    command_prefix="/goal" if body.startswith("/goal") else "",
                    clean_text=body,
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
                        self._on_message(unified), loop
                    )
                else:
                    loop.run_until_complete(self._on_message(unified))
            except RuntimeError:
                asyncio.run(self._on_message(unified))
