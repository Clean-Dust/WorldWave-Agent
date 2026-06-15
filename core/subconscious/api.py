"""
ww/core/subconscious/api.py — subconscious FastAPI route

Mount to WW server, providing subconscious state query and control.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

from .blockchain import bits_to_target, Transaction

logger = logging.getLogger("ww.subconscious.api")


def register_routes(server, subconscious: "Subconscious"):
    """
    at  WW server  registersubconscious API endpoint。

    Args:
        server: FastAPI instance or APIRouter
        subconscious: Subconscious instance
    """

    @server.get("/ww/subconscious/status")
    def sub_status():
        """subconscioussystemstate"""
        return subconscious.get_status()

    @server.get("/ww/subconscious/stats")
    def sub_stats():
        return subconscious.get_stats()

    @server.get("/ww/subconscious/model")
    def sub_model():
        """when model information"""
        m = subconscious.predictor
        return {
            "trained": m._has_trained,
            "model": "DeepRiskNet",
            "size": m.model_size(),
            "params": m._param_count,
            "feature_importance": m.feature_importance()[:10],
        }

    @server.post("/ww/subconscious/train")
    def sub_train():
        """force trigger training"""
        result = subconscious.train()
        return {"training": result}

    @server.get("/ww/subconscious/features")
    def sub_features():
        """when  featurevector"""
        vec = subconscious.feature_extractor.extract()
        from .features import FEATURE_NAMES
        return {
            "features": {
                name: round(vec[i], 3) if isinstance(vec[i], float) else vec[i]
                for i, name in enumerate(FEATURE_NAMES)
            },
            "raw_vector": [round(v, 3) for v in vec],
        }

    @server.get("/ww/subconscious/predict")
    def sub_predict():
        """predict triage vector (4 signals)"""
        vec = subconscious.feature_extractor.extract()
        triage = subconscious.predict(vec)
        return {
            "failure_risk": round(triage.crash_risk, 4),
            "triage": triage.to_dict(),
            "state_vector": [round(v, 3) for v in vec],
        }

    @server.get("/ww/subconscious/rewind/history")
    def sub_rewind_history(limit: int = 10):
        """rewind historical record"""
        events = subconscious.rewind_engine.history[-limit:]
        return {
            "events": [e.to_dict() for e in events],
            "total": len(subconscious.rewind_engine.history),
        }

    @server.get("/ww/subconscious/federation")
    def sub_federation_stats():
        """federationaggregationstatistics"""
        fa = subconscious.federation
        return {
            "node_id": fa.node_id,
            "local_reports": len(fa.local_reports),
            "peers": len(fa.peer_reports),
            "total_reports": sum(len(r) for r in fa.peer_reports.values())
            + len(fa.local_reports),
        }

    @server.post("/ww/subconscious/federation/report")
    def sub_submit_report(data: dict):
        """commit crash report (write Chain + P2P broadcast)"""
        from .federation import CrashReport
        report = CrashReport.from_dict(data)
        sig = subconscious.federation.submit_local_report(report)
        return {"signature": sig, "status": "submitted"}

    @server.post("/ww/subconscious/federation/import")
    def sub_import_update(data: dict):
        """import remote model update"""
        ok = subconscious.federation.import_model_update(
            data, subconscious.predictor
        )
        return {"imported": ok}

    @server.get("/ww/subconscious/federation/export")
    def sub_export_update():
        """export model update (for blockchain broadcast)"""
        update = subconscious.federation.export_model_update(
            subconscious.predictor
        )
        return update

    # ── Chain (Merkle chain) ──

    @server.get("/ww/subconscious/chain/stats")
    def chain_stats():
        chain = subconscious.chain
        if not chain:
            return {"blocks": 0}
        return chain.stats()

    @server.get("/ww/subconscious/chain/blocks")
    def chain_blocks(limit: int = 5):
        chain = subconscious.chain
        if not chain:
            return {"blocks": []}
        blocks = [b.to_dict() for b in chain.blocks[-limit:]]
        return {"blocks": blocks, "total": len(chain.blocks)}

    @server.post("/ww/subconscious/chain/verify")
    def chain_verify():
        chain = subconscious.chain
        if not chain:
            return {"valid": False, "errors": ["no chain"]}
        errors = chain.validate()
        return {"valid": len(errors) == 0, "errors": errors[:10]}

    @server.post("/ww/subconscious/chain/merge")
    def chain_merge(data: dict):
        chain = subconscious.chain
        if not chain:
            return {"merged": False}
        from .chain import Chain
        other = Chain.from_dict(data, data_dir="/tmp/ww_chain_merge")
        result = chain.merge(other)
        return result

    @server.post("/ww/subconscious/chain/flush")
    def chain_flush():
        """force will pending crashes to produce block"""
        fed = subconscious.federation
        fed._flush_block()
        return {"pending": len(fed._pending_crashes)}

    # ── P2P global network ──

    @server.get("/ww/subconscious/network/status")
    def network_status():
        p2p = subconscious.p2p
        if not p2p:
            return {"running": False}
        return p2p.stats()

    @server.post("/ww/subconscious/network/start")
    def network_start():
        p2p = subconscious.p2p
        if p2p:
            p2p.start()
            return {"running": True}
        return {"running": False}

    @server.post("/ww/subconscious/network/stop")
    def network_stop():
        p2p = subconscious.p2p
        if p2p:
            p2p.stop()
            return {"running": False}
        return {"running": False}

    @server.get("/ww/subconscious/network/peers")
    def network_peers():
        p2p = subconscious.p2p
        if not p2p:
            return {"peers": []}
        return {
            "peers": [p.to_dict() for p in p2p.peers.values()],
            "count": len(p2p.peers),
        }

    @server.get("/ww/subconscious/network/blocks")
    def p2p_blocks(from_height: int = 0, count: int = 10):
        """from  P2P networkgetblock（from  peer sync）。"""
        p2p = subconscious.p2p
        if not p2p:
            return {"blocks": []}
        blocks = p2p.get_blocks(from_height, count)
        return {"blocks": blocks, "from": from_height, "count": len(blocks)}

    # ── PoW blockchain ──

    @server.get("/ww/blockchain/status")
    def blockchain_status():
        bc = subconscious.blockchain
        if not bc:
            return {"enabled": False}
        return {
            "enabled": True,
            "height": bc.height,
            "blocks": len(bc.chain),
            "latest_hash": bc.latest_hash[:16] if bc.latest_hash else "none",
            "mempool": len(bc.mempool),
            "orphans": len(bc.orphans),
            "accounts": len(bc.balances),
            "difficulty": bc.difficulty_description(),
            "block_reward": bc._block_reward,
            "hashrate": f"{bc.hashrate:.0f} H/s" if bc.hashrate > 0 else "idle",
            "mining": bc._mining_stats["running"],
            "blocks_mined": bc._mining_stats["blocks_mined"],
        }

    @server.get("/ww/blockchain/blocks")
    def blockchain_blocks(from_height: int = 0, limit: int = 20):
        bc = subconscious.blockchain
        if not bc:
            return {"blocks": []}
        blocks = [b.to_dict() for b in bc.chain[from_height:from_height + limit]]
        return {
            "blocks": blocks,
            "from": from_height,
            "count": len(blocks),
            "total": len(bc.chain),
        }

    @server.post("/ww/blockchain/mine")
    def blockchain_mine():
        """manual trigger mining."""
        bc = subconscious.blockchain
        if not bc:
            return {"mined": False, "error": "blockchain disabled"}
        block = bc.mine_block(bc._miner_id or "api_miner", max_nonce=500000)
        if block and bc.add_block(block):
            return {"mined": True, "hash": block.hash()[:16], "height": bc.height}
        return {"mined": False, "error": "no block found"}

    @server.post("/ww/blockchain/start-mining")
    def blockchain_start_mining(miner_id: str = "api_miner"):
        bc = subconscious.blockchain
        if not bc:
            return {"started": False}
        bc.start_mining(miner_id)
        return {"started": True, "miner": miner_id}

    @server.post("/ww/blockchain/stop-mining")
    def blockchain_stop_mining():
        bc = subconscious.blockchain
        if not bc:
            return {"stopped": False}
        bc.stop_mining()
        return {"stopped": True}

    @server.get("/ww/blockchain/balance")
    def blockchain_balance(node_id: str = ""):
        bc = subconscious.blockchain
        if not bc:
            return {"balance": 0}
        return {"node_id": node_id, "balance": bc.get_balance(node_id)}

    @server.get("/ww/blockchain/mempool")
    def blockchain_mempool():
        bc = subconscious.blockchain
        if not bc:
            return {"transactions": []}
        return {
            "transactions": [tx.to_dict() for tx in bc.mempool],
            "count": len(bc.mempool),
        }

    @server.post("/ww/blockchain/transaction")
    def blockchain_submit_transaction(data: dict):
        """commit a transaction."""
        bc = subconscious.blockchain
        if not bc:
            return {"accepted": False, "error": "blockchain disabled"}
        tx = Transaction.from_dict(data)
        ok = bc.add_transaction(tx)
        return {"accepted": ok, "hash": tx.hash()[:16] if ok else ""}

    @server.get("/ww/blockchain/experiences")
    def blockchain_experiences(from_height: int = 0, limit: int = 50):
        """from blockchain retrieve subconscious experience record."""
        bc = subconscious.blockchain
        if not bc:
            return {"experiences": []}
        return {
            "experiences": bc.get_experiences(from_height, limit),
            "count": len(bc.get_experiences(limit=9999)),
        }

    @server.get("/ww/blockchain/model-updates")
    def blockchain_model_updates(from_height: int = 0, limit: int = 20):
        """Retrieve model update record from blockchain."""
        bc = subconscious.blockchain
        if not bc:
            return {"updates": []}
        return {
            "updates": bc.get_model_updates(from_height, limit),
            "count": len(bc.get_model_updates(limit=9999)),
        }

    @server.get("/ww/blockchain/verify")
    def blockchain_verify():
        """validate the entire chain completeness."""
        bc = subconscious.blockchain
        if not bc or not bc.chain:
            return {"valid": False, "errors": ["empty chain"]}
        errors = []
        for i, block in enumerate(bc.chain):
            target = bits_to_target(block.header.bits)
            h = int(block.hash(), 16)
            if h >= target:
                errors.append(f"Block {i}: invalid PoW ({block.hash()[:16]} >= target)")
            if not block.verify_merkle():
                errors.append(f"Block {i}: invalid merkle root")
            if i > 0 and block.header.previous_hash != bc.chain[i - 1].hash():
                errors.append(f"Block {i}: previous hash mismatch")
        return {"valid": len(errors) == 0, "errors": errors[:20], "blocks": len(bc.chain)}
