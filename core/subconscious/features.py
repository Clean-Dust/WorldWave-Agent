"""
ww/core/subconscious/features.py — subconscious feature extraction 

Main consciousness LLM dialogue completely ignored. Subconscious only looks at a numerical state vector
purely describing WW internal operational health.

|Feature Vector (12 core + 3 provider + 4 system + 4 new + 2 metacognitive = 25 dimensions, padded to 32):|  
  0: consecutive_errors       — consecutive error count  
  1: tool_call_loop_count     — number of calls to the same tool within the last 5 steps  
  2: api_latency_avg          — LLM API average response latency (seconds)  
  3: api_latency_trend        — latency trend: -1 decreasing, 0 stable, 1 increasing  
  4: token_consumption_rate   — tokens consumed per spiral  
  5: spirals_completed        — total completed spirals  
  6: current_phase_id         — current spiral phase (0-5)  
  7: last_action_success      — whether the last step was successful (0/1)  
  8: tool_diversity           — number of different tools in the last 10 steps  
  9: memory_recall_count      — session memory recall count  
  10: llm_response_empty      — LLM returns empty (0/1)  
  11: time_since_checkpoint   — seconds since last checkpoint  

One-hot Provider Encoding (3 dimensions, indices 12-14):
  12: provider_deepseek       — 1 if use DeepSeek
  13: provider_openai_other   — 1 if use OpenAI or its compatible API
  14: provider_anthropic      — 1 if use Anthropic Claude

System Resource Features (4 dimensions, indices 15-18):
  15: cpu_load_1m             — 1-minute load average / num_cores (0.0 ~ N)
  16: mem_free_ratio          — MemAvailable / MemTotal (0.0 ~ 1.0)
  17: context_window_pressure — context window usage ratio (0.0 ~ 1.0), set externally
  18: error_pattern_heterogeneity — entropy of error type distribution over last 10 steps (NEW)

Cognitive Stress Features (4 dimensions, indices 19-22, NEW):
  19: memory_conflict_rate    — ratio of contradictory facts in memory graph (0.0 ~ 1.0)
  20: context_info_density    — ratio of meaningful vs boilerplate tokens (0.0 ~ 1.0)
  21: syscall_anomaly_index   — OS-level anomaly indicator (placeholder, 0.0)
  22: thinking_tokens_ratio   — ratio of <thinking> tags in output (moved from slot 23)

Reserved metacognitive probes (slots 23, for future LLM internal API):
  23: hidden_state_norm       — monitor activation collapse (0.0 until LLM API available)

Reserved dimensions (indices 24-31) for future upgrades, always padding 0.0.

This allows subconscious to learn and isolate optimization strategy at different run states,
and does not need to understand language.
"""

from __future__ import annotations
import math
import time
import os
from typing import Any, Dict, List, Optional, Tuple


FEATURE_NAMES = [
    # Core runtime metrics (12)
    "consecutive_errors",
    "tool_call_loop_count",
    "api_latency_avg",
    "api_latency_trend",
    "token_consumption_rate",
    "spirals_completed",
    "current_phase_id",
    "last_action_success",
    "tool_diversity",
    "memory_recall_count",
    "llm_response_empty",
    "time_since_checkpoint",
    # Provider one-hot (3)
    "provider_deepseek",
    "provider_openai_other",
    "provider_anthropic",
    # System resources (4)
    "cpu_load_1m",
    "mem_free_ratio",
    "context_window_pressure",
    "error_pattern_heterogeneity",  # NEW: error type distribution entropy
    # Cognitive stress features (4)
    "memory_conflict_rate",        # NEW: contradictory facts ratio
    "context_info_density",        # NEW: meaningful vs boilerplate token ratio
    "syscall_anomaly_index",       # NEW: OS anomaly placeholder
    "thinking_tokens_ratio",       # moved from slot 23
    # Reserved metacognitive probe (1)
    "hidden_state_norm",           # monitor activation collapse (0.0 until LLM API)
]

# ── Reserved dimension padding (Forward Compatibility) ──
PADDED_FEATURES = 32

# complete 32-dimensional feature names (reserved slots named reserved_19 ~ reserved_31)
ALL_FEATURE_NAMES = list(FEATURE_NAMES) + [
    f"reserved_{i}" for i in range(len(FEATURE_NAMES), PADDED_FEATURES)
]

