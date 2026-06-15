"""
ww/core/subconscious/chain.py — Merkle Chain ledger

Lightweight blockchain, pure Python, designed specifically for WW subconscious.

each Block contains ：
  - index, timestamp, node_id (who built it)
  - merkle_root (Merkle tree root of crash reports in this block)
  - previous_hash (link to previous block)
  - crash_hashes (this block contains crash report hash list, as transaction ID)
  - hash (own hash = SHA256(index + prev_hash + merkle_root + node_id))

No PoW mining. Proof of Contribution: nodes contributing crash reports naturally keep producing blocks.
"""

from __future__ import annotations
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

from p2p.federation import CrashReport

CHAIN_DIR = os.path.expanduser("~/worldwave/data/subconscious/chain")


# ════════════════════════════════════════════════════════════════
#  Merkle Tree (Pure Python)
# ════════════════════════════════════════════════════════════════


def merkle_root(hashes: List[str]) -> str:
    """
    Given a list of hashes, compute the Merkle tree root.

    Double SHA256 (Bitcoin style).
    Empty list → 64 zeros.
    """
    if not hashes:
        return "0" * 64
    if len(hashes) == 1:
        return hashlib.sha256(hashlib.sha256(hashes[0].encode()).digest()).hexdigest()

    nodes = hashes[:]
    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes.append(nodes[-1])  # Odd number, copy the last one
        new_level = []
        for i in range(0, len(nodes), 2):
            concat = nodes[i] + nodes[i + 1]
            h = hashlib.sha256(hashlib.sha256(concat.encode()).digest()).hexdigest()
            new_level.append(h)
        nodes = new_level
    return nodes[0]


# ════════════════════════════════════════════════════════════════
#  Block
# ════════════════════════════════════════════════════════════════


