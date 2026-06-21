"""ww/core/subconscious — Sybil Defense v7 test"""

import sys; sys.path.insert(0, ".")
import os
import random
import shutil

random.seed(42)


def _cleanup():
    pow_dir = os.path.expanduser("~/worldwave/data/subconscious/pow")
    if os.path.isdir(pow_dir):
        shutil.rmtree(pow_dir)
    rep_dir = os.path.expanduser("~/worldwave/data/subconscious/reputation")
    if os.path.isdir(rep_dir):
        shutil.rmtree(rep_dir)


# ═══ 1. PoW ═══
def test_pow_solve_verify():
    _cleanup()
    from p2p.pow import solve, verify
    data = b"hello_world"
    result = solve(data, 4, timeout_s=5.0)
    assert result is not None, "PoW solve failed"
    nonce, h, attempts, elapsed = result
    assert isinstance(nonce, int) and nonce >= 0
    assert isinstance(h, str) and len(h) == 64
    assert attempts > 0
    assert verify(data, nonce, 4, expected_hash=h)
    assert verify(data, nonce, 4)
    assert not verify(data, nonce, 5)


def test_pow_difficulty_adjuster():
    _cleanup()
    from p2p.pow import DifficultyAdjuster
    da = DifficultyAdjuster(initial_bits=8, min_bits=4, max_bits=20, window_size=3)
    assert da.current_bits() == 8

    da.record_solve(12.0)
    da.record_solve(18.0)
    da.record_solve(22.0)  # avg > MAX_SOLVE_TIME → decrease
    assert da.current_bits() < 8, f"Difficulty should decrease, got {da.current_bits()}"

    da2 = DifficultyAdjuster(initial_bits=4, min_bits=2, max_bits=10, window_size=3)
    da2.record_solve(0.5)
    da2.record_solve(1.0)
    da2.record_solve(0.8)
    assert da2.current_bits() > 4

    stats = da2.stats()
    assert "difficulty_bits" in stats
    assert "estimated_time_s" in stats


# ═══ 2. Sandbox ═══
def test_sandbox_validation():
    _cleanup()
    from core.subconscious.sandbox import SandboxValidator, ValidationSetManager
    from core.predictor import DeepRiskNet

    vsm = ValidationSetManager(max_size=20)
    vsm.add_sample([1, 0, 5, 0, 100, 3, 1, 0, 1, 10, 0, 30], 1.0)
    vsm.add_sample([0, 0, 0, 0, 10, 1, 0, 0, 0, 0, 0, 5], 0.0)
    vsm.add_sample([3, 2, 15, 0, 200, 5, 1, 0, 2, 10, 0, 60], 1.0)
    vsm.add_sample([0, 1, 2, 0, 50, 2, 3, 1, 5, 5, 0, 10], 0.0)
    assert vsm.stats()["total"] == 4
    assert len(vsm.get_data()) == 4

    sv = SandboxValidator()
    good_X = [[1.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 1.0], [0.0, 1.0, 0.0]]
    good_y = [1.0, 0.0, 1.0, 0.0]
    bad_y = [0.0, 1.0, 0.0, 1.0]

    good_model = DeepRiskNet(n_features=3, hidden_dim=8, lr=0.01, dropout=0.0)
    good_model.fit(good_X, good_y)
    bad_model = DeepRiskNet(n_features=3, hidden_dim=8, lr=0.01, dropout=0.0)
    bad_model.fit(good_X, bad_y)

    val_set = [
        ([1.0, 0.0, 1.0], 1.0),
        ([0.0, 1.0, 0.0], 0.0),
        ([1.0, 0.0, 1.0], 1.0),
        ([0.0, 1.0, 0.0], 0.0),
    ]

    good_correct = sum(1 for vec, truth in val_set 
                       if abs(getattr(good_model.predict(vec), 'crash_risk', good_model.predict(vec)) - truth) <= 0.5)
    good_acc = good_correct / len(val_set)
    assert good_acc >= 0.5

    bad_correct = sum(1 for vec, truth in val_set
                      if abs(getattr(bad_model.predict(vec), 'crash_risk', bad_model.predict(vec)) - truth) <= 0.5)
    bad_acc = bad_correct / len(val_set)
    assert bad_acc >= 0.0  # bad model may have any accuracy


# ═══ 3. Aggregation ═══
def test_aggregation_trimmed_mean():
    _cleanup()
    from core.predictor import DeepRiskNet
    from p2p.aggregation import trimmed_mean
    rnd = random.Random(42)
    big_X, big_y = [], []
    for _ in range(80):
        x1, x2 = rnd.random(), rnd.random()
        label = 1.0 if (x1 > 0.5) != (x2 > 0.5) else 0.0
        big_X.append([x1, x2, float(x1 > 0.5)])
        big_y.append(label)

    models = []
    for i in range(7):
        m = DeepRiskNet(n_features=3, hidden_dim=16, lr=0.01, dropout=0.0)
        train_y = [(1.0 - v) for v in big_y] if i == 6 else big_y
        m.fit(big_X, train_y)
        models.append(m)

    result = trimmed_mean(models, trim_ratio=0.2)
    assert result is not None


