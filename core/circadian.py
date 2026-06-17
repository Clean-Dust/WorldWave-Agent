"""
ww/core/circadian.py — Dynamic Circadian Rhythm v0.1

Biomimetic autonomic nervous system for adaptive heartbeat.

Unlike static cron (OpenClaw) or fixed-interval scheduling (Hermes),
the circadian rhythm adapts heartbeat frequency based on:
- System resource load (CPU, memory pressure)
- User interaction frequency (active vs idle)
- Project urgency (CI/CD failures, deadlines, error rates)
- Time of day (night mode → slower, work hours → faster)
- Amygdala stress signals (high stress → accelerate heartbeat)

Heartbeat ranges:
    SLEEP:   1 tick per 15-30 min  (deep idle / night)
    REST:    1 tick per 5-10 min   (light idle)
    NORMAL:  1 tick per 1-3 min    (active)
    ALERT:   1 tick per 15-30 sec  (elevated urgency)
    CRISIS:  1 tick per 5-10 sec   (CI/CD failure, deadline pressure)

Pure Python, zero external dependencies.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


# ── Heartbeat states ──

class RhythmState(Enum):
    SLEEP = "sleep"       # Deep idle / night mode
    REST = "rest"          # Light idle
    NORMAL = "normal"      # Active engagement
    ALERT = "alert"        # Elevated urgency
    CRISIS = "crisis"      # Emergency / deadline pressure


# State → interval in seconds
STATE_INTERVALS = {
    RhythmState.SLEEP:  900,    # 15 min
    RhythmState.REST:   300,    # 5 min
    RhythmState.NORMAL: 120,    # 2 min
    RhythmState.ALERT:  30,     # 30 sec
    RhythmState.CRISIS: 8,      # 8 sec
}


# State → description
STATE_DESCRIPTIONS = {
    RhythmState.SLEEP:  "Deep idle — memory consolidation active",
    RhythmState.REST:   "Light idle — monitoring only",
    RhythmState.NORMAL: "Active — standard heartbeat",
    RhythmState.ALERT:  "Elevated — increased monitoring",
    RhythmState.CRISIS: "Emergency — maximum vigilance",
}


@dataclass
class SystemMetrics:
    """Current system state snapshot for rhythm calculation."""
    timestamp: float = field(default_factory=time.time)
    cpu_percent: float = 0.0       # 0-100 (estimated)
    memory_percent: float = 0.0    # 0-100 (estimated)
    active_tasks: int = 0          # Active spiral tasks
    error_count_recent: int = 0    # Errors in last 5 min
    last_user_interaction: float = 0.0  # Timestamp
    ci_status: str = "unknown"     # "passing", "failing", "unknown"
    stress_signal: float = 0.0     # From amygdala (0-1)
    context_pressure: float = 0.0  # Context window utilization


class CircadianRhythm:
    """Adaptive heartbeat controller for WW's autonomic nervous system.

    Dynamically adjusts the scheduler's tick frequency based on
    environmental signals, matching the biological circadian rhythm concept.

    Usage:
        rhythm = CircadianRhythm()
        rhythm.update_metrics(SystemMetrics(cpu_percent=45, error_count_recent=2))
        state = rhythm.current_state  # NORMAL / ALERT / etc.
        interval = rhythm.heartbeat_interval  # seconds
    """

    def __init__(
        self,
        night_start_hour: int = 23,   # 11 PM
        night_end_hour: int = 7,      # 7 AM
        idle_timeout: float = 600,    # 10 min no interaction → REST
        deep_idle_timeout: float = 3600,  # 1 hour → SLEEP
        crisis_error_threshold: int = 5,  # 5+ errors in window → CRISIS
        alert_error_threshold: int = 2,   # 2+ errors → ALERT
    ):
        self.night_start = night_start_hour
        self.night_end = night_end_hour
        self.idle_timeout = idle_timeout
        self.deep_idle_timeout = deep_idle_timeout
        self.crisis_error_threshold = crisis_error_threshold
        self.alert_error_threshold = alert_error_threshold

        self._current_state: RhythmState = RhythmState.NORMAL
        self._previous_state: RhythmState = RhythmState.NORMAL
        self._state_entered_at: float = time.time()
        self._heartbeat_interval: float = STATE_INTERVALS[RhythmState.NORMAL]
        self._last_tick: float = 0.0
        self._tick_count: int = 0

        # Metric history
        self._metrics_history: List[SystemMetrics] = []
        self._last_metrics: Optional[SystemMetrics] = None

        # Callbacks
        self._on_state_change: Optional[Callable] = None
        self._on_tick: Optional[Callable] = None

        # Night mode override
        self._force_night: bool = False

        # Stats
        self._state_durations: Dict[RhythmState, float] = {
            s: 0.0 for s in RhythmState
        }

    # ── Core rhythm computation ──

    def update_metrics(self, metrics: SystemMetrics):
        """Feed new system metrics and recompute rhythm state."""
        metrics.timestamp = metrics.timestamp or time.time()
        self._last_metrics = metrics
        self._metrics_history.append(metrics)

        # Keep last 100 samples (~15 min at normal rate)
        if len(self._metrics_history) > 100:
            self._metrics_history = self._metrics_history[-60:]

        new_state = self._compute_state(metrics)
        if new_state != self._current_state:
            self._previous_state = self._current_state
            # Accumulate time in previous state
            elapsed = time.time() - self._state_entered_at
            self._state_durations[self._current_state] += elapsed
            self._current_state = new_state
            self._state_entered_at = time.time()
            self._heartbeat_interval = STATE_INTERVALS[new_state]

            if self._on_state_change:
                self._on_state_change(self._previous_state, new_state)

    def _compute_state(self, m: SystemMetrics) -> RhythmState:
        """Compute rhythm state from current metrics.

        Priority order (higher overrides lower):
        1. Night mode → SLEEP
        2. Crisis signals → CRISIS
        3. Error spikes → ALERT
        4. User idle → REST or SLEEP
        5. Default → NORMAL
        """
        now = time.time()

        # ── Night mode ──
        if self._is_night_time() or self._force_night:
            # Even at night, crisis can override
            if m.error_count_recent >= self.crisis_error_threshold:
                return RhythmState.CRISIS
            if m.stress_signal > 0.8:
                return RhythmState.ALERT
            return RhythmState.SLEEP

        # ── Crisis detection ──
        if m.error_count_recent >= self.crisis_error_threshold:
            return RhythmState.CRISIS
        if m.ci_status == "failing":
            return RhythmState.CRISIS
        if m.stress_signal > 0.9:
            return RhythmState.CRISIS

        # ── Alert detection ──
        if m.error_count_recent >= self.alert_error_threshold:
            return RhythmState.ALERT
        if m.context_pressure > 0.85:
            return RhythmState.ALERT
        if m.stress_signal > 0.6:
            return RhythmState.ALERT

        # ── Idle detection ──
        time_since_interaction = now - m.last_user_interaction
        if time_since_interaction > self.deep_idle_timeout:
            return RhythmState.SLEEP
        if time_since_interaction > self.idle_timeout:
            return RhythmState.REST

        # ── Resource pressure ──
        if m.cpu_percent > 90 or m.memory_percent > 90:
            return RhythmState.ALERT  # System under load → watch closely

        # ── Default ──
        return RhythmState.NORMAL

    def _is_night_time(self) -> bool:
        """Check if current time falls within night window."""
        from datetime import datetime
        hour = datetime.now().hour
        if self.night_start > self.night_end:
            # Overnight window: e.g., 23:00 - 07:00
            return hour >= self.night_start or hour < self.night_end
        else:
            return self.night_start <= hour < self.night_end

    # ── Tick interface ──

    def should_tick(self) -> bool:
        """Check if it's time for the next heartbeat tick."""
        now = time.time()
        if now - self._last_tick >= self._heartbeat_interval:
            self._last_tick = now
            self._tick_count += 1
            if self._on_tick:
                self._on_tick(self._current_state)
            return True
        return False

    def tick(self):
        """Force a tick (caller's responsibility to check should_tick first)."""
        self._last_tick = time.time()
        self._tick_count += 1

    # ── Properties ──

    @property
    def current_state(self) -> RhythmState:
        return self._current_state

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval

    @property
    def state_description(self) -> str:
        return STATE_DESCRIPTIONS.get(self._current_state, "unknown")

    # ── Control ──

    def force_night_mode(self, enabled: bool = True):
        """Manually enable/disable night mode (for testing)."""
        self._force_night = enabled

    def set_state(self, state: RhythmState):
        """Manually override state (for emergency use)."""
        self._previous_state = self._current_state
        self._current_state = state
        self._state_entered_at = time.time()
        self._heartbeat_interval = STATE_INTERVALS[state]

    def set_callbacks(self, on_state_change: Optional[Callable] = None,
                      on_tick: Optional[Callable] = None):
        """Register callbacks for state changes and ticks."""
        self._on_state_change = on_state_change
        self._on_tick = on_tick

    # ── Stats ──

    def stats(self) -> Dict:
        now = time.time()
        current_duration = now - self._state_entered_at
        durations = dict(self._state_durations)
        durations[self._current_state] += current_duration

        return {
            "current_state": self._current_state.value,
            "state_description": self.state_description,
            "heartbeat_interval_seconds": round(self._heartbeat_interval, 1),
            "time_in_current_state": round(current_duration, 1),
            "total_ticks": self._tick_count,
            "night_mode": self._is_night_time() or self._force_night,
            "state_durations": {
                s.value: round(d, 1) for s, d in durations.items()
            },
            "last_metrics": {
                "cpu": self._last_metrics.cpu_percent if self._last_metrics else 0,
                "memory": self._last_metrics.memory_percent if self._last_metrics else 0,
                "errors_recent": self._last_metrics.error_count_recent if self._last_metrics else 0,
                "stress": self._last_metrics.stress_signal if self._last_metrics else 0,
            } if self._last_metrics else {},
        }


