"""
ww/core/subconscious/pow.py — lightweight PoW (anti-Sybil attack)

not blockchain mining. It is 'proof of voice':
each subconscious model update when broadcast to P2P network, local must first solve a cryptographic puzzle.

Difficulty auto-adjusts, target solving time 5-10 seconds.
Use SHA256 to find hash prefix with leading zeros.

Normal user broadcasts 1-3 times a day → 10-30 seconds of computation, completely acceptable.
Sybil attack requires sending 10,000 malicious updates → needs ~28 hours of CPU computation.
"""

from __future__ import annotations
import hashlib
import json
import math
import os
import struct
import time
from typing import Any, Dict, Optional, Tuple

# ── Difficulty Constants ──

# Initial difficulty: require hash first 16 bits = 0 (i.e., starting with 0x0000)
#   1 in 65536 probability, average ~65K SHA256 attempts
#   Modern CPU about 3-5 seconds
INITIAL_DIFFICULTY_BITS = 16

# Difficulty adjustment target (seconds)
TARGET_SOLVE_TIME = 8.0  # target 8 seconds
MIN_SOLVE_TIME = 3.0     # below 3 seconds → increase difficulty
MAX_SOLVE_TIME = 15.0    # above 15 seconds → decrease difficulty

# Difficulty boundary
MIN_DIFFICULTY_BITS = 8    # minimum: 2^8 = 256 attempts, ~0.01 seconds (new node first connection)
MAX_DIFFICULTY_BITS = 28   # maximum: 2^28 = 268 million attempts, ~3 hours (prevent ASIC)

POW_DIR = os.path.expanduser("~/worldwave/data/subconscious/pow")


def compute_hash(data: bytes, nonce: int) -> str:
    """Compute double-SHA256 of data + nonce."""
    raw = data + struct.pack(">Q", nonce)
    return hashlib.sha256(hashlib.sha256(raw).digest()).hexdigest()


def check_nonce(data: bytes, nonce: int, target_prefix: str) -> bool:
    """Validate whether nonce meets difficulty requirement."""
    h = compute_hash(data, nonce)
    return h.startswith(target_prefix)


def solve(data: bytes, difficulty_bits: int,
          timeout_s: float = 30.0) -> Optional[Tuple[int, str, int, float]]:
    """
    PoW solving: find a nonce such that hash starts with difficulty_bits zeros.

    Args:
        data: payload to protect
        difficulty_bits: requires N bits to be 0
        timeout_s: timeout seconds

    Returns:
        (nonce, hash) or None (timeout)
    """
    target_prefix = "0" * ((difficulty_bits + 3) // 4)  # bits → hex chars
    # More precise: only count leading zero bits
    # but using hex prefix is enough (conservative, requires more zeros)

    nonce = 0
    start = time.time()
    attempts = 0

    while time.time() - start < timeout_s:
        h = compute_hash(data, nonce)
        attempts += 1
        if h.startswith(target_prefix):
            elapsed = time.time() - start
            return (nonce, h, attempts, elapsed)
        nonce += 1

    return None  # timeout


def verify(data: bytes, nonce: int, difficulty_bits: int,
           expected_hash: str = "") -> bool:
    """
    validate whether PoW is correct.

    Args:
        data: payload
        nonce: the nonce obtained from solving
        difficulty_bits: difficulty (N bits to be 0)
        expected_hash: optional, validate hash consistency

    Returns:
        bool
    """
    h = compute_hash(data, nonce)
    target_prefix = "0" * ((difficulty_bits + 3) // 4)

    if not h.startswith(target_prefix):
        return False
    if expected_hash and h != expected_hash:
        return False
    return True


def estimate_attempts(difficulty_bits: int) -> float:
    """Estimate average number of attempts needed."""
    return 2.0 ** difficulty_bits


def estimate_time(difficulty_bits: int,
                  hash_rate: float = 200_000) -> float:
    """
    Estimate solving time (seconds).

    Args:
        difficulty_bits: difficulty bits
        hash_rate: hashes per second (default 200K = modern CPU ~8 threads)

    Returns:
        estimated seconds
    """
    return estimate_attempts(difficulty_bits) / hash_rate


# ════════════════════════════════════════════════════════════════
#  Self-adaptive difficulty adjustment
# ════════════════════════════════════════════════════════════════


class DifficultyAdjuster:
    """
    Self-adaptive difficulty adjustment.

    Record each solving time, calculate moving average, adjust difficulty to make solving time close to target.
    """

    def __init__(
        self,
        initial_bits: int = INITIAL_DIFFICULTY_BITS,
        target: float = TARGET_SOLVE_TIME,
        min_bits: int = MIN_DIFFICULTY_BITS,
        max_bits: int = MAX_DIFFICULTY_BITS,
        window_size: int = 5,
    ):
        self.bits = initial_bits
        self.target = target
        self.min_bits = min_bits
        self.max_bits = max_bits
        self.window_size = window_size

        # Historical solving times (seconds)
        self._solve_times: list = []
        self._last_adjustment = time.time()

        # Persistence path
        os.makedirs(POW_DIR, exist_ok=True)
        self._load()

    def record_solve(self, elapsed_s: float):
        """Record one solving time consumption."""
        self._solve_times.append(elapsed_s)
        if len(self._solve_times) > self.window_size:
            self._solve_times.pop(0)

        # Adjust every 3 times
        if len(self._solve_times) >= 3:
            self._adjust()
        self._save()

    def _adjust(self):
        """Adjust difficulty based on moving average."""
        if not self._solve_times:
            return

        avg = sum(self._solve_times) / len(self._solve_times)

        if avg < MIN_SOLVE_TIME:
            # Too easy → increase difficulty
            increase = max(1, int((self.target / max(avg, 0.1)) ** 0.5))
            self.bits = min(self.max_bits, self.bits + increase)
        elif avg > MAX_SOLVE_TIME:
            # Too hard → decrease difficulty
            decrease = max(1, int((avg / self.target) ** 0.5))
            self.bits = max(self.min_bits, self.bits - decrease)

        self._last_adjustment = time.time()

    def current_bits(self) -> int:
        """Get current difficulty (bits)."""
        return self.bits

    def estimated_time(self) -> float:
        """Estimate solving time at current difficulty."""
        return estimate_time(self.bits)

    def stats(self) -> Dict[str, Any]:
        return {
            "difficulty_bits": self.bits,
            "target_prefix": "0" * ((self.bits + 3) // 4),
            "expected_attempts": estimate_attempts(self.bits),
            "estimated_time_s": round(self.estimated_time(), 2),
            "last_solve_times": [round(t, 2) for t in self._solve_times],
            "average_solve_time_s": round(
                sum(self._solve_times) / max(1, len(self._solve_times)), 2
            ),
        }

    def to_dict(self) -> dict:
        return {
            "bits": self.bits,
            "solve_times": [round(t, 2) for t in self._solve_times],
        }

    def _save(self):
        path = os.path.join(POW_DIR, "difficulty.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def _load(self):
        path = os.path.join(POW_DIR, "difficulty.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.bits = data.get("bits", self.bits)
                self._solve_times = data.get("solve_times", [])
            except Exception:
                pass
