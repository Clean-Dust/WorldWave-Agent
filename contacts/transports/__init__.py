"""ww/contacts/transports/ — Message transport layer.

Picks the best available transport for delivering agent-to-agent messages.
Priority: HTTP (direct) > MQTT (broker) > Queue (offline)
"""

from __future__ import annotations
import logging
from typing import Optional

from . import http
from . import mqtt

logger = logging.getLogger("ww.contacts.transport")

# Cache whether MQTT is available (check once)
_MQTT_AVAILABLE: Optional[bool] = None


def mqtt_available() -> bool:
    """Check if MQTT transport is usable."""
    global _MQTT_AVAILABLE
    if _MQTT_AVAILABLE is None:
        _MQTT_AVAILABLE = mqtt._check_mosquitto()  # noqa
    return _MQTT_AVAILABLE


def get_available_transports() -> list:
    """List available transport methods."""
    transports = ["http"]
    if mqtt_available():
        transports.append("mqtt")
    return transports


__all__ = ["http", "mqtt", "mqtt_available", "get_available_transports"]
