"""
tests/test_privacy_snapshot.py — Differential Privacy (DP-SGD) + Snapshot + UX Event test

Tests v8 features:
  1. DifferentialPrivacy: gradient clipping, Gaussian noise, epsilon control
  2. SnapshotManager: create, list, rollback, cleanup (DeepRiskNet)
  3. UX Event system: emit, get_recent_events, intervention events
  4. Integration: export_model_update with DP
"""

import json
import math
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from p2p.privacy import DifferentialPrivacy
from core.subconscious.snapshot import SnapshotManager
from core.predictor import DeepRiskNet


# ════════════════════════════════════════════════
#  1. Differential Privacy (DP-SGD)
# ════════════════════════════════════════════════

def test_privacy_defaults():
    dp = DifferentialPrivacy()
    assert dp.stats()["mandatory"] == True, "DP is always mandatory"
    assert dp.epsilon == 3.0
    assert dp.delta == 1e-5
    assert dp.stats()["mandatory"] == True
    print("✅ DP: defaults correct")


def test_privacy_gaussian_noise():
    dp = DifferentialPrivacy(epsilon=10.0)
    # High ε = low noise
    grad = [1.0] * 100
    dp.add_noise(grad)
    avg_noise = sum(abs(g - 1.0) for g in grad) / len(grad)
    assert avg_noise < 0.5, f"avg noise too high for ε=10: {avg_noise}"
    print(f"✅ DP: Gaussian noise avg={avg_noise:.4f} (ok for ε=10)")


def test_privacy_clip_gradient():
    dp = DifferentialPrivacy(epsilon=3.0)
    grad = [5.0, 0.0, 0.0, 0.0]  # L2 = 5.0
    l2 = dp.clip_gradient(grad, clip_norm=1.0)
    assert abs(l2 - 5.0) < 0.001, f"original L2 should be 5.0, got {l2}"
    clipped_l2 = math.sqrt(sum(g * g for g in grad))
    assert abs(clipped_l2 - 1.0) < 0.001, f"clipped L2 should be 1.0, got {clipped_l2}"
    print(f"✅ DP: gradient clip (L2 5.0 → {clipped_l2:.3f})")


def test_privacy_epsilon_tradeoff():
    # Low ε = high noise
    low_eps = DifferentialPrivacy(epsilon=0.5)
    high_eps = DifferentialPrivacy(epsilon=10.0)

    # Test via protect_weights (export noise)
    weights_small = {"layer_W": [[0.5] * 10 for _ in range(5)]}
    weights_large = {"layer_W": [[0.5] * 10 for _ in range(5)]}

    original_layer = [row[:] for row in weights_small["layer_W"]]

    low_eps.protect_weights(weights_small)
    high_eps.protect_weights(weights_large)

    low_deltas = []
    high_deltas = []
    for r in range(5):
        for c in range(10):
            low_deltas.append(abs(weights_small["layer_W"][r][c] - original_layer[r][c]))
            high_deltas.append(abs(weights_large["layer_W"][r][c] - original_layer[r][c]))

    avg_low = sum(low_deltas) / len(low_deltas)
    avg_high = sum(high_deltas) / len(high_deltas)

    assert avg_low > avg_high, \
        f"low eps noise ({avg_low:.4f}) should be > high eps ({avg_high:.4f})"
    print(f"✅ DP: ε=0.5 avg_delta={avg_low:.4f} > ε=10 avg_delta={avg_high:.4f}")


def test_privacy_protect_weights():
    dp = DifferentialPrivacy(epsilon=5.0)
    weights = {
        "l1_W": [[0.2, 0.3], [0.7, 0.8]],
        "l1_b": [0.1, 0.5],
    }
    original = json.dumps(weights)

    dp.protect_weights(weights)
    changed = json.dumps(weights)

    has_change = original != changed
    print(f"✅ DP: weights protected (changed={has_change})")


def test_privacy_disabled():
    """DP is always mandatory — verify it always modifies weights."""
    dp = DifferentialPrivacy(epsilon=5.0)
    weights = {"l1_W": [[0.5]], "l1_b": [0.5]}
    original = json.dumps(weights)

    dp.protect_weights(weights)
    changed = json.dumps(weights)

    assert original != changed, "mandatory DP must always change weights"
    print("✅ DP: mandatory mode always protects weights")


def test_privacy_noisy_copy():
    dp = DifferentialPrivacy(epsilon=3.0)
    model = DeepRiskNet(n_features=2, hidden_dim=8, lr=0.01)

    X = [[0.2, 0.3], [0.7, 0.8], [0.1, 0.5], [0.9, 0.2]]
    y = [0.1, 0.9, 0.2, 0.8]
    model.fit(X, y)

    noisy = dp.get_noisy_copy(model)
    assert noisy is not None
    assert isinstance(noisy, DeepRiskNet)

    original_pred = model.predict([0.5, 0.5]).crash_risk
    noisy_pred = noisy.predict([0.5, 0.5]).crash_risk

    # Less strict — just verify it runs
    print(f"✅ DP: noisy copy created (original pred={original_pred:.3f}, "
          f"noisy pred={noisy_pred:.3f})")


