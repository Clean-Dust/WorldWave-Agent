"""
ww/core/subconscious/reputation.py — Web of Trust (local trust network)

each Agent maintains its own "friend circle" and "credit score book":

- record historical performance of each P2P node
- sandbox validate passed → add points
- sandbox validate failed → deduct points, accumulate to threshold auto blacklist
- malicious node → disconnect gossip connection, do not return peer list
- high reputation node → higher weight, affects contribution to federation aggregation

this mechanism allows high-quality nodes to form a core trust network, malicious nodes are naturally marginalized.
"""

from __future__ import annotations
import json
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("ww.subconscious.reputation")

REPUTATION_DIR = os.path.expanduser("~/worldwave/data/subconscious/reputation")

# ── reputation parameters ──

# each time sandbox validate passes
SCORE_PASS = 10.0
# each time it fails
SCORE_FAIL = -25.0
# consecutive failure penalty stacking
SCORE_CONSECUTIVE_FAIL_MULTIPLIER = 1.5

# blacklist threshold
BLACKLIST_THRESHOLD = -50.0
# graylist threshold (reduce weight, not blacklist)
GRAYLIST_THRESHOLD = -20.0

# reputation decay (decay every hour)
REPUTATION_DECAY_PER_HOUR = 0.02  # 2% / hour

# trust level
TRUST_LEVELS = {
    "core": 80.0,      # coretrust
    "trusted": 40.0,   # trustworthy
    "neutral": 0.0,    #  neutral
    "suspect": -20.0,  # suspicious
    "hostile": -50.0,  # hostility
}

# initial weight
INITIAL_WEIGHT = 1.0
# maximum weight
MAX_WEIGHT = 5.0
# minimum weight (still maintain connection)
MIN_WEIGHT = 0.01


