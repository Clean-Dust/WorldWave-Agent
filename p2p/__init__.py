"""ww/p2p — Decentralized P2P networking and blockchain layer.

Separated from core/subconscious/ (2026-06-16) to decouple network
infrastructure from cognitive logic, per Gemini architectural review.

Modules:
    blockchain.py   — Proof-of-Work blockchain (SHA256, compact bits, mempool)
    chain.py        — Merkle Chain ledger for subconscious model updates
    network.py      — Global P2P network (bootstrap tracker + HTTP gossip)
    gossip.py       — Gossip learning protocol
    dht.py          — Distributed Hash Table
    federation.py   — Cross-node federation aggregation
    pow.py          — Lightweight PoW anti-Sybil (adaptive difficulty)
    nostr.py        — Nostr Relay communication (BIP-340 Schnorr, relay pool)
    reputation.py   — Web of Trust (reputation tracking + blacklist)
    privacy.py      — Differential Privacy
    aggregation.py  — Robust aggregation (Trimmed Mean / Median / Krum)
    relay_server.py — P2P relay server
    bootstrap_server.py — Bootstrap server for peer discovery
    bootstrap_tracker.py — Bootstrap tracker
    blockchain_node.py — Full blockchain node
"""

from p2p.blockchain import Blockchain, Block, Transaction, create_genesis_block
from p2p.chain import Chain
from p2p.network import GlobalP2PNetwork
from p2p.gossip import GossipModule
from p2p.federation import FederationAggregator, CrashReport
from p2p.pow import solve as pow_solve, verify as pow_verify, DifficultyAdjuster
from p2p.nostr import (
    NostrEvent, NostrRelayClient, RelayPool,
    pack_model_update, unpack_model_update,
    generate_keypair,
    schnorr_sign, schnorr_verify,
)
from p2p.reputation import ReputationTracker, ReputationEntry
from p2p.aggregation import (
    trimmed_mean, median_aggregation, krum_aggregation, multi_krum_aggregation,
    aggregate_forest,
)

__all__ = [
    "Blockchain", "Block", "Transaction", "create_genesis_block",
    "Chain",
    "GlobalP2PNetwork",
    "GossipModule",
    "FederationAggregator", "CrashReport",
    "pow_solve", "pow_verify", "DifficultyAdjuster",
    "NostrEvent", "NostrRelayClient", "RelayPool",
    "pack_model_update", "unpack_model_update",
    "generate_keypair", "schnorr_sign", "schnorr_verify",
    "ReputationTracker", "ReputationEntry",
    "trimmed_mean", "median_aggregation", "krum_aggregation",
    "multi_krum_aggregation", "aggregate_forest",
]