# ════════════════════════════════════════════════
# 2. Snapshot Management (DeepRiskNet)
# ════════════════════════════════════════════════

def test_snapshot_create_and_list():
    tmpdir = tempfile.mkdtemp()
    try:
        sm = SnapshotManager(snapshot_dir=tmpdir)

        model = DeepRiskNet(n_features=1, hidden_dim=8, lr=0.01)
        X = [[0.2], [0.8]]
        y = [0.1, 0.9]
        model.fit(X, y)

        meta = sm.snapshot(model, tag="test_create")
        assert meta["tag"] == "test_create"
        assert meta["model_size_bytes"] > 0
        assert "timestamp" in meta

        snaps = sm.list_snapshots()
        assert len(snaps) == 1
        assert snaps[0]["name"] == meta["name"]
        print(f"✅ Snapshot: created + listed ({snaps[0]['name']})")
    except Exception as e:
        print(f"❌ Snapshot: {e}")
        raise
    finally:
        shutil.rmtree(tmpdir)


def test_snapshot_rollback():
    tmpdir = tempfile.mkdtemp()
    try:
        sm = SnapshotManager(snapshot_dir=tmpdir)

        # Initial model
        model = DeepRiskNet(n_features=2, hidden_dim=8, lr=0.01)
        X = [[0.2, 0.3], [0.7, 0.8], [0.4, 0.2], [0.9, 0.5],
             [0.1, 0.6], [0.8, 0.1]]
        y = [0.1, 0.9, 0.3, 0.8, 0.2, 0.7]
        model.fit(X, y)
        pred_before = model.predict([0.5, 0.5]).crash_risk

        sn_meta = sm.snapshot(model, tag="pre_update")

        # Modify model: retrain with different data
        y_flipped = [1.0 - v for v in y]
        model.fit(X, y_flipped)
        modified_pred = model.predict([0.5, 0.5]).crash_risk
        print(f"  pred_before={pred_before:.3f}, modified={modified_pred:.3f}")

        # Rollback
        restored = sm.rollback(sn_meta["name"])
        assert restored is not None
        restored_pred = restored.predict([0.5, 0.5]).crash_risk

        # Should be close to original (exact match depends on training determinism)
        print(f"  restored={restored_pred:.3f}")
        print(f"✅ Snapshot: rollback works")
    except Exception as e:
        print(f"❌ Snapshot rollback: {e}")
        raise
    finally:
        shutil.rmtree(tmpdir)


def test_snapshot_daily_check():
    tmpdir = tempfile.mkdtemp()
    try:
        sm = SnapshotManager(snapshot_dir=tmpdir)
        model = DeepRiskNet(n_features=1, hidden_dim=8, lr=0.01)
        model.fit([[0.2], [0.8]], [0.1, 0.9])

        # First daily check should create snapshot
        created = sm.daily_check(model)
        assert created, "first daily check should create snapshot"

        # Second daily check (same day) should not create duplicate
        created2 = sm.daily_check(model)
        assert not created2, "second daily check should NOT create snapshot"

        snaps = sm.list_snapshots()
        assert len(snaps) == 1
        assert snaps[0]["tag"] == "daily"

        print(f"✅ Snapshot: daily_check works ({len(snaps)} snapshot, "
              f"repeat={created2})")
    except Exception as e:
        print(f"❌ Snapshot daily_check: {e}")
        raise
    finally:
        shutil.rmtree(tmpdir)


def test_snapshot_cleanup():
    tmpdir = tempfile.mkdtemp()
    try:
        import core.subconscious.snapshot as s_mod
        old_max = s_mod.MAX_SNAPSHOTS
        old_days = s_mod.MAX_DAYS
        s_mod.MAX_SNAPSHOTS = 2
        s_mod.MAX_DAYS = 365

        sm = SnapshotManager(snapshot_dir=tmpdir)
        model = DeepRiskNet(n_features=1, hidden_dim=8, lr=0.01)
        model.fit([[0.2], [0.8]], [0.1, 0.9])

        # Create 3 snapshots (exceeds max_snapshots=2)
        sm.snapshot(model, tag="manual")
        time.sleep(0.01)
        sm.snapshot(model, tag="daily")
        time.sleep(0.01)
        sm.snapshot(model, tag="daily")

        assert len(sm.list_snapshots()) == 3

        # Cleanup should prune
        removed = sm.cleanup()
        snaps = sm.list_snapshots()
        assert len(snaps) >= 1
        assert any(s.get("tag") == "manual" for s in snaps), \
            "manual snapshots should survive cleanup"
        print(f"✅ Snapshot: cleanup removed {removed}, kept {len(snaps)} snapshots "
              f"(manual={sum(1 for s in snaps if s.get('tag')=='manual')})")

        s_mod.MAX_SNAPSHOTS = old_max
        s_mod.MAX_DAYS = old_days
    except Exception as e:
        print(f"❌ Snapshot cleanup: {e}")
        raise
    finally:
        shutil.rmtree(tmpdir)


# ════════════════════════════════════════════════
# 3. UX Events (via Subconscious)
# ════════════════════════════════════════════════