class ReputationEntry:
    """singlenode reputationrecord。"""

    def __init__(
        self,
        peer_id: str,
        score: float = 0.0,
        first_seen: float = 0,
        last_seen: float = 0,
        pass_count: int = 0,
        fail_count: int = 0,
        consecutive_fails: int = 0,
        blacklisted: bool = False,
        blacklist_reason: str = "",
        # Karma / contribution leveltrace
        broadcasts_made: int = 0,
        payloads_served: int = 0,
        bytes_served: int = 0,
        payloads_received: int = 0,
        bytes_received: int = 0,
    ):
        self.peer_id = peer_id
        self.score = score
        self.first_seen = first_seen or time.time()
        self.last_seen = last_seen or time.time()
        self.pass_count = pass_count
        self.fail_count = fail_count
        self.consecutive_fails = consecutive_fails
        self.blacklisted = blacklisted
        self.blacklist_reason = blacklist_reason

        # historyvalidaterecord（recently 20 records）
        self._recent_verdicts: List[Dict] = []

        # Karma / contribution leveltrace
        self.broadcasts_made = broadcasts_made
        self.payloads_served = payloads_served
        self.bytes_served = bytes_served
        self.payloads_received = payloads_received
        self.bytes_received = bytes_received

    @property
    def trust_level(self) -> str:
        if self.blacklisted:
            return "blacklisted"
        if self.score >= TRUST_LEVELS["core"]:
            return "core"
        if self.score >= TRUST_LEVELS["trusted"]:
            return "trusted"
        if self.score >= TRUST_LEVELS["neutral"]:
            return "neutral"
        if self.score >= TRUST_LEVELS["suspect"]:
            return "suspect"
        return "hostile"

    @property
    def weight(self) -> float:
        """
        federationaggregation  weightcoefficient。

        core/trustworthy → highweight（1.0-5.0）
         neutral → standard（1.0）
        suspicious → reduce authority（0.1-0.5）
        hostility/blacklist → nearly zero
        """
        if self.blacklisted:
            return 0.0

        # basicweight = sigmoid(score / 20)
        base = 1.0 / (1.0 + math.exp(-self.score / 30.0))
        # mappingto  [MIN_WEIGHT, MAX_WEIGHT]
        return MIN_WEIGHT + (MAX_WEIGHT - MIN_WEIGHT) * base

    def record_validation(self, passed: bool, details: Optional[dict] = None):
        """recordoncesandboxvalidateresult。"""
        self.last_seen = time.time()

        if passed:
            self.score += SCORE_PASS
            self.pass_count += 1
            self.consecutive_fails = 0
        else:
            multiplier = SCORE_CONSECUTIVE_FAIL_MULTIPLIER ** min(
                self.consecutive_fails, 5
            )
            self.score += SCORE_FAIL * multiplier
            self.fail_count += 1
            self.consecutive_fails += 1

        # blacklistcheck
        if self.score <= BLACKLIST_THRESHOLD and not self.blacklisted:
            self.blacklisted = True
            self.blacklist_reason = (
                f"Score {self.score:.0f} below threshold {BLACKLIST_THRESHOLD}"
            )
            logger.warning(f"🚫 Peer {self.peer_id[:12]} blacklisted: {self.blacklist_reason}")

        # recordrecently validatedetails
        record = {
            "time": time.time(),
            "passed": passed,
            "score": round(self.score, 1),
        }
        if details:
            record["accuracy"] = details.get("accuracy", 0)
            record["verdict"] = details.get("verdict", "")
        self._recent_verdicts.append(record)
        if len(self._recent_verdicts) > 20:
            self._recent_verdicts.pop(0)

    def decay(self, hours_passed: float):
        """
        reputationdecay（long  no interaction）。

        no longer active node，reputation naturally decay。
        """
        if hours_passed <= 0:
            return
        decay_factor = 1.0 - (REPUTATION_DECAY_PER_HOUR * hours_passed)
        if decay_factor < 0:
            decay_factor = 0
        self.score *= decay_factor

        # if score recoveryto blacklist to ，autoremove
        if self.blacklisted and self.score > BLACKLIST_THRESHOLD * 0.8:
            self.blacklisted = False
            self.blacklist_reason = ""
            logger.info(f"🔓 Peer {self.peer_id[:12]} auto-unblacklisted (decay)")

    # ── Karma / contribution level（Tit-for-Tat） ──

    @property
    def karma(self) -> float:
        """
        Karma score = contribute / (contribute + consume + 1)

        Returns:
            0.0 (pure freeloading) ~ 1.0 (willingat share)
        """
        contributed = self.broadcasts_made + self.payloads_served
        consumed = self.payloads_received
        total = contributed + consumed
        if total == 0:
            return 0.5  # new node default neutral
        ratio = contributed / (total + 1)  # +1 avoid division by zero
        # if  has provide bytes and large amount download，bonus points
        if self.bytes_served > 0 and self.bytes_received > 0:
            ratio += 0.1 * min(1.0, self.bytes_served / (self.bytes_received + 1))
        return min(1.0, ratio)

    @property
    def is_free_rider(self) -> bool:
        """ is whether it is free-riding （karma < 0.2 and payloads_received > 5）"""
        return self.karma < 0.2 and self.payloads_received > 5

    def record_contribution(self, contributed: bool, bytes_count: int = 0):
        """
        recordone contributionlineas。

        Args:
            contributed: True=shared itself model, False=from from others download 
            bytes_count: transmit byte count
        """
        if contributed:
            self.broadcasts_made += 1
            self.bytes_served += bytes_count
        else:
            self.payloads_received += 1
            self.bytes_received += bytes_count

    def should_serve_payload(self, requester: "ReputationEntry") -> bool:
        """
        Tit-for-Tat: decide is whether for this requester provide large payload。

        rule：
        - blacklist → neverservice
        - has this peer contributed to me ≥ I have contributed to the other party → service
        - Other party karma ≥ 0.3 → service (sexual or otherwise)
        - Others → latency or rejection (free-riding)
        """
        if requester.blacklisted:
            return False
        if requester.karma >= 0.3:
            return True
        # if the other party sends more than me, service
        if (requester.bytes_served >= requester.bytes_received
                and requester.bytes_served > 0):
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "score": round(self.score, 1),
            "trust_level": self.trust_level,
            "weight": round(self.weight, 3),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "consecutive_fails": self.consecutive_fails,
            "blacklisted": self.blacklisted,
            "blacklist_reason": self.blacklist_reason,
            "karma": round(self.karma, 3),
            "broadcasts_made": self.broadcasts_made,
            "payloads_served": self.payloads_served,
            "bytes_served": self.bytes_served,
            "payloads_received": self.payloads_received,
            "bytes_received": self.bytes_received,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReputationEntry":
        return cls(
            peer_id=d.get("peer_id", ""),
            score=d.get("score", 0.0),
            first_seen=d.get("first_seen", 0),
            last_seen=d.get("last_seen", 0),
            pass_count=d.get("pass_count", 0),
            fail_count=d.get("fail_count", 0),
            consecutive_fails=d.get("consecutive_fails", 0),
            blacklisted=d.get("blacklisted", False),
            blacklist_reason=d.get("blacklist_reason", ""),
            broadcasts_made=d.get("broadcasts_made", 0),
            payloads_served=d.get("payloads_served", 0),
            bytes_served=d.get("bytes_served", 0),
            payloads_received=d.get("payloads_received", 0),
            bytes_received=d.get("bytes_received", 0),
        )


# ════════════════════════════════════════════════════════════════
#  Reputation Tracker
# ════════════════════════════════════════════════════════════════


