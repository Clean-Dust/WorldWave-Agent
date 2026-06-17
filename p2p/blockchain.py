"""
ww/core/subconscious/blockchain.py — WW Blockchain v0.7

True PoW blockchain + subconscious Payload design.

Bitcoin style:
  - SHA256 double Hash mining (hash < target)
  - dynamic difficulty adjustment (every 2016 blocks)
  - Block reward halving mechanism
  - Mempool + sort by byte fee rate
  - Longest chain rule fork resolution
  - 1MB blocksize limit

 and Bitcoin differences:
  - transactiontype：subconscious_experience / model_update / coinbase
  - Payload carries feature vector, model weights, crash log
  - node ID replaces wallet address
  - WW Credits as internal incentive token
  - Pure Python, no C acceleration
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("ww.subconscious.blockchain")

# ── Constants ──

GENESIS_TIMESTAMP = 1710000000.0
BLOCK_REWARD_START = 50
HALVING_INTERVAL = 210000
TARGET_BLOCK_TIME = 120
DIFFICULTY_ADJUSTMENT_INTERVAL = 2016
COINBASE_MATURITY = 100

# blocksize limit (1MB, same as Bitcoin)
MAX_BLOCK_BYTES = 1_000_000
MAX_TX_BYTES = 500_000           # Single transaction limit
MAX_TRANSACTIONS_PER_BLOCK = 50

# Transaction fee
MIN_FEE = 1
BASE_FEE_PER_BYTE = 0.01          # Charge 0.01 WW Credits per 1 byte
MIN_FEE_PER_TX = 10               # Minimum fee per transaction

# Difficulty encoding
INITIAL_BITS = 0x21000001          # target=2^240 ≈ 65536 hashes average
MAX_NONCE = 0xFFFFFFFF
VERSION = 1

# Mempool management
MEMPOOL_MAX_TXS = 2000
MEMPOOL_TX_EXPIRY_SECONDS = 86400  # 24 hours expiry
MEMPOOL_MAX_BYTES = 50_000_000     # 50MB total limit

# transactiontype
TX_TYPE_COINBASE = "coinbase"
TX_TYPE_SUBCONSCIOUS = "subconscious_experience"
TX_TYPE_MODEL_UPDATE = "model_update"

BLOCKCHAIN_DIR = os.path.expanduser("~/worldwave/data/subconscious/blockchain")


# ════════════════════════════════════════════════════════════════
# subconscious Payload — this is what blockchain truly carries: value
# ════════════════════════════════════════════════════════════════


def make_experience_payload(
    node_id: str,
    feature_vector: List[float],
    failed_tool_sequence: List[str],
    successful_correction: str = "",
    reward: float = 0.0,
    spiral_count: int = 0,
    error_message: str = "",
) -> dict:
    """
    Create subconscious experience Payload.

    This is the WW blockchain core transaction type — carries a node's "learning experience":

    - feature_vector (12 floats): crash 12-dimensional state vector
    - failed_tool_sequence: tool call sequence that caused the failure
    - successful_correction: how to fix
    - reward: repair effectiveness (0.0 ~ 1.0)

    serialize about 200-500 bytes (excluding model weights).
    """
    return {
        "node_id": node_id,
        "feature_vector": [round(v, 6) for v in feature_vector],
        "failed_tool_sequence": failed_tool_sequence[:20],
        "successful_correction": successful_correction,
        "reward": round(reward, 4),
        "spiral_count": spiral_count,
        "error_message": error_message[:1000],
        "version": 1,
    }


def make_model_update_payload(
    node_id: str,
    tree_deltas: List[Dict[str, Any]],
    base_block_hash: str = "",
    model_size: int = 0,
) -> dict:
    """
    Create model update Payload.

    random forest incremental weight update — much smaller than full model (~KB level):

    - tree_deltas: [{tree_index, feature, threshold, left_value, right_value}]
    - base_block_hash: based on which block the model does incremental update
    - model_size: total size of the updated model

    serialize about 1-50 KB (depending on forest size).
    """
    # Keep only necessary delta fields, remove redundancy
    compact_deltas = []
    for d in tree_deltas[:200]:  # Up to 200 tree deltas
        compact_deltas.append({
            "i": d.get("tree_index", 0),
            "f": d.get("feature", 0),
            "t": round(d.get("threshold", 0.0), 6),
            "lv": round(d.get("left_value", 0.0), 6),
            "rv": round(d.get("right_value", 0.0), 6),
        })
    return {
        "node_id": node_id,
        "tree_deltas": compact_deltas,
        "base_block_hash": base_block_hash,
        "model_size": model_size,
        "version": 1,
    }


def payload_byte_size(payload: dict) -> int:
    """Estimate Payload bytes size."""
    return len(json.dumps(payload, separators=(",", ":")))


# ════════════════════════════════════════════════════════════════
# Transaction (redesigned)
# ════════════════════════════════════════════════════════════════


@dataclass
class Transaction:
    """
    WW blockchaintransaction。

    Three types — miners will sort based on byte_size to ensure high value
     Small experiences prioritized for packing.

    transactiontype:
      coinbase (0x00)
        ─ Miner block reward, no sender, no fee
        data: {block_height, reward, miner}

      subconscious_experience (0x01) ← core value
        ─ subconscious experience record: crash report + 12-dimensional feature vector
        data: {node_id, feature_vector, failed_tool_sequence, ...}
        size: ~200-500 bytes (pure experience)/~KB (with complete log)

      model_update (0x02)
        ─ random forest incremental weight update
        data: {node_id, tree_deltas, base_block_hash, model_size}
        size: ~1-50 KB (depending on forest size)
    """
    type: str = TX_TYPE_SUBCONSCIOUS
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    sender: str = ""
    signature: str = ""
    fee: int = MIN_FEE

    def byte_size(self) -> int:
        """Transaction serialized bytes size (for fee rate calculation)."""
        return len(self.to_json())

    def fee_per_byte(self) -> float:
        """Fee rate per byte (for mining sort)."""
        sz = self.byte_size()
        return self.fee / max(1, sz)

    def hash(self) -> str:
        raw = f"{self.type}:{self.sender}:{self.timestamp}:{self.fee}:{self.to_json()}"
        return hashlib.sha256(hashlib.sha256(raw.encode()).digest()).hexdigest()

    def compute_signature(self) -> str:
        raw = f"{self.sender}:{self.timestamp}:{self.to_json()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def verify(self) -> bool:
        if self.type == TX_TYPE_COINBASE:
            return self.sender == "" and self.fee == 0
        return self.signature == self.compute_signature()

    def to_json(self) -> str:
        return json.dumps(self.data, separators=(",", ":"), sort_keys=True)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "data": self.data,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "signature": self.signature,
            "fee": self.fee,
            "byte_size": self.byte_size(),
            "fee_per_byte": round(self.fee_per_byte(), 6),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls(
            type=d.get("type", TX_TYPE_SUBCONSCIOUS),
            data=d.get("data", {}),
            timestamp=d.get("timestamp", time.time()),
            sender=d.get("sender", ""),
            signature=d.get("signature", ""),
            fee=d.get("fee", MIN_FEE),
        )

    # ── Factory Methods ──

    @classmethod
    def coinbase(cls, miner_id: str, reward: int, block_height: int) -> "Transaction":
        return cls(
            type=TX_TYPE_COINBASE,
            data={"block_height": block_height, "reward": reward, "miner": miner_id},
            timestamp=time.time(),
            signature=f"coinbase:{miner_id}:{block_height}",
            fee=0,
        )

    @classmethod
    def experience(
        cls,
        node_id: str,
        feature_vector: List[float],
        failed_tool_sequence: List[str],
        successful_correction: str = "",
        reward: float = 0.0,
        spiral_count: int = 0,
        error_message: str = "",
        fee: Optional[int] = None,
    ) -> "Transaction":
        """Create subconscious experience transaction (this is the WW blockchain core value)."""
        payload = make_experience_payload(
            node_id=node_id,
            feature_vector=feature_vector,
            failed_tool_sequence=failed_tool_sequence,
            successful_correction=successful_correction,
            reward=reward,
            spiral_count=spiral_count,
            error_message=error_message,
        )
        auto_fee = max(MIN_FEE_PER_TX, int(payload_byte_size(payload) * BASE_FEE_PER_BYTE))
        tx = cls(
            type=TX_TYPE_SUBCONSCIOUS,
            data=payload,
            sender=node_id,
            fee=fee or auto_fee,
        )
        tx.signature = tx.compute_signature()
        return tx

    @classmethod
    def model_update(
        cls,
        node_id: str,
        tree_deltas: List[Dict[str, Any]],
        base_block_hash: str = "",
        model_size: int = 0,
        fee: Optional[int] = None,
    ) -> "Transaction":
        """Create model update transaction (on-chain learning)."""
        payload = make_model_update_payload(
            node_id=node_id,
            tree_deltas=tree_deltas,
            base_block_hash=base_block_hash,
            model_size=model_size,
        )
        auto_fee = max(MIN_FEE_PER_TX * 5, int(payload_byte_size(payload) * BASE_FEE_PER_BYTE))
        tx = cls(
            type=TX_TYPE_MODEL_UPDATE,
            data=payload,
            sender=node_id,
            fee=fee or auto_fee,
        )
        tx.signature = tx.compute_signature()
        return tx


# ════════════════════════════════════════════════════════════════
# Block header & block
# ════════════════════════════════════════════════════════════════


def merkle_root_tx(transactions: List[Transaction]) -> str:
    """Calculate Merkle tree root of transaction list."""
    if not transactions:
        return "0" * 64
    hashes = [tx.hash() for tx in transactions]
    while len(hashes) > 1:
        if len(hashes) % 2 == 1:
            hashes.append(hashes[-1])
        new_level = []
        for i in range(0, len(hashes), 2):
            concat = hashes[i] + hashes[i + 1]
            h = hashlib.sha256(hashlib.sha256(concat.encode()).digest()).hexdigest()
            new_level.append(h)
        hashes = new_level
    return hashes[0]


@dataclass
class BlockHeader:
    """Block header (80 bytes serialized)."""
    version: int = VERSION
    previous_hash: str = "0" * 64
    merkle_root: str = "0" * 64
    timestamp: float = 0.0
    bits: int = INITIAL_BITS
    nonce: int = 0

    def serialize(self) -> bytes:
        return (
            struct.pack(">I", self.version)
            + bytes.fromhex(self.previous_hash)
            + bytes.fromhex(self.merkle_root)
            + struct.pack(">Q", int(self.timestamp))
            + struct.pack(">I", self.bits)
            + struct.pack(">I", self.nonce)
        )

    def hash(self) -> str:
        data = self.serialize()
        h = hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()
        return h

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "previous_hash": self.previous_hash,
            "merkle_root": self.merkle_root,
            "timestamp": self.timestamp,
            "bits": self.bits,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BlockHeader":
        return cls(
            version=d.get("version", VERSION),
            previous_hash=d.get("previous_hash", "0" * 64),
            merkle_root=d.get("merkle_root", "0" * 64),
            timestamp=d.get("timestamp", 0.0),
            bits=d.get("bits", INITIAL_BITS),
            nonce=d.get("nonce", 0),
        )


@dataclass
class Block:
    """completeblock。"""
    header: BlockHeader = field(default_factory=BlockHeader)
    transactions: List[Transaction] = field(default_factory=list)

    def hash(self) -> str:
        return self.header.hash()

    def byte_size(self) -> int:
        """Block serialized bytes size (including all transactions)."""
        return 80 + sum(tx.byte_size() for tx in self.transactions)

    def total_fees(self) -> int:
        return sum(tx.fee for tx in self.transactions if tx.type != TX_TYPE_COINBASE)

    def to_dict(self) -> dict:
        return {
            "header": self.header.to_dict(),
            "transactions": [tx.to_dict() for tx in self.transactions],
            "hash": self.hash(),
            "byte_size": self.byte_size(),
            "tx_count": len(self.transactions),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(
            header=BlockHeader.from_dict(d["header"]),
            transactions=[Transaction.from_dict(td) for td in d.get("transactions", [])],
        )

    def verify_merkle(self) -> bool:
        return self.header.merkle_root == merkle_root_tx(self.transactions)


# ════════════════════════════════════════════════════════════════
# Genesis block
# ════════════════════════════════════════════════════════════════


def create_genesis_block(miner_id: str = "genesis") -> Block:
    """Create genesis block (lowest difficulty, can be directly computed)."""
    coinbase = Transaction.coinbase(miner_id, BLOCK_REWARD_START, 0)
    header = BlockHeader(
        version=VERSION,
        previous_hash="0" * 64,
        merkle_root=merkle_root_tx([coinbase]),
        timestamp=GENESIS_TIMESTAMP,
        bits=INITIAL_BITS,
        nonce=0,
    )
    target = bits_to_target(header.bits)
    while True:
        h = int(header.hash(), 16)
        if h < target:
            break
        header.nonce += 1
    return Block(header=header, transactions=[coinbase])


# ════════════════════════════════════════════════════════════════
# Difficulty encoding
# ════════════════════════════════════════════════════════════════


def target_bytes(target: int) -> int:
    if target <= 0:
        return 256
    leading = 0
    while (1 << (255 - leading)) >= target:
        leading += 1
    return leading // 8


def difficulty_desc(target: int) -> str:
    zeros = target_bytes(target)
    return f"{zeros} leading zero bytes (target={target:#x})"


def bits_to_target(bits: int) -> int:
    exponent = (bits >> 24) & 0xff
    mantissa = bits & 0x00ffffff
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def target_to_bits(target: int) -> int:
    target_bytes_256 = target.to_bytes(32, 'big')
    first_nonzero = 0
    for b in target_bytes_256:
        if b != 0:
            break
        first_nonzero += 1
    size = 32 - first_nonzero
    if size < 3:
        mantissa_bytes = target_bytes_256[first_nonzero:first_nonzero + 3]
        while len(mantissa_bytes) < 3:
            mantissa_bytes += b'\x00'
        mantissa = int.from_bytes(mantissa_bytes, 'big')
    else:
        mantissa = int.from_bytes(target_bytes_256[first_nonzero:first_nonzero + 3], 'big')
    return (size << 24) | mantissa


# ════════════════════════════════════════════════════════════════
# Blockchain main body
# ════════════════════════════════════════════════════════════════


class Blockchain:
    """
    WW Blockchain v0.7 — PoW blockchain + subconscious experience Payload.

    Miner packing strategy (different from Bitcoin):
      1. Sort transactions by fee-per-byte
      2. Prioritize high-density subconscious_experience (core value)
      3. Then pack model_update (learning weights)
      4. Until reaching 1MB limit or 50 transactions
    """

    def __init__(self, data_dir: str = BLOCKCHAIN_DIR, mining_enabled: bool = False):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

        # Chain
        self.chain: List[Block] = []
        self.orphans: Dict[str, Block] = {}
        self._height = 0
        self._latest_hash = "0" * 64

        # Mempool (supports large Payload)
        self.mempool: List[Transaction] = []
        self._mempool_hashes: Set[str] = set()
        self._mempool_bytes = 0
        self._confirmed_tx: Set[str] = set()

        # Account
        self.balances: Dict[str, int] = {}
        self._block_reward = BLOCK_REWARD_START

        # mining
        self.mining_enabled = mining_enabled
        self._mining_thread: Optional[threading.Thread] = None
        self._mining_stop = threading.Event()
        self._miner_id = ""
        self._mining_stats = {"total_hashes": 0, "blocks_mined": 0, "running": False}
        self.hashrate = 0.0

        # callback
        self._on_tx_callback: Optional[Callable] = None
        self._on_block_callback: Optional[Callable] = None

        self._load()

    # ── Chain State ──

    @property
    def height(self) -> int:
        return len(self.chain) - 1 if self.chain else -1

    @property
    def latest_block(self) -> Optional[Block]:
        return self.chain[-1] if self.chain else None

    @property
    def latest_hash(self) -> str:
        return self.latest_block.hash() if self.latest_block else "0" * 64

    def difficulty_target(self) -> int:
        if not self.chain:
            return bits_to_target(INITIAL_BITS)
        return bits_to_target(self.chain[-1].header.bits)

    def difficulty_description(self) -> str:
        target = self.difficulty_target()
        prefix = format(target, "0256b")
        leading_zeros = len(prefix) - len(prefix.lstrip("0"))
        return f"{leading_zeros} leading zeros (target: {target:#x})"

    # ── Mempool management ──

    def add_transaction(self, tx: Transaction) -> bool:
        """
        Commit a transaction to mempool.

        Supports large Payload (subconscious_experience can reach hundreds of KB).
        Sort by fee-per-byte to ensure high-density value transactions are prioritized.
        """
        if tx.type == TX_TYPE_COINBASE:
            return False

        if not tx.verify():
            logger.warning(f"TX rejected: invalid signature ({tx.hash()[:16]})")
            return False

        tx_hash = tx.hash()
        if tx_hash in self._mempool_hashes or tx_hash in self._confirmed_tx:
            return False

        tx_bytes = tx.byte_size()
        if tx_bytes > MAX_TX_BYTES:
            logger.warning(f"TX rejected: too large ({tx_bytes} > {MAX_TX_BYTES})")
            return False

        # Check sender balance (calculate fee by byte size)
        min_required = max(MIN_FEE_PER_TX, int(tx_bytes * BASE_FEE_PER_BYTE))
        if tx.fee < min_required:
            logger.warning(f"TX rejected: fee too low ({tx.fee} < {min_required}")
            return False

        sender_bal = self.balances.get(tx.sender, 0)
        if sender_bal < tx.fee:
            logger.warning(f"TX rejected: insufficient balance ({sender_bal} < {tx.fee})")
            return False

        # Mempool capacitymanagement
        if len(self.mempool) >= MEMPOOL_MAX_TXS:
            # Kick out lowest fee-per-byte
            self._evict_lowest_fee()
        if self._mempool_bytes + tx_bytes > MEMPOOL_MAX_BYTES:
            self._evict_lowest_fee()

        self.mempool.append(tx)
        self._mempool_hashes.add(tx_hash)
        self._mempool_bytes += tx_bytes

        if self._on_tx_callback:
            try:
                self._on_tx_callback(tx)
            except Exception:
                pass

        logger.debug(f"📥 TX accepted: {tx.type} {tx.hash()[:16]} ({tx_bytes}B, fee={tx.fee})")
        return True

    def _evict_lowest_fee(self):
        """Kick out lowest fee-per-byte transaction."""
        if not self.mempool:
            return
        sorted_tx = sorted(self.mempool, key=lambda t: t.fee_per_byte())
        victim = sorted_tx[0]
        self._mempool_hashes.discard(victim.hash())
        self._mempool_bytes -= victim.byte_size()
        self.mempool.remove(victim)

    def _clean_expired_txs(self):
        """Clean up expired transactions."""
        now = time.time()
        expired = [tx for tx in self.mempool if now - tx.timestamp > MEMPOOL_TX_EXPIRY_SECONDS]
        for tx in expired:
            self._mempool_hashes.discard(tx.hash())
            self._mempool_bytes -= tx.byte_size()
            self.mempool.remove(tx)

    def mempool_snapshot(self, max_bytes: int = MAX_BLOCK_BYTES) -> List[Transaction]:
        """
        Select transactions from mempool (sorted by fee-per-byte + blocksize limit).

        Args:
            max_bytes: blocksize limit (deducting coinbase ~80 bytes overhead)

        Returns:
             Select transaction list, sorted by fee-per-byte descending, total size <= max_bytes
        """
        self._clean_expired_txs()
        # Fee-per-byte descending (high-density experience prioritized)
        sorted_tx = sorted(self.mempool, key=lambda t: t.fee_per_byte(), reverse=True)

        selected: List[Transaction] = []
        total_bytes = 80  # block header overhead
        for tx in sorted_tx:
            if len(selected) >= MAX_TRANSACTIONS_PER_BLOCK:
                break
            tx_bytes = tx.byte_size()
            if total_bytes + tx_bytes > max_bytes:
                continue
            selected.append(tx)
            total_bytes += tx_bytes
        return selected

    def remove_from_mempool(self, tx_hash: str):
        tx_to_remove = [tx for tx in self.mempool if tx.hash() == tx_hash]
        for tx in tx_to_remove:
            self._mempool_bytes -= tx.byte_size()
            self.mempool.remove(tx)
        self._mempool_hashes.discard(tx_hash)

    def mempool_count(self) -> int:
        return len(self.mempool)

    def mempool_stats(self) -> dict:
        return {
            "count": len(self.mempool),
            "total_bytes": self._mempool_bytes,
            "avg_bytes": int(self._mempool_bytes / max(1, len(self.mempool))),
            "by_type": {
                TX_TYPE_SUBCONSCIOUS: sum(1 for t in self.mempool if t.type == TX_TYPE_SUBCONSCIOUS),
                TX_TYPE_MODEL_UPDATE: sum(1 for t in self.mempool if t.type == TX_TYPE_MODEL_UPDATE),
            },
        }

    # ── mining（PoW） ──

    def mine_block(self, miner_id: str, max_nonce: int = MAX_NONCE,
                   max_block_bytes: int = MAX_BLOCK_BYTES) -> Optional[Block]:
        """
         Mine a block (sync).

         Difference from Bitcoin: blocksize is determined by payload, not transaction count.
        subconscious_experience transactions are prioritized even if small (core value).
        """
        if not self.chain:
            return None

        latest = self.chain[-1]
        target = self.difficulty_target()

        # Select transactions by fee-per-byte sort
        txs = self.mempool_snapshot(max_bytes=max_block_bytes)
        total_fee = sum(tx.fee for tx in txs)

        # Coinbase transaction
        block_height = self.height + 1
        reward = self._block_reward + total_fee
        coinbase = Transaction.coinbase(miner_id, reward, block_height)
        all_txs = [coinbase] + txs

        merkle = merkle_root_tx(all_txs)

        header = BlockHeader(
            previous_hash=latest.hash(),
            merkle_root=merkle,
            timestamp=time.time(),
            bits=latest.header.bits,
            nonce=0,
        )

        # PoW mining
        start = time.time()
        hashes_done = 0

        while header.nonce <= max_nonce:
            h = int(header.hash(), 16)
            hashes_done += 1

            if h < target:
                duration = time.time() - start
                block = Block(header=header, transactions=all_txs)
                tx_types = {}
                for tx in all_txs:
                    tx_types[tx.type] = tx_types.get(tx.type, 0) + 1
                logger.info(
                    f"⛏️ Block #{block_height} mined! "
                    f"hash={header.hash()[:16]}... "
                    f"nonce={header.nonce} "
                    f"txs={len(all_txs)} "
                    f"bytes={block.byte_size()} "
                    f"types={tx_types} "
                    f"reward={reward} "
                    f"{hashes_done} hashes in {duration:.1f}s "
                    f"({hashes_done/duration:.0f} H/s)"
                )
                return block

            header.nonce += 1

            if hashes_done % 100000 == 0 and self._mining_stop.is_set():
                break

        return None

    def start_mining(self, miner_id: str):
        if self._mining_thread and self._mining_thread.is_alive():
            return
        self._miner_id = miner_id
        self._mining_stop.clear()
        self._mining_thread = threading.Thread(
            target=self._mining_loop, daemon=True
        )
        self._mining_thread.start()
        logger.info(f"⛏️ Mining started (miner={miner_id[:12]})")
        self._mining_stats["running"] = True

    def stop_mining(self):
        self._mining_stop.set()
        if self._mining_thread:
            self._mining_thread.join(timeout=5)
        self._mining_stats["running"] = False
        logger.info("⛏️ Mining stopped")

    def _mining_loop(self):
        hashes_this_second = 0
        last_second = time.time()

        while not self._mining_stop.is_set():
            try:
                block = self.mine_block(self._miner_id, max_nonce=500000)
                if block:
                    self.add_block(block)
                    self._mining_stats["blocks_mined"] += 1
                else:
                    time.sleep(0.1)
            except Exception as e:
                logger.error(f"Mining error: {e}")
                time.sleep(1)

            hashes_this_second += 500000
            now = time.time()
            if now - last_second >= 1.0:
                self.hashrate = hashes_this_second / (now - last_second)
                self._mining_stats["total_hashes"] += int(hashes_this_second)
                hashes_this_second = 0
                last_second = now

        self._mining_stats["running"] = False

    # ── blockvalidate ──

    def add_block(self, block: Block, broadcast: bool = True) -> bool:
        if not self._validate_block(block):
            return False

        # checkblocksize
        block_sz = block.byte_size()
        if block_sz > MAX_BLOCK_BYTES:
            logger.warning(f"Block rejected: too large ({block_sz} > {MAX_BLOCK_BYTES})")
            return False

        if block.header.previous_hash == self.latest_hash:
            self._append_block(block)
            self._process_block_transactions(block)

            if broadcast and self._on_block_callback:
                try:
                    self._on_block_callback(block)
                except Exception:
                    pass

            self._resolve_orphans()

            if self.height % DIFFICULTY_ADJUSTMENT_INTERVAL == 0 and self.height > 0:
                self._adjust_difficulty()

            self._save()
            return True

        # Fork
        for i, existing in enumerate(self.chain):
            if existing.hash() == block.header.previous_hash:
                new_chain = self.chain[:i + 1] + [block]
                if len(new_chain) > len(self.chain):
                    self._reorg_to(new_chain)
                    self._save()
                    return True
                self.orphans[block.hash()] = block
                return True

        self.orphans[block.hash()] = block
        return False

    def _validate_block(self, block: Block) -> bool:
        if not block.transactions:
            logger.warning("Block rejected: no transactions")
            return False

        if block.transactions[0].type != TX_TYPE_COINBASE:
            logger.warning("Block rejected: first tx not coinbase")
            return False

        if not block.verify_merkle():
            logger.warning("Block rejected: merkle root mismatch")
            return False

        # PoW validate
        target = bits_to_target(block.header.bits)
        h = int(block.hash(), 16)
        if h >= target:
            logger.warning("Block rejected: PoW invalid")
            return False

        # Timestamp
        if self.chain:
            latest = self.chain[-1]
            if block.header.timestamp < latest.header.timestamp - 7200:
                logger.warning("Block rejected: timestamp too old")
                return False
        if block.header.timestamp > time.time() + 7200:
            logger.warning("Block rejected: timestamp in future")
            return False

        # Coinbase reward
        coinbase = block.transactions[0]
        actual_reward = coinbase.data.get("reward", 0)
        # Calculate all non-coinbase fees
        total_other_fees = sum(tx.fee for tx in block.transactions[1:]
                              if tx.type != TX_TYPE_COINBASE)
        # Find corresponding height reward
        block_height = self.height + 1
        halvings = block_height // HALVING_INTERVAL
        block_reward = BLOCK_REWARD_START >> halvings
        expected_reward = block_reward + total_other_fees
        if actual_reward != expected_reward:
            logger.warning(f"Block rejected: reward mismatch ({actual_reward} != {expected_reward})")
            return False

        # blocksize
        if block.byte_size() > MAX_BLOCK_BYTES:
            logger.warning("Block rejected: too large")
            return False

        return True

    def _append_block(self, block: Block):
        self.chain.append(block)
        self._height = self.height

    def _process_block_transactions(self, block: Block):
        for i, tx in enumerate(block.transactions):
            tx_hash = tx.hash()
            self._confirmed_tx.add(tx_hash)

            if tx.type == TX_TYPE_COINBASE:
                miner = tx.data.get("miner", tx.sender)
                reward = tx.data.get("reward", 0)
                self.balances[miner] = self.balances.get(miner, 0) + reward
            elif tx.type in (TX_TYPE_SUBCONSCIOUS, TX_TYPE_MODEL_UPDATE):
                sender_bal = self.balances.get(tx.sender, 0)
                self.balances[tx.sender] = sender_bal - tx.fee
                self.remove_from_mempool(tx_hash)
                # Record experience or model update statistics
                if tx.type == TX_TYPE_SUBCONSCIOUS:
                    pass  # subconscious experience writes to chain Data section

    def _resolve_orphans(self):
        resolved = True
        while resolved:
            resolved = False
            for h, block in list(self.orphans.items()):
                if block.header.previous_hash == self.latest_hash:
                    if self._validate_block(block):
                        self._append_block(block)
                        self._process_block_transactions(block)
                        self.orphans.pop(h, None)
                        resolved = True

    def _reorg_to(self, new_chain: List[Block]):
        old_height = self.height
        for block in reversed(self.chain[len(new_chain) - 1:]):
            for tx in block.transactions:
                if tx.type == TX_TYPE_COINBASE:
                    miner = tx.data.get("miner", tx.sender)
                    reward = tx.data.get("reward", 0)
                    self.balances[miner] = self.balances.get(miner, 0) - reward
        self.chain = new_chain
        for block in new_chain[old_height:]:
            self._process_block_transactions(block)
        logger.info(f"🔄 Chain reorg: {old_height} → {self.height}")

    def _adjust_difficulty(self):
        if len(self.chain) < DIFFICULTY_ADJUSTMENT_INTERVAL:
            return
        first = self.chain[-DIFFICULTY_ADJUSTMENT_INTERVAL]
        last = self.chain[-1]
        actual_time = last.header.timestamp - first.header.timestamp
        expected_time = TARGET_BLOCK_TIME * DIFFICULTY_ADJUSTMENT_INTERVAL
        ratio = actual_time / expected_time
        ratio = max(0.25, min(4.0, ratio))
        old_target = bits_to_target(last.header.bits)
        new_target = int(old_target / ratio)
        new_bits = target_to_bits(new_target)
        self.chain[-1].header.bits = new_bits
        logger.info(f"⚙️ Difficulty adjusted: {last.header.bits:#x} → {new_bits:#x} "
                    f"(ratio={ratio:.2f}, actual={actual_time:.0f}s, expected={expected_time}s)")
        halvings = self.height // HALVING_INTERVAL
        self._block_reward = BLOCK_REWARD_START >> halvings

    # ── Account ──

    def get_balance(self, node_id: str) -> int:
        return self.balances.get(node_id, 0)

    def reward_miner(self, node_id: str, amount: int):
        self.balances[node_id] = self.balances.get(node_id, 0) + amount

    # ── query ──

    def get_experiences(self, from_height: int = 0, limit: int = 50) -> List[dict]:
        """Retrieve subconscious experience record from blockchain (does not contain model_update)."""
        experiences = []
        for block in self.chain[from_height:from_height + limit]:
            for tx in block.transactions:
                if tx.type == TX_TYPE_SUBCONSCIOUS:
                    exp = tx.data.copy()
                    exp["block_height"] = self.chain.index(block)
                    exp["block_hash"] = block.hash()[:16]
                    exp["tx_hash"] = tx.hash()[:16]
                    experiences.append(exp)
                    if len(experiences) >= limit:
                        return experiences
        return experiences

    def get_model_updates(self, from_height: int = 0, limit: int = 20) -> List[dict]:
        """Retrieve model update record from blockchain."""
        updates = []
        for block in self.chain[from_height:]:
            for tx in block.transactions:
                if tx.type == TX_TYPE_MODEL_UPDATE:
                    upd = tx.data.copy()
                    upd["block_height"] = self.chain.index(block)
                    upd["block_hash"] = block.hash()[:16]
                    updates.append(upd)
                    if len(updates) >= limit:
                        return updates
        return updates

    # ── statistics ──

    def stats(self) -> Dict[str, Any]:
        experience_count = sum(
            1 for b in self.chain
            for tx in b.transactions
            if tx.type == TX_TYPE_SUBCONSCIOUS
        )
        model_update_count = sum(
            1 for b in self.chain
            for tx in b.transactions
            if tx.type == TX_TYPE_MODEL_UPDATE
        )
        return {
            "height": self.height,
            "blocks": len(self.chain),
            "mempool": self.mempool_stats(),
            "orphans": len(self.orphans),
            "accounts": len(self.balances),
            "latest_hash": self.latest_hash[:16] if self.latest_hash else "none",
            "difficulty": self.difficulty_description(),
            "block_reward": self._block_reward,
            "total_experiences": experience_count,
            "total_model_updates": model_update_count,
            "hashrate": f"{self.hashrate:.0f} H/s" if self.hashrate > 0 else "idle",
            "mining": self._mining_stats["running"],
            "blocks_mined": self._mining_stats["blocks_mined"],
        }

    # ── serialize ──

    def to_dict(self) -> dict:
        return {
            "blocks": [b.to_dict() for b in self.chain],
            "balances": self.balances,
        }

    @classmethod
    def from_dict(cls, d: dict, data_dir: str = BLOCKCHAIN_DIR) -> "Blockchain":
        bc = cls(data_dir=data_dir)
        bc.chain = [Block.from_dict(bd) for bd in d.get("blocks", [])]
        bc.balances = d.get("balances", {})
        bc._block_reward = BLOCK_REWARD_START >> (bc.height // HALVING_INTERVAL)
        bc._height = bc.height
        return bc

    # ── Persistence ──

    def _save(self):
        path = os.path.join(self.data_dir, "blockchain.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        mempool_path = os.path.join(self.data_dir, "mempool.json")
        with open(mempool_path, "w") as f:
            json.dump([tx.to_dict() for tx in self.mempool], f, indent=2)

    def _load(self):
        path = os.path.join(self.data_dir, "blockchain.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.chain = [Block.from_dict(bd) for bd in data.get("blocks", [])]
                self.balances = data.get("balances", {})
                self._block_reward = BLOCK_REWARD_START >> (self.height // HALVING_INTERVAL)
                logger.info(f"📚 Chain loaded: {len(self.chain)} blocks, {len(self.balances)} accounts")
            except Exception as e:
                logger.warning(f"Chain load failed: {e}")

        mempool_path = os.path.join(self.data_dir, "mempool.json")
        if os.path.isfile(mempool_path):
            try:
                with open(mempool_path) as f:
                    mp_data = json.load(f)
                self.mempool = [Transaction.from_dict(td) for td in mp_data]
                self._mempool_hashes = {tx.hash() for tx in self.mempool}
                self._mempool_bytes = sum(tx.byte_size() for tx in self.mempool)
            except Exception:
                pass

        if not self.chain:
            genesis = create_genesis_block()
            self.chain = [genesis]
            self._save()
            logger.info(f"🌱 Genesis block created: {genesis.hash()[:16]}")
