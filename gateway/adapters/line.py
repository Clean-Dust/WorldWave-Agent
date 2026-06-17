"""
ww/gateway/adapters/line.py — LINE Messaging API Adapter v0.2

Uses LINE Messaging API with webhook callback.
Env vars: LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET
"""

from __future__ import annotations
import logging
import os
from typing import Callable, Dict, Optional

logger = logging.getLogger("ww.gateway.line")

LINE_API = "https://api.line.me/v2"


class LineAdapter:
    """LINE platform adapter for Worldwave Gateway."""

    def __init__(self):
        self.channel_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        self.channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
        self._message_handler: Optional[Callable] = None

    @property
    def connected(self) -> bool:
        return bool(self.channel_token)

    async def connect(self) -> bool:
        """Verify LINE connectivity."""
        if not self.channel_token:
            logger.warning("LINE_CHANNEL_ACCESS_TOKEN not set")
            return False
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{LINE_API}/bot/info",
                    headers={"Authorization": f"Bearer {self.channel_token}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"LINE connected as {data.get('displayName', 'Unknown')}")
                    return True
                logger.error(f"LINE auth failed: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"LINE connect error: {e}")
            return False

    async def send_message(self, user_id: str, text: str) -> bool:
        """Send a text message via LINE Push API."""
        if not self.channel_token:
            return False
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{LINE_API}/bot/message/push",
                    headers={
                        "Authorization": f"Bearer {self.channel_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "to": user_id,
                        "messages": [{"type": "text", "text": text}],
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"LINE send error: {e}")
            return False

    async def send_flex_message(self, user_id: str, alt_text: str, contents: Dict) -> bool:
        """Send a LINE Flex Message (rich card UI)."""
        if not self.channel_token:
            return False
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{LINE_API}/bot/message/push",
                    headers={
                        "Authorization": f"Bearer {self.channel_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "to": user_id,
                        "messages": [{
                            "type": "flex",
                            "altText": alt_text,
                            "contents": contents,
                        }],
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"LINE flex send error: {e}")
            return False

    async def reply_message(self, reply_token: str, text: str) -> bool:
        """Reply to a LINE message using the reply token."""
        if not self.channel_token:
            return False
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{LINE_API}/bot/message/reply",
                    headers={
                        "Authorization": f"Bearer {self.channel_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "replyToken": reply_token,
                        "messages": [{"type": "text", "text": text}],
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"LINE reply error: {e}")
            return False

    async def send_image(self, user_id: str, image_url: str, preview_url: str = None) -> bool:
        """Send an image via LINE."""
        if not self.channel_token:
            return False
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{LINE_API}/bot/message/push",
                    headers={
                        "Authorization": f"Bearer {self.channel_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "to": user_id,
                        "messages": [{
                            "type": "image",
                            "originalContentUrl": image_url,
                            "previewImageUrl": preview_url or image_url,
                        }],
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"LINE image send error: {e}")
            return False

    def verify_signature(self, body: str, signature: str) -> bool:
        """Verify LINE webhook signature."""
        import hmac
        import hashlib
        import base64
        if not self.channel_secret:
            return True  # Skip verification if not configured
        expected = base64.b64encode(
            hmac.new(
                self.channel_secret.encode(),
                body.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return hmac.compare_digest(expected, signature)

    async def handle_webhook(self, body: Dict) -> Optional[Dict]:
        """Process a LINE webhook event. Returns a normalized message dict or None."""
        events = body.get("events", [])
        results = []
        for event in events:
            if event.get("type") == "message":
                msg = event.get("message", {})
                if msg.get("type") == "text":
                    results.append({
                        "platform": "line",
                        "user_id": event.get("source", {}).get("userId", ""),
                        "reply_token": event.get("replyToken", ""),
                        "message": msg.get("text", ""),
                        "timestamp": event.get("timestamp", 0),
                    })
        return results[0] if results else None


# LINE webhook is typically received via HTTP endpoint (handled by gateway server)
