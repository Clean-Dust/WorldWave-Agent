"""Minimal bridge between server.py's GatewayManager/TelegramGateway API
and the new Wavegate TelegramAdapter.

This exists because server.py references GatewayManager and TelegramGateway
classes that were never implemented. The actual adapter is TelegramAdapter
in gateway/adapters/telegram.py.
"""

from __future__ import annotations
import logging
from typing import Optional, Callable

log = logging.getLogger("gateway.bridge")


class TelegramGateway:
    """Wrapper around TelegramAdapter matching server.py's old API."""

    def __init__(
        self,
        token: str = "",
        workspace_id: Optional[int] = None,
        poll_interval: float = 2.0,
        task_handler: Optional[Callable] = None,
    ):
        log.info("TelegramGateway init workspace=%s", workspace_id)
        from gateway.adapters.telegram import TelegramAdapter

        # Wrap task_handler so it receives UnifiedMessage and extracts command + context
        async def _on_message(unified):
            text = ""
            chat_id = ""
            user_id = ""
            sender_name = "?"
            platform = "telegram"
            photo_path = ""
            try:
                if hasattr(unified, "content") and unified.content:
                    if hasattr(unified.content, "text") and unified.content.text:
                        text = (
                            getattr(unified.content.text, "clean_text", "")
                            or getattr(unified.content.text, "body", "")
                            or ""
                        )
                    if hasattr(unified, "routing") and unified.routing:
                        photo_path = getattr(unified.routing, "photo_path", "") or ""
                if hasattr(unified, "sender") and unified.sender:
                    sender_name = getattr(unified.sender, "display_name", "?")
                    user_id = (
                        getattr(unified.sender, "user_id", "")
                        or getattr(unified.sender, "id", "")
                        or ""
                    )
                    platform = getattr(unified, "platform", "telegram") or "telegram"
                # session_key is "telegram:{user_id}:{chat_id}" (or with tenant prefix)
                if hasattr(unified, "session_key") and unified.session_key:
                    parts = str(unified.session_key).split(":")
                    if len(parts) >= 3:
                        # last two segments are usually user_id, chat_id
                        user_id = user_id or parts[-2]
                        chat_id = parts[-1]
                    elif len(parts) == 2:
                        user_id = user_id or parts[0]
                        chat_id = parts[1]

                if not chat_id:
                    log.warning(
                        "No chat_id extracted, session_key=%s",
                        getattr(unified, "session_key", "N/A"),
                    )

                context = {
                    "platform": platform,
                    "chat_id": str(chat_id or ""),
                    "user_id": str(user_id or chat_id or "default"),
                    "sender": sender_name,
                    "photo_path": photo_path,
                    "session_key": getattr(unified, "session_key", "") or "",
                }

                log.info(
                    "gateway message chat=%s user=%s text=%r",
                    chat_id,
                    context["user_id"],
                    (text or "")[:80],
                )

                if task_handler:
                    # One user-visible text send per inbound message (chat_id).
                    # Covers adapter.send_message and tools/telegram path.
                    # begin_inbound is atomic busy+budget; refuses concurrent
                    # handlers for the same chat (poll must not fire-and-forget).
                    from gateway.outbound import begin_inbound, end_inbound

                    cid = str(chat_id) if chat_id else ""
                    if cid and not begin_inbound(cid):
                        log.warning(
                            "skip concurrent inbound for chat=%s (already busy)",
                            cid,
                        )
                        return
                    try:
                        result = task_handler(text, context)
                        if result and chat_id:
                            sent = self._adapter.send_message(chat_id, result)
                            log.info("send_message(%s) -> %s", chat_id, sent)
                        else:
                            log.warning(
                                "No reply sent: result=%r chat_id=%r",
                                (result[:80] if result else result),
                                chat_id,
                            )
                    finally:
                        if cid:
                            end_inbound(cid)
            except Exception as e:
                log.exception("BRIDGE handler error: %s", e)

        self._adapter = TelegramAdapter(
            token=token,
            workspace_id=workspace_id,
            poll_interval=poll_interval,
            on_message=_on_message,
        )
        self._started = False

    def start(self):
        if self._started or (hasattr(self._adapter, "is_running") and self._adapter.is_running()):
            log.info("TelegramGateway already running — skip start")
            return
        self._adapter.start()
        self._started = True
        log.info(
            "TelegramGateway started running=%s",
            self._adapter.is_running() if hasattr(self._adapter, "is_running") else "?",
        )

    def stop(self):
        self._adapter.stop()
        self._started = False

    def is_running(self) -> bool:
        return self._adapter.is_running() if hasattr(self._adapter, "is_running") else self._started

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        return self._adapter.send_message(chat_id, text, **kwargs)

    @property
    def bot_username(self) -> str:
        return getattr(self._adapter, "_bot_username", "")


class GatewayManager:
    """Simple registry for gateway adapters.

    register() only stores adapters — start_all() starts them once.
    This avoids double-start (register + startup) and import-time side effects.
    """

    def __init__(self):
        self._adapters: list = []
        self._started = False

    def register(self, adapter, start: bool = False):
        """Register adapter. Does NOT start by default (prevents double pollers)."""
        self._adapters.append(adapter)
        if start and hasattr(adapter, "start"):
            adapter.start()
        log.info("Gateway registered: %s", type(adapter).__name__)

    def list_gateways(self) -> list:
        gateways = []
        for a in self._adapters:
            adapter_type = type(a).__name__
            running = a.is_running() if hasattr(a, "is_running") else False
            configured = bool(getattr(a, "_token", True))
            gateways.append({
                "platform": adapter_type,
                "running": running,
                "configured": configured,
            })
        return gateways

    def stop_all(self):
        for a in self._adapters:
            if hasattr(a, "stop"):
                try:
                    a.stop()
                except Exception as e:
                    log.warning("Gateway stop failed: %s", e)
        self._started = False

    def start_all(self):
        """Idempotent start — safe to call from FastAPI startup only."""
        for a in self._adapters:
            try:
                if hasattr(a, "is_running") and a.is_running():
                    continue
                if hasattr(a, "start"):
                    a.start()
            except Exception as e:
                log.warning("Gateway start failed for %s: %s", type(a).__name__, e)
        self._started = True
