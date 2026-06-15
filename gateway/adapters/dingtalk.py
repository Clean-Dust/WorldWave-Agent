"""DingTalk (钉钉) Platform Adapter for WW Gateway.

Architecture:
  - Runs HTTP server to receive DingTalk robot webhook callbacks
  - Outbound via webhook URL (simple robot) or Bot API (enterprise)
  - Auto-registers if DINGTALK_WEBHOOK_URL or DINGTALK_APP_KEY is set
"""

from __future__ import annotations

import hashlib
import hmac
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

log = logging.getLogger("gateway.dingtalk")

DINGTALK_API = "https://api.dingtalk.com/v1.0"


class DingTalkAdapter(BaseAdapter):
    """DingTalk (钉钉) platform adapter for WW Gateway."""

    platform = "dingtalk"

    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        webhook_url: str = "",
        port: int = 0,
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._app_key = app_key or os.environ.get("DINGTALK_APP_KEY", "")
        self._app_secret = app_secret or os.environ.get("DINGTALK_APP_SECRET", "")
        self._webhook_url = webhook_url or os.environ.get("DINGTALK_WEBHOOK_URL", "")
        self._port = port or int(os.environ.get("DINGTALK_WW_PORT", "9308"))
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
        if not self._webhook_url and not (self._app_key and self._app_secret):
            log.info("DingTalk adapter: no credentials, skipping")
            return

        adapter_ref = self

        class DingTalkHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                try:
                    length = int(handler.headers.get("Content-Length", 0))
                    body = handler.rfile.read(length)
                    data = json.loads(body.decode("utf-8"))

                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/json")
                    handler.end_headers()
                    handler.wfile.write(b'{"errcode":0}')

                    threading.Thread(
                        target=adapter_ref._process,
                        args=(data,), daemon=True,
                        name="dingtalk-callback",
                    ).start()
                except Exception as e:
                    log.debug("DingTalk callback error: %s", e)
                    try:
                        handler.send_response(200)
                        handler.end_headers()
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass

        try:
            self._server = HTTPServer(("0.0.0.0", self._port), DingTalkHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="dingtalk-adapter"
            )
            self._thread.start()
            self._running = True
            log.info("DingTalk adapter listening on :%s", self._port)
        except OSError as e:
            log.warning("DingTalk adapter port %s in use: %s", self._port, e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        if self._webhook_url:
            return self._send_webhook(text)
        else:
            return self._send_bot_api(chat_id, text)

    def _send_webhook(self, text: str) -> bool:
        try:
            payload = json.dumps({
                "msgtype": "markdown",
                "markdown": {"title": "WW", "text": text[:4000]},
            }).encode()
            req = Request(self._webhook_url, data=payload,
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("errcode") == 0
        except Exception as e:
            log.debug("DingTalk webhook send failed: %s", e)
            return False

    def _send_bot_api(self, user_id: str, text: str) -> bool:
        if not self._access_token:
            self._refresh_token()
        try:
            payload = json.dumps({
                "robotCode": self._app_key,
                "userIds": [user_id],
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": "WW", "text": text[:4000]}),
            }).encode()
            url = f"{DINGTALK_API}/robot/oToMessages/batchSend"
            req = Request(url, data=payload, headers={
                "x-acs-dingtalk-access-token": self._access_token,
                "Content-Type": "application/json",
            })
            with urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            log.debug("DingTalk bot send failed: %s", e)
            return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        return self.send_message(chat_id, chunk)

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        url = os.environ.get("DINGTALK_WEBHOOK_URL", "")
        app_key = os.environ.get("DINGTALK_APP_KEY", "")
        if url or app_key:
            adapter = cls(
                webhook_url=url, app_key=app_key,
                on_message=on_message, session_mgr=session_mgr,
                whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    def _refresh_token(self):
        if not self._app_key or not self._app_secret:
            return
        try:
            url = f"{DINGTALK_API}/oauth2/accessToken"
            payload = json.dumps({
                "appKey": self._app_key, "appSecret": self._app_secret,
            }).encode()
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                self._access_token = data.get("accessToken", "")
                self._token_expiry = time.time() + data.get("expireIn", 7200) - 300
        except Exception as e:
            log.warning("DingTalk token refresh failed: %s", e)

    def _process(self, data: dict):
        user_id = data.get("senderStaffId", data.get("senderId", ""))
        text = (
            data.get("text", {}).get("content", "")
            or data.get("content", "")
        )

        if not user_id or not text:
            return
        if self._whitelist and user_id not in self._whitelist:
            return

        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="dingtalk",
            session_key=f"dingtalk:{user_id}:{user_id}",
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