# normalization scope (for training feature scaling)
FEATURE_RANGES = {
    "consecutive_errors": (0, 20),
    "tool_call_loop_count": (0, 10),
    "api_latency_avg": (0, 60),
    "api_latency_trend": (-1, 1),
    "token_consumption_rate": (0, 50000),
    "spirals_completed": (0, 1000),
    "current_phase_id": (0, 5),
    "last_action_success": (0, 1),
    "tool_diversity": (0, 30),
    "memory_recall_count": (0, 100),
    "llm_response_empty": (0, 1),
    "time_since_checkpoint": (0, 3600),
    "provider_deepseek": (0, 1),
    "provider_openai_other": (0, 1),
    "provider_anthropic": (0, 1),
    # System resource ranges
    "cpu_load_1m": (0, 4),
    "mem_free_ratio": (0, 1),
    "context_window_pressure": (0, 1),
    "error_pattern_heterogeneity": (0, 1),    # NEW: entropy normalized to [0, 1]
    # Cognitive stress ranges
    "memory_conflict_rate": (0, 1),
    "context_info_density": (0, 1),
    "syscall_anomaly_index": (0, 1),
    "thinking_tokens_ratio": (0, 1),
    # Metacognitive probe range
    "hidden_state_norm": (0, 100),
}

# Reserved dimension scope
for i in range(len(FEATURE_NAMES), PADDED_FEATURES):
    FEATURE_RANGES[f"reserved_{i}"] = (0, 1)

NUM_FEATURES = len(FEATURE_NAMES)  # 24
NUM_CORE_FEATURES = 12

# ── System resource helpers ──

_NUM_CORES: Optional[int] = None

def _num_cores() -> int:
    global _NUM_CORES
    if _NUM_CORES is None:
        try:
            _NUM_CORES = os.cpu_count() or 1
        except Exception:
            _NUM_CORES = 1
    return _NUM_CORES

def _read_proc_loadavg() -> float:
    """Read 1-minute load average from /proc/loadavg."""
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().split()
            return float(parts[0]) if parts else 0.0
    except Exception:
        return 0.0

def _read_proc_meminfo() -> Tuple[float, float]:
    """Read MemAvailable and MemTotal from /proc/meminfo. Returns (available_kb, total_kb)."""
    try:
        available = 0.0
        total = 0.0
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    available = float(line.split()[1])
                elif line.startswith("MemTotal:"):
                    total = float(line.split()[1])
                if available and total:
                    break
        return available, total
    except Exception:
        return 0.0, 1.0  # Safe defaults


# ── Padding helper function ──

def pad_vector(vec: list) -> list:
    """will pad/truncate any length vector to PADDED_FEATURES dimensions."""
    padded = list(vec[:PADDED_FEATURES])
    while len(padded) < PADDED_FEATURES:
        padded.append(0.0)
    return padded

def is_padded_index(idx: int) -> bool:
    """is whether it is a reserved padding slot."""
    return idx >= NUM_FEATURES

# LLM Provider mapping (one-hot encoding)
LLM_PROVIDER_ENCODING = {
    "deepseek": [1.0, 0.0, 0.0],
    "openai": [0.0, 1.0, 0.0],
    "anthropic": [0.0, 0.0, 1.0],
}


