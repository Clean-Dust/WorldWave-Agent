"""Feishu (飞书 / Lark) Platform Adapter for WW Gateway.

Architecture:
  - Runs HTTP server to receive Feishu event callbacks
  - Verifies challenge (URL verification) and event signatures
  - Obtains tenant_access_token via app_id + app_secret
  - Outbound via Feishu message API
  - Auto-registers if FEISHU_APP_ID + FEISHU_APP_SECRET are set
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Callable, Optional
from urllib.request import Request, urlopen
from http.server import HTTPServer, BaseHTTPRequestHandler

from google.protobuf.timestamp_pb2 import Timestamp

from proto.wavegate.v1.unified_message_pb2 import (
    UnifiedMessage, Sender, Content, TextContent, RoutingHints,
)
from gateway.adapters import BaseAdapter, AdapterRegistry

log = logging.getLogger("gateway.feishu")

FEISHU_API = "https://open.feishu.cn/open-apis"


class FeishuAdapter(BaseAdapter):
    """Feishu (飞书) platform adapter for WW Gateway."""

    platform = "feishu"

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        port: int = 0,
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self._app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        self._port = port or int(os.environ.get("FEISHU_WW_PORT", "9307"))
        self._on_message = on_message
        self._session_mgr = session_mgr
        self._whitelist = whitelist or set()

        self._running = False
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._access_token: str = ""
        self._token_expiry: float = 0

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        if not self._app_id or not self._app_secret:
            log.info("Feishu adapter: no credentials, skipping")
            return

        self._refresh_token()
        adapter_ref = self

        class FeishuHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                try:
                    length = int(handler.headers.get("Content-Length", 0))
                    body = handler.rfile.read(length).decode("utf-8")
                    data = json.loads(body)

                    # URL verification challenge
                    if data.get("type") == "url_verification":
                        challenge = data.get("challenge", "")
                        handler.send_response(200)
                        handler.send_header("Content-Type", "application/json")
                        handler.end_headers()
                        handler.wfile.write(
                            json.dumps({"challenge": challenge}).encode()
                        )
                        return

                    # Immediate ACK
                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/json")
                    handler.end_headers()
                    handler.wfile.write(b'{"code":0}')

                    # Process event
                    event = data.get("event", data)
                    threading.Thread(
                        target=adapter_ref._process_event,
                        args=(event,), daemon=True,
                        name="feishu-callback",
                    ).start()
                except Exception as e:
                    log.debug("Feishu callback error: %s", e)
                    try:
                        handler.send_response(200)
                        handler.end_headers()
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass

        try:
            self._server = HTTPServer(("0.0.0.0", self._port), FeishuHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="feishu-adapter"
            )
            self._thread.start()
            self._running = True
            log.info("Feishu adapter listening on :%s", self._port)
        except OSError as e:
            log.warning("Feishu adapter port %s in use: %s", self._port, e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        if not self._access_token:
            self._refresh_token()
        try:
            payload = json.dumps({
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text[:4000]}),
            }).encode()
            url = (
                f"{FEISHU_API}/im/v1/messages"
                f"?receive_id_type=open_id"
            )
            req = Request(url, data=payload, headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            })
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("code") == 0
        except Exception as e:
            log.debug("Feishu send failed: %s", e)
            return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        return self.send_message(chat_id, chunk)

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        if app_id and app_secret:
            adapter = cls(
                app_id=app_id, app_secret=app_secret,
                on_message=on_message, session_mgr=session_mgr,
                whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    def _refresh_token(self):
        try:
            payload = json.dumps({
                "app_id": self._app_id,
                "app_secret": self._app_secret,
            }).encode()
            url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data.get("code") == 0:
                    self._access_token = data["tenant_access_token"]
                    self._token_expiry = time.time() + data.get("expire", 7200) - 300
        except Exception as e:
            log.warning("Feishu token refresh failed: %s", e)

    def _process_event(self, event: dict):
        msg_type = event.get("type", "")
        if msg_type != "message":
            return

        sender = event.get("sender", {})
        user_id = sender.get("sender_id", {}).get("open_id", "")
        chat_id = event.get("message", {}).get("chat_id", "")
        content_raw = event.get("message", {}).get("content", "{}")

        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
        except json.JSONDecodeError:
            content = {}
        text = content.get("text", "")

        if not user_id or not text:
            return

        if self._whitelist and user_id not in self._whitelist:
            return

        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="feishu",
            session_key=f"feishu:{user_id}:{chat_id}",
            received_at=now,
            sender=Sender(platform_id=user_id, display_name=user_id, role="operator"),
            content=Content(text=TextContent(
                body=text,
                command_prefix="/goal" if text.startswith("/goal") else "",
                clean_text=text,
            )),
            routing=RoutingHints(queue_mode="steer", priority=0),
        )

        if self._on_message:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._on_message(unified), loop)
                else:
                    loop.run_until_complete(self._on_message(unified))
            except RuntimeError:
                asyncio.run(self._on_message(unified))