# ── System metric collector ──

def collect_system_metrics(
    active_tasks: int = 0,
    error_count_recent: int = 0,
    last_user_interaction: float = 0.0,
    ci_status: str = "unknown",
    stress_signal: float = 0.0,
    context_pressure: float = 0.0,
) -> SystemMetrics:
    """Collect current system metrics (cross-platform).

    Attempts to read real CPU/memory if on Linux, otherwise uses estimates.
    """
    cpu = 0.0
    mem = 0.0

    # Try to read real system metrics
    try:
        # CPU usage via /proc/stat (simple two-sample would be better,
        # but single-sample gives a rough estimate)
        with open("/proc/stat") as f:
            line = f.readline()
            parts = line.split()
            if len(parts) >= 5:
                idle = float(parts[4])
                total = sum(float(p) for p in parts[1:])
                if total > 0:
                    cpu = 100.0 * (1.0 - idle / total)
    except (FileNotFoundError, PermissionError, ValueError):
        cpu = 0.0

    try:
        with open("/proc/meminfo") as f:
            total_mem = 0
            avail_mem = 0
            for line in f:
                if line.startswith("MemTotal:"):
                    total_mem = float(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_mem = float(line.split()[1])
            if total_mem > 0:
                mem = 100.0 * (1.0 - avail_mem / total_mem)
    except (FileNotFoundError, PermissionError, ValueError):
        mem = 0.0

    return SystemMetrics(
        timestamp=time.time(),
        cpu_percent=round(cpu, 1),
        memory_percent=round(mem, 1),
        active_tasks=active_tasks,
        error_count_recent=error_count_recent,
        last_user_interaction=last_user_interaction or time.time(),
        ci_status=ci_status,
        stress_signal=stress_signal,
        context_pressure=context_pressure,
    )


# ── Factory ──

def create_circadian_rhythm(**kwargs) -> CircadianRhythm:
    return CircadianRhythm(**kwargs)