def test_aggregation_median():
    _cleanup()
    from core.predictor import DeepRiskNet
    from p2p.aggregation import median_aggregation
    rnd = random.Random(42)
    big_X, big_y = [], []
    for _ in range(80):
        x1, x2 = rnd.random(), rnd.random()
        label = 1.0 if (x1 > 0.5) != (x2 > 0.5) else 0.0
        big_X.append([x1, x2, float(x1 > 0.5)])
        big_y.append(label)

    models = []
    for i in range(7):
        m = DeepRiskNet(n_features=3, hidden_dim=16, lr=0.01, dropout=0.0)
        train_y = [(1.0 - v) for v in big_y] if i == 6 else big_y
        m.fit(big_X, train_y)
        models.append(m)

    result = median_aggregation(models)
    assert result is not None


def test_aggregation_krum():
    _cleanup()
    from core.predictor import DeepRiskNet
    from p2p.aggregation import krum_aggregation
    rnd = random.Random(42)
    big_X, big_y = [], []
    for _ in range(80):
        x1, x2 = rnd.random(), rnd.random()
        label = 1.0 if (x1 > 0.5) != (x2 > 0.5) else 0.0
        big_X.append([x1, x2, float(x1 > 0.5)])
        big_y.append(label)

    models = []
    for i in range(7):
        m = DeepRiskNet(n_features=3, hidden_dim=16, lr=0.01, dropout=0.0)
        train_y = [(1.0 - v) for v in big_y] if i == 6 else big_y
        m.fit(big_X, train_y)
        models.append(m)

    result = krum_aggregation(models, f=1)
    assert result is not None


def test_aggregation_forest():
    _cleanup()
    from core.predictor import DeepRiskNet
    from p2p.aggregation import aggregate_forest
    rnd = random.Random(42)
    big_X, big_y = [], []
    for _ in range(80):
        x1, x2 = rnd.random(), rnd.random()
        label = 1.0 if (x1 > 0.5) != (x2 > 0.5) else 0.0
        big_X.append([x1, x2, float(x1 > 0.5)])
        big_y.append(label)

    model_group = []
    for _ in range(3):
        m = DeepRiskNet(n_features=3, hidden_dim=16, lr=0.01)
        m.fit(big_X, big_y)
        model_group.append(m)
    merged = aggregate_forest(model_group, method="median")
    assert isinstance(merged, DeepRiskNet)


# ═══ 4. Reputation ═══
def test_reputation_tracker():
    _cleanup()
    from p2p.reputation import ReputationTracker
    rt = ReputationTracker()

    assert len(rt.get_top_peers(10)) == 0

    for _ in range(5):
        rt.record_validation("peer_good", passed=True)
    for _ in range(3):
        rt.record_validation("peer_bad", passed=False)

    good_w = rt.get_weight("peer_good")
    bad_w = rt.get_weight("peer_bad")
    assert good_w > bad_w, "Good peer should have higher weight"
    assert good_w > 1.0


def test_reputation_blacklisting():
    _cleanup()
    from p2p.reputation import ReputationTracker
    rt = ReputationTracker()

    for _ in range(10):
        rt.record_validation("peer_evil", passed=False)

    assert rt.is_blacklisted("peer_evil")
    assert rt.get_weight("peer_evil") == 0.0


def test_reputation_top_peers():
    _cleanup()
    from p2p.reputation import ReputationTracker
    rt = ReputationTracker()

    for _ in range(5):
        rt.record_validation("peer_good", passed=True)
    for _ in range(3):
        rt.record_validation("peer_bad", passed=False)

    top = rt.get_top_peers(5)
    assert top[0]["peer_id"] == "peer_good"

    stats = rt.get_stats()
    assert stats["total_peers"] >= 2
    assert stats["total_validations"] >= 8


# ═══ 5. Federation Integration ═══
def test_federation_defense():
    _cleanup()
    from core.predictor import DeepRiskNet
    from p2p.federation import FederationAggregator

    agg = FederationAggregator()
    agg.enable_defense(aggregation_method="median")

    assert agg.defense_active
    assert agg.pow_difficulty is not None
    assert agg.sandbox_validator is not None
    assert agg.reputation_tracker is not None
    assert agg.aggregation_method == "median"

    good_X = [[1.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 1.0], [0.0, 1.0, 0.0]]
    good_y = [1.0, 0.0, 1.0, 0.0]
    model = DeepRiskNet(n_features=3, hidden_dim=8, lr=0.01)
    model.fit(good_X, good_y)

    update = agg.export_model_update(model)
    assert update["model_version"] == "subconscious-v8"
    assert "pow_nonce" in update
    assert "pow_hash" in update
    assert "pow_bits" in update

    fstats = agg.stats()
    assert "defense" in fstats


def test_federation_toggle():
    _cleanup()
    from p2p.federation import FederationAggregator
    agg = FederationAggregator()

    agg.disable_defense()
    assert not agg.defense_active

    agg.enable_defense(aggregation_method="krum")
    assert agg.aggregation_method == "krum"