class Block:
    """Single block."""

    def __init__(
        self,
        index: int,
        previous_hash: str,
        crash_hashes: List[str],
        node_id: str = "",
        timestamp: Optional[float] = None,
    ):
        self.index = index
        self.previous_hash = previous_hash
        self.crash_hashes = crash_hashes
        self.merkle_root = merkle_root(crash_hashes)
        self.node_id = node_id
        self.timestamp = timestamp or time.time()
        self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        raw = (
            str(self.index)
            + self.previous_hash
            + self.merkle_root
            + self.node_id
            + str(self.timestamp)
        )
        return hashlib.sha256(hashlib.sha256(raw.encode()).digest()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "previous_hash": self.previous_hash,
            "crash_hashes": self.crash_hashes,
            "merkle_root": self.merkle_root,
            "node_id": self.node_id,
            "timestamp": self.timestamp,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        b = cls(
            index=d["index"],
            previous_hash=d["previous_hash"],
            crash_hashes=d.get("crash_hashes", []),
            node_id=d.get("node_id", ""),
            timestamp=d.get("timestamp"),
        )
        b.merkle_root = d.get("merkle_root", b.merkle_root)
        b.hash = d.get("hash", b.hash)
        return b

    def verify(self) -> bool:
        """Validate whether Merkle root and hash are consistent."""
        if self.merkle_root != merkle_root(self.crash_hashes):
            return False
        if self.hash != self._compute_hash():
            return False
        return True


# ════════════════════════════════════════════════════════════════
#  Genesis Block
# ════════════════════════════════════════════════════════════════


def genesis_block(node_id: str = "") -> Block:
    """Genesis block."""
    return Block(
        index=0,
        previous_hash="0" * 64,
        crash_hashes=[],
        node_id=node_id,
        timestamp=1710000000.0,  # Fixed, same for all nodes
    )


# ════════════════════════════════════════════════════════════════
#  Chain
# ════════════════════════════════════════════════════════════════


class Chain:
    """
    Merkle Chain ledger.

    All operations are validated by hash. Tampering with historical data will cause hash mismatch.
    """

    def __init__(self, data_dir: str = CHAIN_DIR):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.blocks: List[Block] = []
        self._crash_index: Dict[str, int] = {}  # crash_hash → block_index
        self._load()

    # ── Core operations ──

    def append_block(self, crash_hashes: List[str], node_id: str = "") -> Block:
        """
        Append a new block.

        Args:
            crash_hashes: The list of crash report hashes to be included in this block
            node_id: The node that produced the block

        Returns:
             New Block
        """
        if not crash_hashes:
            # Empty block not allowed (except genesis)
            raise ValueError("Block must contain at least one crash report")

        prev_hash = self.blocks[-1].hash if self.blocks else "0" * 64
        index = len(self.blocks)

        block = Block(
            index=index,
            previous_hash=prev_hash,
            crash_hashes=crash_hashes,
            node_id=node_id,
        )

        self.blocks.append(block)
        for ch in crash_hashes:
            self._crash_index[ch] = index

        self._save()
        return block

    def validate(self) -> List[str]:
        """
        Validate the integrity of the entire chain.

        Returns:
            error list (empty = chain intact)
        """
        errors = []
        for i, block in enumerate(self.blocks):
            if not block.verify():
                errors.append(f"Block {i}: hash/merkle mismatch")
            if i > 0 and block.previous_hash != self.blocks[i - 1].hash:
                errors.append(f"Block {i}: previous_hash mismatch (got {block.previous_hash[:16]}, expected {self.blocks[i-1].hash[:16]})")
        return errors

    def find_block_by_crash(self, crash_hash: str) -> Optional[Block]:
        """Find the block containing a crash report via its hash."""
        block_idx = self._crash_index.get(crash_hash)
        if block_idx is not None and block_idx < len(self.blocks):
            return self.blocks[block_idx]
        return None

    def merge(self, other: "Chain") -> Dict[str, Any]:
        """
        Merge another chain (for P2P sync).

        Strategy: keep the longer valid chain (longest chain rule).
        """
        if len(other.blocks) <= len(self.blocks):
            return {"merged": False, "reason": "shorter"}

        # Find the fork point
        fork_idx = 0
        for i in range(min(len(self.blocks), len(other.blocks))):
            if self.blocks[i].hash != other.blocks[i].hash:
                fork_idx = i
                break
        else:
            fork_idx = min(len(self.blocks), len(other.blocks))

        # from fork point take other chain
        new_blocks = self.blocks[:fork_idx] + other.blocks[fork_idx:]
        errors = self._validate_blocks(new_blocks)
        if errors:
            return {"merged": False, "reason": f"validation: {'; '.join(errors[:3])}"}

        old_len = len(self.blocks)
        self.blocks = new_blocks
        self._rebuild_index()
        self._save()

        return {
            "merged": True,
            "old_blocks": old_len,
            "new_blocks": len(self.blocks),
            "added": len(self.blocks) - old_len,
        }

    # ── statistics ──

    def stats(self) -> Dict[str, Any]:
        total_crashes = sum(len(b.crash_hashes) for b in self.blocks)
        return {
            "blocks": len(self.blocks),
            "total_crashes": total_crashes,
            "latest_block": self.blocks[-1].hash[:16] if self.blocks else "none",
            "latest_index": self.blocks[-1].index if self.blocks else -1,
            "age_s": round(time.time() - (self.blocks[-1].timestamp if self.blocks else time.time()), 0),
            "valid": len(self.validate()) == 0,
        }

    def to_dict(self) -> dict:
        return {
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, d: dict, data_dir: str = CHAIN_DIR) -> "Chain":
        chain = cls(data_dir=data_dir)
        chain.blocks = [Block.from_dict(bd) for bd in d.get("blocks", [])]
        chain._rebuild_index()
        return chain

    # ── internal ──

    def _validate_blocks(self, blocks: List[Block]) -> List[str]:
        errors = []
        for i, block in enumerate(blocks):
            if not block.verify():
                errors.append(f"Block {i}: hash mismatch")
            if i > 0 and block.previous_hash != blocks[i - 1].hash:
                errors.append(f"Block {i}: chain broken")
        return errors

    def _rebuild_index(self):
        self._crash_index = {}
        for block in self.blocks:
            for ch in block.crash_hashes:
                self._crash_index[ch] = block.index

    def _save(self):
        path = os.path.join(self.data_dir, "chain.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def _load(self):
        path = os.path.join(self.data_dir, "chain.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.blocks = [Block.from_dict(bd) for bd in data.get("blocks", [])]
                self._rebuild_index()
            except Exception:
                pass
        if not self.blocks:
            self.blocks = [genesis_block()]
            self._save()
