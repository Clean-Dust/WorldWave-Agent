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
        print(f"[WW] TelegramGateway init: token={'***' if token else 'EMPTY'}, ws={workspace_id}", flush=True)
        from gateway.adapters.telegram import TelegramAdapter
        
        # Wrap task_handler so it receives UnifiedMessage and extracts command + context
        async def _on_message(unified):
            print(f"[BRIDGE] _on_message called with unified type={type(unified).__name__}", flush=True)
            # Extract text from UnifiedMessage content
            text = ""
            chat_id = ""
            sender_name = "?"
            platform = "telegram"
            photo_path = ""
            try:
                if hasattr(unified, 'content') and unified.content:
                    if hasattr(unified.content, 'text') and unified.content.text:
                        text = getattr(unified.content.text, 'clean_text', '') or \
                               getattr(unified.content.text, 'body', '') or ''
                    # Extract photo_path from routing hints (set by TelegramAdapter)
                    if hasattr(unified, 'routing') and unified.routing:
                        photo_path = getattr(unified.routing, 'photo_path', '') or ''
                if hasattr(unified, 'sender') and unified.sender:
                    sender_name = getattr(unified.sender, 'display_name', '?')
                    platform = getattr(unified, 'platform', 'telegram')
                # session_key is "telegram:{user_id}:{chat_id}"
                if hasattr(unified, 'session_key') and unified.session_key:
                    parts = unified.session_key.split(':')
                    if len(parts) >= 3:
                        chat_id = parts[2]  # third segment is chat_id

                if not chat_id:
                    print(f"[BRIDGE] WARNING: No chat_id extracted, session_key={getattr(unified, 'session_key', 'N/A')}", flush=True)

                context = {
                    "platform": platform,
                    "chat_id": chat_id,
                    "sender": sender_name,
                    "photo_path": photo_path,
                }
                
                print(f"[BRIDGE] text={text[:80]!r} chat_id={chat_id} sender={sender_name}", flush=True)
                
                if task_handler:
                    result = task_handler(text, context)
                    print(f"[BRIDGE] task_handler returned: {result[:100] if result else 'None'!r}", flush=True)
                    # Send back to the platform
                    if result and chat_id:
                        sent = self._adapter.send_message(chat_id, result)
                        print(f"[BRIDGE] send_message({chat_id}, ...) -> {sent}", flush=True)
                    else:
                        print(f"[BRIDGE] No reply sent: result={result!r} chat_id={chat_id!r}", flush=True)
            except Exception as e:
                import traceback
                print(f"[BRIDGE] ERROR: {e}", flush=True)
                traceback.print_exc()
        
        self._adapter = TelegramAdapter(
            token=token,
            workspace_id=workspace_id,
            poll_interval=poll_interval,
            on_message=_on_message,
        )
        print(f"[WW] TelegramAdapter created, bot_username={self._adapter._bot_username}", flush=True)

    def start(self):
        print("[WW] TelegramGateway.start() called", flush=True)
        self._adapter.start()
        print(f"[WW] TelegramGateway.start() done, running={self._adapter.is_running()}", flush=True)

    def stop(self):
        self._adapter.stop()

    def is_running(self) -> bool:
        return self._adapter.is_running()

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        return self._adapter.send_message(chat_id, text, **kwargs)

    @property
    def bot_username(self) -> str:
        return getattr(self._adapter, '_bot_username', '')


class GatewayManager:
    """Simple registry for gateway adapters."""

    def __init__(self):
        self._adapters: list = []

    def register(self, adapter):
        self._adapters.append(adapter)
        if hasattr(adapter, 'start'):
            adapter.start()
        log.info("Gateway registered: %s", type(adapter).__name__)

    def list_gateways(self) -> list:
        gateways = []
        for a in self._adapters:
            adapter_type = type(a).__name__
            running = a.is_running() if hasattr(a, 'is_running') else False
            configured = bool(a._token) if hasattr(a, '_token') else True
            gateways.append({
                "platform": adapter_type,
                "running": running,
                "configured": configured,
            })
        return gateways

    def stop_all(self):
        for a in self._adapters:
            if hasattr(a, 'stop'):
                a.stop()

    def start_all(self):
        for a in self._adapters:
            if hasattr(a, 'start'):
                a.start()