class ReputationTracker:
    """
    All node reputation trace.

    usage：
      tracker = ReputationTracker()
      tracker.record_validation(peer_id, passed=True)
      weight = tracker.get_weight(peer_id)
      if tracker.is_blacklisted(peer_id):
          drop_connection(peer_id)
    """

    def __init__(self, data_dir: str = REPUTATION_DIR):
        self.data_dir = data_dir
        self._entries: Dict[str, ReputationEntry] = {}

        os.makedirs(data_dir, exist_ok=True)
        self._load()

    def record_validation(
        self, peer_id: str, passed: bool,
        details: Optional[dict] = None,
    ):
        """Record one validation result."""
        entry = self._get_or_create(peer_id)
        entry.record_validation(passed, details)
        self._save()

    def get_weight(self, peer_id: str) -> float:
        """Get this peer's contribution weight."""
        entry = self._entries.get(peer_id)
        if entry is None:
            return INITIAL_WEIGHT
        if entry.blacklisted:
            return 0.0
        return entry.weight

    def get_weights(self, peer_ids: List[str]) -> List[float]:
        """Batch get weight."""
        return [self.get_weight(pid) for pid in peer_ids]

    def get_trust_level(self, peer_id: str) -> str:
        """Trust level."""
        entry = self._entries.get(peer_id)
        if entry is None:
            return "unknown"
        return entry.trust_level

    def is_blacklisted(self, peer_id: str) -> bool:
        """Is it blacklisted."""
        entry = self._entries.get(peer_id)
        return entry is not None and entry.blacklisted

    def is_graylisted(self, peer_id: str) -> bool:
        """
         Is it graylisted (downgraded but not blocked).
         Suspicious or hostile level.
        """
        entry = self._entries.get(peer_id)
        if entry is None or entry.blacklisted:
            return False
        return entry.score <= GRAYLIST_THRESHOLD

    def blacklist(self, peer_id: str, reason: str = ""):
        """Manually blacklist a node."""
        entry = self._get_or_create(peer_id)
        entry.blacklisted = True
        entry.blacklist_reason = reason or "manual blacklist"
        entry.score = BLACKLIST_THRESHOLD - 10  # Ensure it will not auto-remove
        self._save()

    def unblacklist(self, peer_id: str):
        """Remove blacklist."""
        entry = self._entries.get(peer_id)
        if entry:
            entry.blacklisted = False
            entry.blacklist_reason = ""
            entry.score = max(entry.score, GRAYLIST_THRESHOLD)
            self._save()

    def decay_all(self):
        """Apply reputation decay to all nodes."""
        now = time.time()
        for entry in self._entries.values():
            hours_passed = (now - entry.last_seen) / 3600.0
            if hours_passed > 24:  # Decay only after 24 hours of no interaction
                entry.decay(hours_passed)
        self._save()

    def get_peers_to_drop(self) -> List[str]:
        """Get peer list to disconnect."""
        return [
            pid for pid, entry in self._entries.items()
            if entry.blacklisted
        ]

    def get_top_peers(self, n: int = 10) -> List[Dict]:
        """Get top N peers by reputation."""
        sorted_entries = sorted(
            self._entries.values(),
            key=lambda e: e.score,
            reverse=True,
        )
        return [e.to_dict() for e in sorted_entries[:n]]

    def get_stats(self) -> Dict[str, Any]:
        total = len(self._entries)
        blacklisted = sum(1 for e in self._entries.values() if e.blacklisted)
        core = sum(1 for e in self._entries.values() if e.trust_level == "core")
        hostile = sum(1 for e in self._entries.values() if e.trust_level == "hostile")
        total_validations = sum(
            e.pass_count + e.fail_count for e in self._entries.values()
        )
        return {
            "total_peers": total,
            "blacklisted": blacklisted,
            "core_trust": core,
            "hostile": hostile,
            "total_validations": total_validations,
            "overall_reputation": round(
                sum(e.score for e in self._entries.values()) / max(1, total), 1
            ),
        }

    def to_dict(self) -> dict:
        return {
            "entries": {
                pid: entry.to_dict()
                for pid, entry in self._entries.items()
            },
        }

    def _get_or_create(self, peer_id: str) -> ReputationEntry:
        if peer_id not in self._entries:
            self._entries[peer_id] = ReputationEntry(peer_id=peer_id)
        return self._entries[peer_id]

    def _save(self):
        path = os.path.join(self.data_dir, "reputation.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def _load(self):
        path = os.path.join(self.data_dir, "reputation.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                for pid, entry in data.get("entries", {}).items():
                    self._entries[pid] = ReputationEntry.from_dict(entry)
            except Exception as e:
                logger.warning(f"Reputation load failed: {e}")
