"""test_p2p_e2e.py — Multi-node P2P integration test.

Tests two Subconscious instances with p2p_enabled=True and blockchain_enabled=False
coexisting on the same machine without external bootstrap infrastructure.
"""

import pytest


@pytest.fixture(autouse=True)
def _patch_env_and_consent(monkeypatch):
    """Ensure P2P stays enabled and external network calls are no-ops."""
    monkeypatch.setenv("WW_BOOTSTRAP_URLS", "")
    monkeypatch.setattr("core.consent.ConsentManager.check", lambda s, f: True)
    monkeypatch.setattr("p2p.nostr.RelayPool.start", lambda s, sub_id="": None)


def _make_two_nodes():
    """Create two Subconscious instances with distinct node_ids and auto-assigned ports."""
    from core.subconscious import Subconscious

    node1 = Subconscious(
        p2p_enabled=True,
        blockchain_enabled=False,
        miner_id="e2e-node-alpha",
    )
    node2 = Subconscious(
        p2p_enabled=True,
        blockchain_enabled=False,
        miner_id="e2e-node-bravo",
    )
    return node1, node2


def _cleanup(n1, n2):
    """Stop P2P and gossip on both nodes."""
    for node in (n1, n2):
        try:
            if node.p2p is not None:
                node.p2p.stop()
        except Exception:
            pass
        try:
            if node.gossip is not None:
                node.gossip.stop()
        except Exception:
            pass


class TestTwoNodeStartup:
    """Both nodes must start cleanly, expose correct attributes, and shut down."""

    def test_two_nodes_start_cleanly(self):
        node1, node2 = _make_two_nodes()
        try:
            assert node1.p2p is not None, "node1 P2P should be initialized"
            assert node2.p2p is not None, "node2 P2P should be initialized"
            assert node1.p2p.running, "node1 P2P should be running"
            assert node2.p2p.running, "node2 P2P should be running"
        finally:
            _cleanup(node1, node2)

    def test_p2p_attributes_exist(self):
        node1, node2 = _make_two_nodes()
        try:
            for name, node in [("node1", node1), ("node2", node2)]:
                assert node.p2p is not None, f"{name}.p2p is None"
                assert node.dht is not None, f"{name}.dht is None"
                assert node.gossip is not None, f"{name}.gossip is None"
                assert node.federation is not None, f"{name}.federation is None"
                assert node.blockchain is None, f"{name}.blockchain should be None (disabled)"
                count = node.p2p.peer_count()
                assert count == 0, f"{name} peer count={count}, expected 0"
        finally:
            _cleanup(node1, node2)

    def test_shutdown_clean(self):
        node1, node2 = _make_two_nodes()
        _cleanup(node1, node2)

        assert not node1.p2p.running, "node1 P2P should be stopped"
        assert not node2.p2p.running, "node2 P2P should be stopped"
        assert not node1.gossip.running, "node1 gossip should be stopped"
        assert not node2.gossip.running, "node2 gossip should be stopped"