class FeatureExtractor:
    """
    from WW internal state retrieve feature vector.

    Does not read any dialogue content, only reads numerical value state.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Reset session-level state."""
        self._provider_id: str = ""
        self._tool_history: List[str] = []        # Last 10 steps of tool calls
        self._error_history: List[bool] = []       # Last 10 steps success/failed
        self._error_type_history: List[str] = []   # Error types (for heterogeneity)
        self._latency_history: List[float] = []    # Last 5 steps latency
        self._token_history: List[int] = []        # Token consumption per spiral
        self._success_count = 0
        self._failure_count = 0
        self._last_checkpoint_time = time.time()
        self._recall_count = 0
        self._start_time = time.time()
        # External state setters
        self._context_window_pressure: float = 0.0
        self._memory_conflict_rate: float = 0.0   # NEW
        self._context_info_density: float = 0.0   # NEW
        self._cpu_load_avg: float = 0.0
        self._mem_free_ratio: float = 0.5
        # Metacognitive probe values (set externally by plugin system)
        self._probe_values: Dict[str, float] = {
            "thinking_tokens_ratio": 0.0,
            "hidden_state_norm": 0.0,
            "token_entropy": 0.0,
        }

    def set_provider(self, provider_id: str):
        """
        setting when LLM provider.

        Args:
            provider_id: "deepseek", "openai", "anthropic", or others
                        Unregistered provider will encode as [0, 1, 0] (other/OpenAI compatible)
        """
        self._provider_id = provider_id

    def set_context_window_pressure(self, ratio: float):
        """
        Set the current context window usage ratio (0.0 ~ 1.0).
        Called externally from the spiral loop when context length is known.
        """
        self._context_window_pressure = max(0.0, min(1.0, ratio))

    def set_memory_conflict_rate(self, rate: float):
        """Set memory conflict ratio (0.0 ~ 1.0). Called externally from memory system."""
        self._memory_conflict_rate = max(0.0, min(1.0, rate))

    def set_context_info_density(self, ratio: float):
        """Set context info density (0.0 ~ 1.0). Called externally from spiral loop."""
        self._context_info_density = max(0.0, min(1.0, ratio))

    def set_probe_value(self, name: str, value: float):
        """Set a metacognitive probe value (e.g. 'token_entropy', 'hidden_state_norm')."""
        self._probe_values[name] = max(0.0, min(1.0, value))

    def observe_action(self, tool_name: str, success: bool,
                       latency: float = 0.0, token_count: int = 0,
                       error_type: Optional[str] = None):
        """Record one tool call.
        
        Args:
            tool_name: Name of the tool called.
            success: Whether the call succeeded.
            latency: API latency in seconds.
            token_count: Token count consumed.
            error_type: Optional error category for heterogeneity tracking.
                        If None, derived from tool_name when success=False.
        """
        self._tool_history.append(tool_name)
        if len(self._tool_history) > 10:
            self._tool_history.pop(0)

        self._error_history.append(success)
        if len(self._error_history) > 10:
            self._error_history.pop(0)

        # Error type tracking for heterogeneity
        if not success:
            err_type = error_type or tool_name.split("/")[-1].split(".")[0]
            self._error_type_history.append(err_type)
            if len(self._error_type_history) > 10:
                self._error_type_history.pop(0)

        if latency > 0:
            self._latency_history.append(latency)
            if len(self._latency_history) > 5:
                self._latency_history.pop(0)

        if token_count > 0:
            self._token_history.append(token_count)
            if len(self._token_history) > 10:
                self._token_history.pop(0)

        if success:
            self._success_count += 1
        else:
            self._failure_count += 1

    def observe_memory_recall(self):
        """Record one memory recall."""
        self._recall_count += 1

    def notify_checkpoint(self):
        """record checkpoint."""
        self._last_checkpoint_time = time.time()

    def extract(
        self,
        spirals_completed: int = 0,
        current_phase_id: int = 0,
        llm_returned_empty: bool = False,
    ) -> List[float]:
        """Retrieve the 19-dimensional state vector (padded to 32)."""
        # 0: consecutive_errors
        consecutive = 0
        for s in reversed(self._error_history):
            if not s:
                consecutive += 1
            else:
                break

        # 1: tool_call_loop_count — Most common tool count in last 5 steps
        recent_tools = self._tool_history[-5:] if self._tool_history else []
        tool_loop = 0
        if recent_tools:
            from collections import Counter
            tool_loop = max(Counter(recent_tools).values())

        # 2: api_latency_avg
        latency_avg = (sum(self._latency_history) / max(1, len(self._latency_history))
                       if self._latency_history else 0.0)

        # 3: api_latency_trend
        latency_trend = 0
        if len(self._latency_history) >= 3:
            half = len(self._latency_history) // 2
            first_half = sum(self._latency_history[:half]) / max(1, half)
            second_half = sum(self._latency_history[half:]) / max(1, len(self._latency_history) - half)
            diff = second_half - first_half
            latency_trend = 1 if diff > 1.0 else (-1 if diff < -1.0 else 0)

        # 4: token_consumption_rate
        token_rate = (sum(self._token_history) / max(1, len(self._token_history))
                      if self._token_history else 0)

        # 5: spirals_completed
        spirals = spirals_completed

        # 6: current_phase_id
        phase = current_phase_id

        # 7: last_action_success
        last_ok = 1 if (self._error_history and self._error_history[-1]) else 0

        # 8: tool_diversity — Number of different tools in last 10 steps
        diversity = len(set(self._tool_history))

        # 9: memory_recall_count
        recall = self._recall_count

        # 10: llm_response_empty
        empty = 1 if llm_returned_empty else 0

        # 11: time_since_checkpoint
        time_since = time.time() - self._last_checkpoint_time

        # ── System resource features (live read) ──
        load_1m = _read_proc_loadavg()
        nc = _num_cores()
        cpu_norm = load_1m / max(1, nc)

        mem_avail_kb, mem_total_kb = _read_proc_meminfo()
        mem_free = mem_avail_kb / max(1, mem_total_kb) if mem_total_kb > 0 else 0.5

        ctx_pressure = self._context_window_pressure

        # 18: error_pattern_heterogeneity — entropy of error type distribution
        err_heterogeneity = 0.0
        if len(self._error_type_history) >= 3:
            from collections import Counter
            err_counts = Counter(self._error_type_history)
            total = len(self._error_type_history)
            # Shannon entropy over error type distribution (normalized to [0, 1])
            entropy = sum(-(c / total) * math.log(c / total)
                          for c in err_counts.values() if c > 0)
            max_entropy = math.log(len(err_counts)) if len(err_counts) > 1 else 1.0
            err_heterogeneity = min(1.0, entropy / max_entropy) if max_entropy > 0 else 0.0

        # 19: memory_conflict_rate — set externally
        mem_conflict = self._memory_conflict_rate

        # 20: context_info_density — set externally
        info_density = self._context_info_density

        # 21: syscall_anomaly_index — placeholder (always 0.0 for now)
        syscall_anomaly = 0.0

        return [
            # Core metrics (0-11)
            float(consecutive),
            float(tool_loop),
            round(latency_avg, 2) if latency_avg > 0 else 0.0,
            float(latency_trend),
            float(token_rate) if token_rate > 0 else 0.0,
            float(spirals),
            float(phase),
            float(last_ok),
            float(diversity),
            float(recall),
            float(empty),
            round(time_since, 1),
            # Provider one-hot (12-14)
        ] + self._provider_encode() + [
            # System resources (15-18)
            round(cpu_norm, 3),
            round(mem_free, 3),
            round(ctx_pressure, 3),
            round(err_heterogeneity, 3),  # NEW
            # Cognitive stress features (19-22)
            round(mem_conflict, 3),
            round(info_density, 3),
            round(syscall_anomaly, 3),
            self._probe_values.get("thinking_tokens_ratio", 0.0),  # 22
            # Metacognitive probe (23)
            self._probe_values.get("hidden_state_norm", 0.0),  # 23
            # Padding (24-31)
        ] + [0.0] * (PADDED_FEATURES - NUM_FEATURES)

    def _provider_encode(self) -> List[float]:
        """Convert provider_id to one-hot encode."""
        default = [0.0, 1.0, 0.0]  # default is other/OpenAI compatible
        return LLM_PROVIDER_ENCODING.get(self._provider_id, default)

    def normalize(self, vector: List[float]) -> List[float]:
        """Normalize to [0, 1] scope (for model training).

        Supports any length vector (including reserved padding dimensions, normalize as identity mapping).
        """
        normed = []
        for i, val in enumerate(vector):
            if i < len(FEATURE_NAMES):
                lo, hi = FEATURE_RANGES[FEATURE_NAMES[i]]
            else:
                # Reserved dimension: identity mapping [0, 1]
                lo, hi = 0.0, 1.0
            if hi - lo == 0:
                normed.append(0.5)
            else:
                normed.append(max(0.0, min(1.0, (val - lo) / (hi - lo))))
        return normed

    def stats(self) -> Dict[str, Any]:
        """Feature extractor internal statistics."""
        return {
            "provider": self._provider_id or "unset",
            "observations": len(self._tool_history),
            "successes": self._success_count,
            "failures": self._failure_count,
            "total_recalls": self._recall_count,
            "unique_tools": len(set(self._tool_history)),
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "context_window_pressure": round(self._context_window_pressure, 3),
        }
