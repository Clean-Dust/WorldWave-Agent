"""ww/contacts/transports/mqtt.py — MQTT transport for agent-to-agent messaging.

Uses mosquitto_pub/sub subprocess for pub/sub messaging.
Falls back gracefully if Mosquitto tools are not installed.

Topic convention:
  contacts/<recipient_friend_code>/in   (incoming messages for a specific agent)
  contacts/<sender_friend_code>/out     (acknowledgement / response)
  contacts/broadcast                    (LAN-wide announcement)
"""

from __future__ import annotations
import json
import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("ww.contacts.transport.mqtt")

MQTT_HOST = os.environ.get("WW_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("WW_MQTT_PORT", "1883"))
MQTT_QOS = 1
MQTT_RECONNECT_INTERVAL = 5  # seconds


def _check_mosquitto() -> bool:
    """Check if mosquitto_pub is available on PATH."""
    try:
        subprocess.run(
            ["mosquitto_pub", "--help"],
            capture_output=True, timeout=3,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def send(payload: str, topic: str) -> bool:
    """Publish a message to an MQTT topic.

    Args:
        payload: JSON string to publish
        topic: MQTT topic to publish to

    Returns:
        True if publish command exited successfully
    """
    if not _check_mosquitto():
        logger.warning("mosquitto_pub not available, MQTT transport disabled")
        return False
    try:
        result = subprocess.run(
            [
                "mosquitto_pub",
                "-h", MQTT_HOST,
                "-p", str(MQTT_PORT),
                "-t", topic,
                "-m", payload,
                "-q", str(MQTT_QOS),
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("MQTT publish failed: %s", result.stderr.strip())
            return False
        return True
    except Exception as e:
        logger.debug("MQTT publish error: %s", e)
        return False


def subscribe(
    friend_code: str,
    on_message: Callable[[str], None],
    stop_event: Optional[threading.Event] = None,
):
    """Subscribe to incoming messages for this agent.

    Runs a blocking loop in the calling thread. Start in a daemon thread.

    Args:
        friend_code: This agent's friend code (8 chars)
        on_message: Callback receiving raw JSON message strings
        stop_event: Optional event to signal graceful shutdown
    """
    if not _check_mosquitto():
        logger.warning("mosquitto_sub not available, MQTT listener disabled")
        return

    topic = f"contacts/{friend_code}/in"
    topic_broadcast = "contacts/broadcast"

    cmd = [
        "mosquitto_sub",
        "-h", MQTT_HOST,
        "-p", str(MQTT_PORT),
        "-t", topic,
        "-t", topic_broadcast,
        "-q", str(MQTT_QOS),
        "-v",  # Verbose: outputs "topic payload"
    ]

    while not (stop_event and stop_event.is_set()):
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
            while not (stop_event and stop_event.is_set()):
                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    if proc.poll() is not None:
                        break
                    continue

                line = line.strip()
                if not line:
                    continue

                # Parse "topic payload"
                if " " in line:
                    topic_received, payload = line.split(" ", 1)
                else:
                    continue

                try:
                    on_message(payload)
                except Exception as e:
                    logger.warning("MQTT message handler error: %s", e)

            # Cleanup before reconnect
            proc.terminate()
            proc.wait(timeout=3)

        except Exception as e:
            logger.warning("MQTT listener error: %s", e)

        if stop_event and stop_event.is_set():
            break

        logger.debug("MQTT reconnecting in %ss...", MQTT_RECONNECT_INTERVAL)
        time.sleep(MQTT_RECONNECT_INTERVAL)


def topic_for(friend_code: str) -> str:
    """Get the MQTT topic for a specific agent's inbox."""
    return f"contacts/{friend_code}/in"
