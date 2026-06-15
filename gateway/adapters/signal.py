"""Signal Platform Adapter for WW Gateway.

Blueprint ref:
  "Signal — focused on privacy, requires external signal-cli daemon
   in HTTP mode as bridge. WW gateway has a built-in heartbeat with
   JSON Schema frame validation."

Architecture:
  - Connects to signal-cli REST API (default http://127.0.0.1:8080)
  - signal-cli must be installed separately as a system daemon
  - Polls /v1/receive/{number} for incoming messages
  - Sends via /v2/send
  - Auto-registers if SIGNAL_CLI_URL or SIGNAL_CLI_NUMBER is set
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

log = logging.getLogger("gateway.signal")

SIGNAL_CLI_DEFAULT = "http://127.0.0.1:8080"


class SignalAdapter(BaseAdapter):
    """Signal platform adapter for WW Gateway.

    Features:
    - Connects to signal-cli REST API
    - Polls for incoming messages every 2 seconds
    - Sends text messages via signal-cli
    - Whitelist-aware for DM pairing
    """

    platform = "signal"

    def __init__(
        self,
        cli_url: str = "",
        account: str = "",
        on_message: Optional[Callable] = None,
        session_mgr=None,
        whitelist: Optional[set] = None,
    ):
        self._cli_url = cli_url or os.environ.get("SIGNAL_CLI_URL", SIGNAL_CLI_DEFAULT)
        self._account = account or os.environ.get("SIGNAL_CLI_NUMBER", "")
        self._on_message = on_message
        self._session_mgr = session_mgr
        self._whitelist = whitelist or set()

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._last_timestamp: int = 0

    # ── Adapter interface ──────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        if not self._account:
            log.info("Signal adapter: no account (SIGNAL_CLI_NUMBER), skipping")
            return

        self._http = httpx.AsyncClient(
            base_url=self._cli_url,
            timeout=httpx.Timeout(15.0),
        )
        self._poll_task = asyncio.ensure_future(self._poll_loop())
        self._running = True
        log.info("Signal adapter started (account: %s)", self._account)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._http:
            try:
                asyncio.ensure_future(self._http.aclose())
            except Exception:
                pass
        log.info("Signal adapter stopped")

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        if not self._http:
            return False
        try:
            payload = {
                "number": self._account,
                "recipients": [chat_id],
                "message": text[:4000],
            }

            async def _send():
                resp = await self._http.post("/v2/send", json=payload)
                return resp.status_code in (200, 201)

            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_send(), loop)
                return future.result(timeout=10)
            return loop.run_until_complete(_send())
        except RuntimeError:
            return asyncio.run(_send())
        except Exception as e:
            log.debug("Signal send failed: %s", e)
            return False

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        text = chunk if isinstance(chunk, str) else str(chunk)
        return self.send_message(chat_id, text)

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None, whitelist=None):
        url = os.environ.get("SIGNAL_CLI_URL", "")
        account = os.environ.get("SIGNAL_CLI_NUMBER", "")
        if url or account:
            adapter = cls(
                cli_url=url, account=account,
                on_message=on_message, session_mgr=session_mgr,
                whitelist=whitelist,
            )
            AdapterRegistry.register(adapter)

    # ── Poll loop ───────────────────────────────────────────────

    async def _poll_loop(self):
        """Poll signal-cli for incoming messages."""
        # Initialize timestamp to now
        self._last_timestamp = int(time.time() * 1000)

        while self._running:
            try:
                resp = await self._http.get(
                    f"/v1/receive/{self._account}",
                    params={"timeout": 5},
                )
                if resp.status_code == 200:
                    for msg in resp.json():
                        await self._handle_message(msg)
                        ts = msg.get("timestamp", 0)
                        if ts > self._last_timestamp:
                            self._last_timestamp = ts
                elif resp.status_code == 204:
                    pass  # No messages
                else:
                    log.debug("Signal receive: HTTP %s", resp.status_code)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Signal poll error: %s", e)

            await asyncio.sleep(2)

    async def _handle_message(self, msg: dict):
        """Process an incoming Signal message."""
        envelope = msg.get("envelope", msg)
        source = envelope.get("source", "")
        source_name = envelope.get("sourceName", source)
        body = msg.get("message", envelope.get("dataMessage", {}).get("message", ""))

        if not source or not body:
            return

        # Skip non-data messages (receipts, typing indicators, etc.)
        if not envelope.get("dataMessage"):
            return

        if self._whitelist and source not in self._whitelist:
            log.debug("Signal: non-whitelisted user %s dropped", source)
            return

        now = Timestamp()
        now.GetCurrentTime()

        unified = UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="signal",
            session_key=f"signal:{source}:{source}",
            received_at=now,
            sender=Sender(
                platform_id=source,
                display_name=source_name,
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
            await self._on_message(unified)
