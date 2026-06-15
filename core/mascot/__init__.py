"""core/mascot/__init__.py — Worldwave Mascot Manager

Fat Shark is the state display system.
Manage WW's emotion state, push to client via SSE.

State cycle:
  idle → thinking → (success → happy) | (failure → sad) → idle
  idle → excited (special events)
  idle → sleep (extended idle, 60s)
  any → error → idle
"""

import json
import time
import threading
import logging
from typing import Optional, Callable

logger = logging.getLogger("ww.mascot")

MASCOT_STATES = {
    "idle":     "waiting...",
    "thinking": "thinking...",
    "happy":    "done! ✨",
    "sad":      "failed... ",
    "excited":  "amazing! 🎉",
    "sleep":    "zzz...",
    "error":    "error! 💥",
}

# Temporary state will auto return to idle (seconds)
AUTO_IDLE_TIMEOUTS = {
    "happy":   3.5,
    "sad":     4.0,
    "excited": 4.0,
    "error":   3.0,
}


class MascotManager:
    """management Mascot state + SSE broadcast. """

    def __init__(self):
        self._state = "idle"
        self._message = "waiting..."
        self._last_activity = time.time()
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []
        self._sleep_checker: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        """Start background sleep detection."""
        if self._running:
            return
        self._running = True
        self._sleep_checker = threading.Thread(
            target=self._sleep_monitor, daemon=True, name="mascot-sleep"
        )
        self._sleep_checker.start()
        logger.info("Mascot started")

    def stop(self):
        self._running = False

    def set_state(self, state: str, message: Optional[str] = None):
        """Switch state and notify all subscribers."""
        if state not in MASCOT_STATES:
            logger.warning(f"Unknown mascot state: {state}")
            return

        with self._lock:
            old_state = self._state
            self._state = state
            self._message = message or MASCOT_STATES.get(state, state)
            self._last_activity = time.time()

        if old_state != state:
            logger.info(f"Mascot: {old_state} → {state} — {self._message}")

        self._broadcast({"state": state, "message": self._message})

        # Temporary state: scheduled auto return to idle
        timeout = AUTO_IDLE_TIMEOUTS.get(state)
        if timeout:

            def _auto_idle():
                time.sleep(timeout)
                with self._lock:
                    if self._state == state:
                        self._state = "idle"
                        self._message = "waiting..."
                        self._last_activity = time.time()
                self._broadcast({"state": "idle", "message": "waiting..."})

            threading.Thread(target=_auto_idle, daemon=True).start()

    def get_state(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "message": self._message,
                "last_activity": self._last_activity,
                "idle_seconds": round(time.time() - self._last_activity, 1),
            }

    # ── Event-driven hooks ──

    def on_task_start(self, task: str = ""):
        """Receive task trigger."""
        self.set_state("thinking", f"working... {task[:40]}" if task else "thinking...")

    def on_task_complete(self, success: bool, result: str = ""):
        """Task completion trigger."""
        if success:
            self.set_state("happy")
        else:
            self.set_state("sad", f"failed: {result[:40]}" if result else "failed...")

    def on_error(self, error: str = ""):
        """System error trigger."""
        self.set_state("error", f"error: {error[:40]}" if error else "error!")

    def on_idle(self):
        """Idle trigger (external call)."""
        self.set_state("idle")

    def on_wake(self):
        """from sleeparousal. """
        self.set_state("idle", "awake!")

    # ── SSE ──

    def subscribe(self, callback: Callable[[dict], None]):
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict], None]):
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _broadcast(self, data: dict):
        dead = []
        for cb in self._subscribers:
            try:
                cb(data)
            except Exception as e:
                logger.warning(f"Mascot subscriber error: {e}")
                dead.append(cb)
        for cb in dead:
            self.unsubscribe(cb)

    # ── Sleep detection ──

    def _sleep_monitor(self):
        while self._running:
            time.sleep(15)
            with self._lock:
                idle = time.time() - self._last_activity
            if idle > 90 and self._state not in ("sleep", "thinking", "error"):
                self.set_state("sleep")
            elif idle < 30 and self._state == "sleep":
                self.on_wake()

    def poke(self):
        """User/system active arousal."""
        self._last_activity = time.time()
        if self._state == "sleep":
            self.on_wake()


# ── Global instance ──
mascot = MascotManager()