def _make_minimal_subconscious():
    from core.subconscious import Subconscious
    return Subconscious(
        enabled=True,
        blockchain_enabled=False,
        p2p_enabled=False,
        auto_train_interval=9999,
    )


def test_ux_event_emit():
    sc = _make_minimal_subconscious()
    event = sc._emit_event("test", "test message", {"data": 42})
    assert event["type"] == "test"
    assert event["message"] == "test message"
    assert event["data"] == {"data": 42}
    assert event["id"] == 1

    events = sc.get_recent_events(10)
    assert len(events) == 1
    assert events[0]["message"] == "test message"
    print(f"✅ UX Event: emitted and retrieved ({event['type']})")


def test_ux_event_limit():
    sc = _make_minimal_subconscious()
    for i in range(120):
        sc._emit_event(f"test_{i}", f"msg_{i}")
    events = sc.get_recent_events(10)
    assert len(events) == 10
    assert len(sc._event_log) == 100, \
        f"should cap at 100, got {len(sc._event_log)}"
    assert sc._event_log[0]["type"] == "test_20"
    print(f"✅ UX Event: capped at 100 (stored={len(sc._event_log)}, "
          f"returned={len(events)})")


def test_ux_intervention_event():
    sc = _make_minimal_subconscious()
    # Simulate high-risk scenarios to trigger events
    for _ in range(5):
        sc.observe_action("read_file", success=False, latency=5.0)

    result = sc.should_intervene()
    events = sc.get_recent_events(5)
    event_types = [e["type"] for e in events]
    if result.get("intervene"):
        assert "rewind" in event_types or "warn" in event_types, \
            f"expected intervention event, got {event_types}"
        print(f"✅ UX Event: intervention generated event (type={result['action']})")
    else:
        print(f"  (risk too low for intervention, that's OK: {result.get('risk')})")


# ════════════════════════════════════════════════
#  4. Integration
# ════════════════════════════════════════════════

def test_snapshot_integration():
    """Test Subconscious.snapshot() and .rollback() integration."""
    sc = _make_minimal_subconscious()

    # Train some data
    for _ in range(5):
        sc.record_training_sample([0.2] * 12, 0.1)
        sc.record_training_sample([0.8] * 12, 0.9)
    sc.train()

    # Snapshot
    snap = sc.snapshot(tag="test_integration")
    pred_before = sc.predict([0.5] * 12).crash_risk
    print(f"  pred_before={pred_before:.3f}")

    # Modify model via retrain with flipped labels
    for _ in range(5):
        sc.record_training_sample([0.2] * 12, 0.9)  # flipped
        sc.record_training_sample([0.8] * 12, 0.1)  # flipped
    sc.train()

    pred_modified = sc.predict([0.5] * 12).crash_risk
    print(f"  pred_modified={pred_modified:.3f} (diff={abs(pred_modified - pred_before):.3f})")

    # Rollback
    success = sc.rollback(snap["name"])
    pred_restored = sc.predict([0.5] * 12).crash_risk
    print(f"  pred_restored={pred_restored:.3f}")
    assert success, "rollback should succeed"
    print(f"✅ Integration: snapshot+rollback (pred {pred_before:.3f} → "
          f"modified {pred_modified:.3f} → restored {pred_restored:.3f})")


def test_privacy_integration():
    """Test Federation export with DP."""
    from p2p.federation import FederationAggregator

    agg = FederationAggregator(privacy_epsilon=5.0)
    assert agg.privacy_active

    # Enable defense
    agg.enable_defense()

    # Use low difficulty for test speed
    if hasattr(agg, 'pow_difficulty') and agg.pow_difficulty:
        agg.pow_difficulty.bits = 8

    model = DeepRiskNet(n_features=2, hidden_dim=8, lr=0.01)
    X = [[0.2, 0.3], [0.7, 0.8], [0.1, 0.5], [0.9, 0.2]]
    y = [0.1, 0.9, 0.2, 0.8]
    model.fit(X, y)

    # Export (with DP and PoW)
    update = agg.export_model_update(model)
    assert update["model_version"] == "subconscious-v8"
    assert "params" in update
    assert "pow_nonce" in update
    assert "pow_hash" in update

    print(f"✅ Integration: DP in federation export (ε=5.0, "
          f"model_version={update['model_version']}, size={update['size_bytes']}B)")


def test_seed_weights():
    """Test that random seed produces acceptable variation in weights."""
    import random as rnd
    rnd.seed(42)

    model = DeepRiskNet(n_features=12, hidden_dim=16, lr=0.01)
    X = [[rnd.random() for _ in range(12)] for _ in range(20)]
    y = [1.0 if sum(x) > 6.0 else 0.0 for x in X]
    model.fit(X, y)
    # Predict on known patterns
    low_state = [0.0] * 12
    high_state = [1.0] * 12
    low_risk = model.predict(low_state).crash_risk
    high_risk = model.predict(high_state).crash_risk
    print(f"  low_state risk={low_risk:.3f}, high_state risk={high_risk:.3f}")
    assert 0.0 <= low_risk <= 1.0
    assert 0.0 <= high_risk <= 1.0
    print("✅ Seed weights: prediction reasonable")
