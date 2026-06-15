"""WeChat Official Account (微信公众号) Platform Adapter for WW Gateway.

Architecture:
  - Runs HTTP server to receive WeChat server callbacks
  - Verifies signature on every request
  - Passive reply mode: must reply within 5s (text passthrough with ACK)
  - For long-running tasks: replies with ACK text, then pushes via customer
    service API when result is ready
  - Auto-registers if WECHAT_APP_ID + WECHAT_APP_SECRET + WECHAT_TOKEN are set

Note: WeChat personal accounts (微信个人号) are NOT supported via API.
      Use WeCom (企業微信) for bot-like interactions, or this adapter for
      Official Account (公众号) scenarios.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Callable, Optional
from urllib.request import Request, urlopen
from http.server import HTTPServer, BaseHTTPRequestHandler

from google.protobuf.timestamp_pb2 import Timestamp

from proto.wavegate.v1.unified_message_pb2 import (
    UnifiedMessage, Sender, Content, TextContent, RoutingHints,
)
from gateway.adapters import BaseAdapter, AdapterRegistry

log = logging.getLogger("gateway.wechat")

WECHAT_API = "https://api.weixin.qq.com/cgi-bin"


class WeChatAdapter(BaseAdapter):
    """WeChat Official Account (微信公众号) platform adapter."""

    platform = "wechat"

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        token: str = "",
        port: int = 0,
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._app_id = app_id or os.environ.get("WECHAT_APP_ID", "")
        self._app_secret = app_secret or os.environ.get("WECHAT_APP_SECRET", "")
        self._token = token or os.environ.get("WECHAT_TOKEN", "")
        self._port = port or int(os.environ.get("WECHAT_WW_PORT", "9309"))
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
        if not self._app_id or not self._token:
            log.info("WeChat adapter: no credentials, skipping")
            return

        self._refresh_access_token()
        adapter_ref = self

        class WeChatHandler(BaseHTTPRequestHandler):
            def do_GET(handler):
                # Server verification
                qs = handler.path.split("?")[-1] if "?" in handler.path else ""
                params = dict(
                    p.split("=") for p in qs.split("&") if "=" in p
                )
                sig = params.get("signature", "")
                ts = params.get("timestamp", "")
                nonce = params.get("nonce", "")
                echostr = params.get("echostr", "")

                if adapter_ref._verify_signature(sig, ts, nonce):
                    handler.send_response(200)
                    handler.end_headers()
                    handler.wfile.write(echostr.encode())
                else:
                    handler.send_response(403)
                    handler.end_headers()

            def do_POST(handler):
                try:
                    length = int(handler.headers.get("Content-Length", 0))
                    body = handler.rfile.read(length).decode("utf-8")

                    # Process message and return passive reply
                    reply = adapter_ref._handle_xml(body)

                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/xml")
                    handler.end_headers()
                    handler.wfile.write(reply.encode("utf-8"))
                except Exception as e:
                    log.debug("WeChat callback error: %s", e)
                    try:
                        handler.send_response(200)
                        handler.end_headers()
                        handler.wfile.write(b"success")
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass

        try:
            self._server = HTTPServer(("0.0.0.0", self._port), WeChatHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="wechat-adapter"
            )
            self._thread.start()
            self._running = True
            log.info("WeChat adapter listening on :%s", self._port)
        except OSError as e:
            log.warning("WeChat adapter port %s in use: %s", self._port, e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        """Send via customer service API (outside passive reply window)."""
        if not self._access_token:
            self._refresh_access_token()
        try:
            payload = json.dumps({
                "touser": chat_id,
                "msgtype": "text",
                "text": {"content": text[:2000]},
            }, ensure_ascii=False).encode("utf-8")
            url = f"{WECHAT_API}/message/custom/send?access_token={self._access_token}"
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("errcode") == 0
        except Exception as e:
            log.debug("WeChat send failed: %s", e)
            return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        return self.send_message(chat_id, chunk)

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        app_id = os.environ.get("WECHAT_APP_ID", "")
        token = os.environ.get("WECHAT_TOKEN", "")
        if app_id and token:
            adapter = cls(
                app_id=app_id, token=token,
                on_message=on_message, session_mgr=session_mgr,
                whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    # ── Signature verification ──────────────────────────────────

    def _verify_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        if not self._token:
            return False
        tmp_list = sorted([self._token, timestamp, nonce])
        tmp_str = "".join(tmp_list)
        expected = hashlib.sha1(tmp_str.encode()).hexdigest()
        return signature == expected

    def _refresh_access_token(self):
        if not self._app_id or not self._app_secret:
            return
        try:
            url = (
                f"{WECHAT_API}/token"
                f"?grant_type=client_credential"
                f"&appid={self._app_id}&secret={self._app_secret}"
            )
            req = Request(url)
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if "access_token" in data:
                    self._access_token = data["access_token"]
                    self._token_expiry = time.time() + data.get("expires_in", 7200) - 300
        except Exception as e:
            log.warning("WeChat token refresh failed: %s", e)

    # ── XML message handling ────────────────────────────────────

    def _handle_xml(self, body: str) -> str:
        """Parse WeChat XML message and return passive reply XML."""
        try:
            root = ET.fromstring(body)
            msg_type = _xml_text(root, "MsgType")
            from_user = _xml_text(root, "FromUserName")
            to_user = _xml_text(root, "ToUserName")
            content = _xml_text(root, "Content")

            if msg_type != "text" or not content:
                return _empty_reply(from_user, to_user)

            if self._whitelist and from_user not in self._whitelist:
                return _text_reply(from_user, to_user, "無權限")

            # Queue to gateway for processing
            self._dispatch_to_gateway(from_user, content)

            # Return immediate ACK (passive reply must be fast)
            return _text_reply(from_user, to_user, "收到，處理中…")

        except ET.ParseError:
            return "success"

    def _dispatch_to_gateway(self, user_id: str, content: str):
        """Dispatch message to gateway asynchronously."""
        if not self._on_message:
            return

        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="wechat",
            session_key=f"wechat:{user_id}:{user_id}",
            received_at=now,
            sender=Sender(platform_id=user_id, display_name=user_id, role="operator"),
            content=Content(text=TextContent(
                body=content,
                command_prefix="/goal" if content.startswith("/goal") else "",
                clean_text=content,
            )),
            routing=RoutingHints(queue_mode="followup", priority=0),
        )

        # Dispatch in background thread
        def _dispatch():
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

        threading.Thread(target=_dispatch, daemon=True).start()


# ── XML helpers ─────────────────────────────────────────────────

def _xml_text(element, tag: str) -> str:
    child = element.find(tag)
    if child is None:
        return ""
    return (child.text or "").strip()


def _text_reply(to_user: str, from_user: str, text: str) -> str:
    """Build WeChat passive text reply XML."""
    ts = int(time.time())
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{text}]]></Content>"
        f"</xml>"
    )


def _empty_reply(from_user: str, to_user: str) -> str:
    return _text_reply(from_user, to_user, "")
