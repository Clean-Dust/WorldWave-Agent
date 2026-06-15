"""Wavegate Platform Adapter Registry.

Manages all platform adapters. Each adapter:
- Connects to one external messaging platform (Telegram, Discord, etc.)
- Normalizes incoming events into UnifiedMessage format
- Sends responses back through the platform's API
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

log = logging.getLogger("gateway.adapters")

# Global registry of adapter instances
_registry: Dict[str, "BaseAdapter"] = {}


class BaseAdapter:
    """Abstract base for platform adapters.

    Each adapter handles one messaging platform.
    Lifecycle: connect() → start() → stop() → disconnect()
    Token locking ensures single connection ownership across instances.
    """

    platform: str = "unknown"

    def start(self): ...
    def stop(self): ...
    def is_running(self) -> bool: return False
    def send_message(self, chat_id: str, text: str, **kwargs) -> bool: return False
    def send_stream_chunk(self, chat_id: str, chunk) -> bool: return False

    def connect(self, nats=None) -> bool:
        """Acquire token lock before starting the adapter.
        Blueprint: Single Connection Ownership via distributed locks.
        Returns True if lock acquired and connection established.
        """
        if not nats:
            return True  # No NATS, skip locking
        import hashlib
        token = getattr(self, '_token', '') or getattr(self, 'platform', 'unknown')
        token_id = hashlib.md5(token.encode()).hexdigest()[:8]
        return nats.token_lock_acquire(self.platform, token_id)

    def disconnect(self, nats=None):
        """Release token lock on adapter shutdown."""
        if not nats:
            return
        import hashlib
        token = getattr(self, '_token', '') or getattr(self, 'platform', 'unknown')
        token_id = hashlib.md5(token.encode()).hexdigest()[:8]
        nats.token_lock_release(self.platform, token_id)


class AdapterRegistry:
    """Global registry for platform adapters."""

    @staticmethod
    def register(adapter: BaseAdapter):
        _registry[adapter.platform] = adapter
        log.info("Adapter registered: %s", adapter.platform)

    @staticmethod
    def start_all(on_message: Callable, session_mgr=None, nats=None, whitelist: Optional[set] = None):
        """Start all registered adapters with token lock acquisition."""
        from gateway.adapters.telegram import TelegramAdapter
        from gateway.adapters.slack import SlackAdapter
        from gateway.adapters.discord import DiscordAdapter
        from gateway.adapters.signal import SignalAdapter
        from gateway.adapters.wecom import WeComAdapter
        from gateway.adapters.webhook import WebhookAdapter
        from gateway.adapters.whatsapp import WhatsAppAdapter
        from gateway.adapters.feishu import FeishuAdapter
        from gateway.adapters.dingtalk import DingTalkAdapter
        from gateway.adapters.wechat import WeChatAdapter

        # Auto-register Telegram if token is available
        TelegramAdapter.try_register(on_message=on_message, session_mgr=session_mgr)
        # Auto-register Slack if token is available
        SlackAdapter.try_register(on_message=on_message, session_mgr=session_mgr)
        # Auto-register Discord if token is available
        DiscordAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)
        # Auto-register Signal if signal-cli is configured
        SignalAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)
        # Auto-register WeCom if configured
        WeComAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)
        # Auto-register generic Webhook if enabled
        WebhookAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)
        # Auto-register WhatsApp if configured
        WhatsAppAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)
        # Auto-register Feishu if configured
        FeishuAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)
        # Auto-register DingTalk if configured
        DingTalkAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)
        # Auto-register WeChat Official Account if configured
        WeChatAdapter.try_register(on_message=on_message, session_mgr=session_mgr, whitelist=whitelist)

        for name, adapter in _registry.items():
            try:
                # Acquire token lock before starting
                if nats:
                    locked = adapter.connect(nats)
                    if not locked:
                        log.warning(
                            "Adapter %s: token lock held by another instance, skipping", name
                        )
                        continue
                adapter.start()
                log.info("Adapter started: %s", name)
            except Exception as e:
                log.error("Failed to start adapter %s: %s", name, e)

    @staticmethod
    def stop_all(nats=None):
        for name, adapter in _registry.items():
            try:
                adapter.stop()
                if nats:
                    adapter.disconnect(nats)
            except Exception as e:
                log.error("Failed to stop adapter %s: %s", name, e)

    @staticmethod
    def list_running() -> List[str]:
        return [name for name, a in _registry.items() if a.is_running()]

    @staticmethod
    def send_response(platform: str, session_key: str, text: str):
        """Send a response back through the appropriate platform adapter."""
        adapter = _registry.get(platform)
        if adapter:
            # Extract chat_id from session_key (format: platform:user_id:chat_id)
            parts = session_key.split(":")
            chat_id = parts[2] if len(parts) > 2 else session_key
            adapter.send_message(chat_id, text)

    @staticmethod
    def send_stream_chunk(platform: str, session_key: str, chunk):
        adapter = _registry.get(platform)
        if adapter:
            parts = session_key.split(":")
            chat_id = parts[2] if len(parts) > 2 else session_key
            adapter.send_stream_chunk(chat_id, chunk)
