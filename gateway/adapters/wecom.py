"""WeCom (企業微信) Platform Adapter for WW Gateway.

Blueprint ref:
  "Slack/WeCom — Webhook callback mode requires immediate 200 OK
   within 3-5 seconds. WW adapter must instantly ACK and push the
   actual LLM task into a background queue."

Architecture:
  - Runs a lightweight HTTP server to receive WeCom bot callbacks
  - Immediately returns 200 OK, then processes via on_message queue
  - Uses WeCom Bot webhook URL for outbound messages
  - Auto-registers if WECOM_BOT_KEY or WECOM_WEBHOOK_URL is set

WeCom bot message format:
  - Inbound: POST with XML/JSON body containing user, content, msgtype
  - Outbound: POST to webhook URL with markdown/text content
"""

from __future__ import annotations

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

log = logging.getLogger("gateway.wecom")

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComAdapter(BaseAdapter):
    """WeCom (企業微信) platform adapter for WW Gateway.

    Features:
    - HTTP webhook callback server on configurable port
    - Immediate 200 ACK, background processing
    - Outbound via Webhook URL (markdown/text)
    - Whitelist-aware for user access control
    - XML + JSON body parsing (WeCom sends both)
    """

    platform = "wecom"

    def __init__(
        self,
        corp_id: str = "",
        bot_key: str = "",
        webhook_url: str = "",
        port: int = 0,
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._corp_id = corp_id or os.environ.get("WECOM_CORP_ID", "")
        self._bot_key = bot_key or os.environ.get("WECOM_BOT_KEY", "")
        self._webhook_url = webhook_url or os.environ.get("WECOM_WEBHOOK_URL", "")
        self._port = port or int(os.environ.get("WECOM_WW_PORT", "9304"))
        self._on_message = on_message
        self._session_mgr = session_mgr
        self._whitelist = whitelist or set()

        self._running = False
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._access_token: str = ""
        self._token_expiry: float = 0

    # ── Adapter interface ──────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        if not self._bot_key and not self._webhook_url:
            log.info("WeCom adapter: no config, skipping")
            return

        # Refresh access token if using bot key
        if self._bot_key and self._corp_id:
            self._refresh_token()

        # Start HTTP server
        adapter_ref = self

        class WeComHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                try:
                    length = int(handler.headers.get("Content-Length", 0))
                    body = handler.rfile.read(length).decode("utf-8")

                    # Immediately ACK
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/plain")
                    handler.end_headers()
                    handler.wfile.write(b"success")

                    # Process in background thread
                    threading.Thread(
                        target=adapter_ref._process_callback,
                        args=(body,),
                        daemon=True,
                        name="wecom-callback",
                    ).start()
                except Exception as e:
                    log.debug("WeCom callback error: %s", e)
                    try:
                        handler.send_response(200)
                        handler.end_headers()
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass

        try:
            self._server = HTTPServer(("0.0.0.0", self._port), WeComHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="wecom-adapter"
            )
            self._thread.start()
            self._running = True
            log.info("WeCom adapter started on port %s", self._port)
        except OSError as e:
            log.warning("WeCom adapter port %s in use: %s", self._port, e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("WeCom adapter stopped")

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        """Send a message via WeCom webhook or bot API."""
        if self._webhook_url:
            return self._send_webhook(text)
        elif self._bot_key:
            return self._send_bot_api(chat_id, text)
        return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        text = chunk if isinstance(chunk, str) else str(chunk)
        return self.send_message(chat_id, text)

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        key = os.environ.get("WECOM_BOT_KEY", "")
        url = os.environ.get("WECOM_WEBHOOK_URL", "")
        if key or url:
            adapter = cls(
                bot_key=key, webhook_url=url,
                on_message=on_message, session_mgr=session_mgr,
                whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    # ── Token management ───────────────────────────────────────

    def _refresh_token(self):
        """Refresh WeCom access token."""
        if not self._corp_id or not self._bot_key:
            return
        try:
            url = (
                f"{WECOM_API}/gettoken"
                f"?corpid={self._corp_id}&corpsecret={self._bot_key}"
            )
            req = Request(url)
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data.get("errcode") == 0:
                    self._access_token = data["access_token"]
                    self._token_expiry = time.time() + data.get("expires_in", 7200) - 300
                    log.info("WeCom access token refreshed")
        except Exception as e:
            log.warning("WeCom token refresh failed: %s", e)

    # ── Outbound ────────────────────────────────────────────────

    def _send_webhook(self, text: str) -> bool:
        """Send via webhook URL (simple bot mode)."""
        try:
            payload = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": text[:4000]},
            }).encode()
            req = Request(
                self._webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            log.debug("WeCom webhook send failed: %s", e)
            return False

    def _send_bot_api(self, user_id: str, text: str) -> bool:
        """Send via bot API (enterprise bot mode)."""
        if not self._access_token:
            self._refresh_token()
            if not self._access_token:
                return False

        try:
            payload = json.dumps({
                "touser": user_id,
                "msgtype": "text",
                "agentid": os.environ.get("WECOM_AGENT_ID", "0"),
                "text": {"content": text[:2000]},
            }).encode()
            url = f"{WECOM_API}/message/send?access_token={self._access_token}"
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("errcode") == 0
        except Exception as e:
            log.debug("WeCom bot send failed: %s", e)
            return False

    # ── Inbound processing ─────────────────────────────────────

    def _process_callback(self, body: str):
        """Parse WeCom callback and dispatch to gateway."""
        user_id = ""
        user_name = ""
        content = ""

        # Try JSON first (webhook mode), then XML (bot mode)
        try:
            data = json.loads(body)
            # WeCom bot callback JSON format
            user_id = (
                data.get("From", {}).get("UserId", "")
                or data.get("from", {}).get("userid", "")
                or data.get("user", "")
            )
            user_name = data.get("From", {}).get("Name", user_id)
            content = (
                data.get("Text", {}).get("Content", "")
                or data.get("text", {}).get("content", "")
                or data.get("content", "")
            )
        except (json.JSONDecodeError, ValueError):
            pass

        if not content:
            try:
                root = ET.fromstring(body)
                user_id = _xml_text(root, ".//FromUserName") or ""
                content = _xml_text(root, ".//Content") or ""
                user_name = user_id
            except ET.ParseError:
                pass

        if not content or not user_id:
            return

        if self._whitelist and user_id not in self._whitelist:
            log.debug("WeCom: non-whitelisted user %s dropped", user_id)
            return

        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="wecom",
            session_key=f"wecom:{user_id}:{user_id}",
            received_at=now,
            sender=Sender(
                platform_id=user_id,
                display_name=user_name,
                role="operator",
            ),
            content=Content(
                text=TextContent(
                    body=content,
                    command_prefix="/goal" if content.startswith("/goal") else "",
                    clean_text=content,
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


def _xml_text(element, xpath: str) -> str:
    """Extract text from XML element by tag path."""
    parts = xpath.replace(".//", "").split("/")
    current = element
    for part in parts:
        child = current.find(part)
        if child is None:
            # Try with namespace
            child = current.find(f"{{*}}{part}")
        if child is None:
            return ""
        current = child
    return (current.text or "").strip()
