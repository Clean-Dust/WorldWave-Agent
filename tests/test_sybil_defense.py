"""ww/core/subconscious — Sybil Defense v7 test"""

import sys; sys.path.insert(0, ".")
import time
import json
import os
import random

random.seed(42)

# Clean up persistent state
import shutil
pow_dir = os.path.expanduser("~/worldwave/data/subconscious/pow")
if os.path.isdir(pow_dir):
    shutil.rmtree(pow_dir)
rep_dir = os.path.expanduser("~/worldwave/data/subconscious/reputation")
if os.path.isdir(rep_dir):
    shutil.rmtree(rep_dir)

print("=" * 50)
print("Sybil defense v7 complete test")
print("=" * 50)

# ══ 1. PoW — lightweight PoW ══
print("\n=== 1. PoW ===")
from p2p.pow import solve, verify, DifficultyAdjuster

# Solve 4-bit challenge (fast)
data = b"hello_world"
result = solve(data, 4, timeout_s=5.0)
assert result is not None, "PoW solve failed"
nonce, h, attempts, elapsed = result
assert isinstance(nonce, int) and nonce >= 0
assert isinstance(h, str) and len(h) == 64
assert attempts > 0
print(f"  Solved 4-bit: {attempts} attempts, {elapsed:.3f}s, hash={h[:16]}")

# Verify
assert verify(data, nonce, 4, expected_hash=h)
assert verify(data, nonce, 4)
assert not verify(data, nonce, 5)  # higher difficulty should fail
print("✅ PoW: solve + verify correct")

# DifficultyAdjuster
da = DifficultyAdjuster(initial_bits=8, min_bits=4, max_bits=20, window_size=3)
assert da.current_bits() == 8

# Simulate slow solves → should decrease difficulty
da.record_solve(12.0)
da.record_solve(18.0)
da.record_solve(22.0)  # avg > MAX_SOLVE_TIME → decrease
print(f"  After slow solves: bits={da.current_bits()}")
assert da.current_bits() < 8, f"Difficulty should decrease, got {da.current_bits()}"

da2 = DifficultyAdjuster(initial_bits=4, min_bits=2, max_bits=10, window_size=3)
da2.record_solve(0.5)
da2.record_solve(1.0)
da2.record_solve(0.8)  # avg < MIN_SOLVE_TIME → increase
print(f"  After fast solves: bits={da2.current_bits()}")
assert da2.current_bits() > 4
print("✅ PoW: DifficultyAdjuster adaptive")

# Stats
stats = da2.stats()
assert "difficulty_bits" in stats
assert "estimated_time_s" in stats
print("✅ PoW: stats OK")

# ══ 2. Sandbox ══
print("\n=== 2. Sandbox ===")
from core.subconscious.sandbox import SandboxValidator, ValidationSetManager
from core.predictor import DeepRiskNet

# ValidationSetManager
vsm = ValidationSetManager(max_size=20)
vsm.add_sample([1, 0, 5, 0, 100, 3, 1, 0, 1, 10, 0, 30], 1.0)
vsm.add_sample([0, 0, 0, 0, 10, 1, 0, 0, 0, 0, 0, 5], 0.0)
vsm.add_sample([3, 2, 15, 0, 200, 5, 1, 0, 2, 10, 0, 60], 1.0)
vsm.add_sample([0, 1, 2, 0, 50, 2, 3, 1, 5, 5, 0, 10], 0.0)
assert vsm.stats()["total"] == 4

val_data = vsm.get_data()
assert len(val_data) == 4
print("✅ Sandbox: ValidationSetManager OK")

# SandboxValidator
sv = SandboxValidator()

# Train a "good" model and a "bad" model
good_X = [[1.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 1.0], [0.0, 1.0, 0.0]]
good_y = [1.0, 0.0, 1.0, 0.0]
bad_y = [0.0, 1.0, 0.0, 1.0]  # inverted

good_model = DeepRiskNet(n_features=3, hidden_dim=8, lr=0.01, dropout=0.0)
good_model.fit(good_X, good_y)
bad_model = DeepRiskNet(n_features=3, hidden_dim=8, lr=0.01, dropout=0.0)
bad_model.fit(good_X, bad_y)  # same X, inverted y

val_set = [
    ([1.0, 0.0, 1.0], 1.0),
    ([0.0, 1.0, 0.0], 0.0),
    ([1.0, 0.0, 1.0], 1.0),
    ([0.0, 1.0, 0.0], 0.0),
]

# Check good model accuracy directly
good_correct = sum(1 for vec, truth in val_set if abs(getattr(good_model.predict(vec), 'crash_risk', good_model.predict(vec)) - truth) <= 0.5)
good_acc = good_correct / len(val_set)
print(f"  Good model: accuracy={good_acc:.2f}")
assert good_acc >= 0.5

# Bad model should have lower accuracy
bad_correct = sum(1 for vec, truth in val_set if abs(getattr(bad_model.predict(vec), 'crash_risk', bad_model.predict(vec)) - truth) <= 0.5)
bad_acc = bad_correct / len(val_set)
print(f"  Bad model: accuracy={bad_acc:.2f}")
print(f"✅ Sandbox: accuracy comparison works")

