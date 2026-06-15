"""
ww/gateway/adapters/matrix.py — Matrix (Element/Synapse) Adapter v0.2

Uses Matrix Client-Server API via matrix-nio or raw HTTP.
Env vars: MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger("ww.gateway.matrix")

DEFAULT_HOMESERVER = "https://matrix.org"


class MatrixAdapter:
    """Matrix protocol adapter for Worldwave Gateway."""

    def __init__(self):
        self.homeserver = os.getenv("MATRIX_HOMESERVER", DEFAULT_HOMESERVER)
        self.user_id = os.getenv("MATRIX_USER_ID", "")
        self.access_token = os.getenv("MATRIX_ACCESS_TOKEN", "")
        self._polling = False
        self._since = None
        self._message_handler: Optional[Callable] = None

    @property
    def connected(self) -> bool:
        return bool(self.access_token and self.user_id)

    async def connect(self) -> bool:
        """Verify connectivity to Matrix homeserver."""
        if not self.access_token:
            logger.warning("MATRIX_ACCESS_TOKEN not set")
            return False
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.homeserver}/_matrix/client/v3/account/whoami",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"Matrix connected as {data.get('user_id')}")
                    return True
                logger.error(f"Matrix auth failed: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Matrix connect error: {e}")
            return False

    async def send_message(self, room_id: str, text: str) -> bool:
        """Send a text message to a Matrix room."""
        if not self.access_token:
            return False
        try:
            import httpx
            txn_id = str(int(time.time() * 1000))
            async with httpx.AsyncClient() as client:
                resp = await client.put(
                    f"{self.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "msgtype": "m.text",
                        "body": text,
                        "format": "org.matrix.custom.html",
                        "formatted_body": f"<p>{text}</p>",
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"Matrix send error: {e}")
            return False

    async def send_media(self, room_id: str, file_path: str, mime_type: str, filename: str) -> bool:
        """Upload and send a media file to a Matrix room."""
        if not self.access_token:
            return False
        try:
            import httpx
            
            # Step 1: Upload
            async with httpx.AsyncClient() as client:
                with open(file_path, 'rb') as f:
                    upload_resp = await client.post(
                        f"{self.homeserver}/_matrix/media/v3/upload?filename={filename}",
                        headers={"Authorization": f"Bearer {self.access_token}"},
                        content=f.read(),
                        timeout=30,
                    )
                if upload_resp.status_code != 200:
                    return False
                mxc_uri = upload_resp.json().get("content_uri", "")

                # Step 2: Send message with media
                txn_id = str(int(time.time() * 1000))
                resp = await client.put(
                    f"{self.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "msgtype": "m.file" if mime_type.startswith("application/") else "m.image",
                        "body": filename,
                        "url": mxc_uri,
                        "info": {"mimetype": mime_type, "size": os.path.getsize(file_path)},
                    },
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"Matrix media send error: {e}")
            return False

    async def poll_messages(self, handler: Callable):
        """Long-poll for new messages (simplified sync)."""
        self._polling = True
        self._message_handler = handler

        while self._polling:
            try:
                import httpx
                params = {"timeout": "30000"}
                if self._since:
                    params["since"] = self._since

                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{self.homeserver}/_matrix/client/v3/sync",
                        headers={"Authorization": f"Bearer {self.access_token}"},
                        params=params,
                        timeout=60,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self._since = data.get("next_batch", self._since)

                        # Process room events
                        rooms = data.get("rooms", {}).get("join", {})
                        for room_id, room_data in rooms.items():
                            timeline = room_data.get("timeline", {}).get("events", [])
                            for event in timeline:
                                if event.get("type") == "m.room.message":
                                    if event.get("sender") != self.user_id:
                                        content = event.get("content", {})
                                        body = content.get("body", "")
                                        if body and self._message_handler:
                                            await self._message_handler({
                                                "platform": "matrix",
                                                "room_id": room_id,
                                                "sender": event["sender"],
                                                "message": body,
                                                "event_id": event["event_id"],
                                            })
            except Exception as e:
                logger.error(f"Matrix poll error: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        """Stop polling."""
        self._polling = False
