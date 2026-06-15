"""ww/contacts/transports/http.py — HTTP transport for agent-to-agent messaging.

Sends AgentMessage JSON payloads to peer's /contacts/inbox endpoint.
Auto-discovers peer IP from discovery module, falls back to relay.
"""

from __future__ import annotations
import json
import logging
from typing import Optional

logger = logging.getLogger("ww.contacts.transport.http")

DEFAULT_WW_PORT = 9300


def send(payload: str, peer_ip: str, peer_port: int = 0) -> bool:
    """Send a message to a peer via direct HTTP POST.

    Args:
        payload: JSON string of AgentMessage
        peer_ip: Peer's IP address
        peer_port: Peer's port (0 = use default WW port)

    Returns:
        True if delivery succeeded (HTTP 200/202)
    """
    port = peer_port if peer_port and peer_port != 9420 else DEFAULT_WW_PORT
    try:
        import httpx
        url = f"http://{peer_ip}:{port}/contacts/inbox"
        resp = httpx.post(
            url,
            content=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        return resp.status_code in (200, 202)
    except Exception as e:
        logger.debug("HTTP send to %s:%s failed: %s", peer_ip, port, e)
        return False