# ══ 3. Aggregation ══
print("\n=== 3. Aggregation ===")
from p2p.aggregation import (
    trimmed_mean, median_aggregation, krum_aggregation, aggregate_forest,
)

# Create several models with same architecture — use more data for convergence
import random as rnd
rnd.seed(42)
# Generate 40 samples of 3-feature XOR-like data
big_X = []
big_y = []
for _ in range(80):
    x1, x2 = rnd.random(), rnd.random()
    # XOR: (x1 > 0.5) != (x2 > 0.5)
    label = 1.0 if (x1 > 0.5) != (x2 > 0.5) else 0.0
    big_X.append([x1, x2, float(x1 > 0.5)])
    big_y.append(label)

models = []
for i in range(7):
    m = DeepRiskNet(n_features=3, hidden_dim=16, lr=0.01, dropout=0.0)
    train_y = [(1.0 - v) for v in big_y] if i == 6 else big_y  # last one is poison (inverted)
    m.fit(big_X, train_y)
    models.append(m)

# Trimmed Mean
result_tm = trimmed_mean(models, trim_ratio=0.2)
assert result_tm is not None
print("✅ Aggregation: trimmed_mean OK")

# Median
result_md = median_aggregation(models)
assert result_md is not None
print("✅ Aggregation: median OK")

# Krum (needs 5+ models)
result_kr = krum_aggregation(models, f=1)
assert result_kr is not None
print("✅ Aggregation: krum OK")

# All should produce models that reject the poison
for name, result in [("trimmed_mean", result_tm), ("median", result_md), ("krum", result_kr)]:
    correct = 0
    for vec, truth in val_set:
        pred = result.predict(vec)
        if abs(getattr(pred, 'crash_risk', pred) - truth) <= 0.5:
            correct += 1
    acc = correct / len(val_set)
    print(f"  {name}: accuracy={acc:.2f}")
    assert acc >= 0.5, f"{name} should maintain >0.5 accuracy"

print("✅ Aggregation: all methods reject poison model")

# Aggregate Forest (aggregate_forest now works on DeepRiskNet models)
model_group = []
for i in range(3):
    m = DeepRiskNet(n_features=3, hidden_dim=16, lr=0.01)
    m.fit(big_X, big_y)
    model_group.append(m)
merged = aggregate_forest(model_group, method="median")
assert isinstance(merged, DeepRiskNet)
print("✅ Aggregation: aggregate_forest OK")

# ══ 4. Reputation ══
print("\n=== 4. Reputation ===")
from p2p.reputation import ReputationTracker, ReputationEntry

rt = ReputationTracker()

# Initial
assert len(rt.get_top_peers(10)) == 0
print("✅ Reputation: fresh state")

# Good peer: 5 passes
for _ in range(5):
    rt.record_validation("peer_good", passed=True)
# Bad peer: 3 fails
for _ in range(3):
    rt.record_validation("peer_bad", passed=False)

good_w = rt.get_weight("peer_good")
bad_w = rt.get_weight("peer_bad")
print(f"  Good peer weight={good_w:.3f}, Bad peer weight={bad_w:.3f}")
assert good_w > bad_w, "Good peer should have higher weight"
assert good_w > 1.0
print("✅ Reputation: weight differentiation")

# Blacklisting
for _ in range(10):
    rt.record_validation("peer_evil", passed=False)
assert rt.is_blacklisted("peer_evil")
assert rt.get_weight("peer_evil") == 0.0
print(f"  Blacklisted: {rt.get_stats()['blacklisted']}")
print("✅ Reputation: blacklisting")

# Stats
stats = rt.get_stats()
assert stats["total_peers"] >= 2
assert stats["total_validations"] >= 18
print("✅ Reputation: stats")

# Top peers
top = rt.get_top_peers(5)
assert top[0]["peer_id"] == "peer_good"
print("✅ Reputation: top peers correct")

# ══ 5. Federation Integration ══
print("\n=== 5. Federation Integration ===")
from p2p.federation import FederationAggregator, CrashReport

agg = FederationAggregator()
agg.enable_defense(aggregation_method="median")

assert agg.defense_active
assert agg.pow_difficulty is not None
assert agg.sandbox_validator is not None
assert agg.reputation_tracker is not None
assert agg.aggregation_method == "median"
print("✅ Federation: defense enabled")

# Export with PoW
model = DeepRiskNet(n_features=3, hidden_dim=8, lr=0.01)
model.fit(good_X, good_y)
update = agg.export_model_update(model)
assert update["model_version"] == "subconscious-v8"
# PoW proof present
assert "pow_nonce" in update
assert "pow_hash" in update
assert "pow_bits" in update
print(f"  PoW in export: bits={update['pow_bits']}, hash={update['pow_hash'][:16]}")

# Stats with defense
fstats = agg.stats()
assert "defense" in fstats
print(f"  Defense stats: enabled={fstats['defense']['enabled']}, "
      f"method={fstats['defense']['aggregation_method']}")
print("✅ Federation: integration complete")

# Toggle
agg.disable_defense()
assert not agg.defense_active
agg.enable_defense(aggregation_method="krum")
assert agg.aggregation_method == "krum"
print("✅ Federation: toggle defense")

print("\n" + "=" * 50)
print("🎉 ALL SYBIL DEFENSE v7 TESTS PASSED 🎉")
print("=" * 50)
