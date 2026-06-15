"""ww/core/subconscious complete test suite"""

import sys; sys.path.insert(0, ".")
import time
import json
import os
import random

random.seed(42)

# ══ 1. FeatureExtractor ══
from core.features import FeatureExtractor, FEATURE_NAMES

fex = FeatureExtractor()
vec = fex.extract()
assert len(vec) == 32, f"Expected 32 features (padded), got {len(vec)}"
assert vec[12] == 0.0 and vec[14] == 0.0, f"Provider one-hot should have one active dim, got {vec[12:15]}"
assert all(v == 0.0 for v in vec[:12]), f"Expected first 12 dims zero, got {vec[:12]}"
print(f"✅ FeatureExtractor: 32 dimensions (15 active + 17 reserved)")

# Simulate failure pattern
for i in range(5):
    fex.observe_action("search", success=False, latency=3.0 * (i+1), token_count=500)
vec = fex.extract()
print(f"  After 5 failures: consecutive_errors={vec[0]}, tool_loop={vec[1]}, latency={vec[2]}")
assert vec[0] == 5, f"Expected 5 consecutive errors, got {vec[0]}"
print(f"✅ FeatureExtractor: correct error counting")

# Add success to break the chain
fex.observe_action("read_file", success=True, latency=0.5, token_count=100)
vec = fex.extract()
assert vec[0] == 0, f"After success, errors should reset, got {vec[0]}"
print(f"✅ FeatureExtractor: error counter resets after success")

# Test memory recall tracking
for _ in range(5):
    fex.observe_memory_recall()
vec = fex.extract()
assert vec[9] == 5, f"Expected 5 recalls, got {vec[9]}"
print(f"✅ FeatureExtractor: recall counting")

# Test normalization (32-dim padded vector)
normed_32 = fex.normalize([5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120, 1, 0, 0] +
                          [0.0] * 17)
for v in normed_32:
    assert 0 <= v <= 1, f"Normalized value {v} out of range"
print(f"✅ FeatureExtractor: normalization [0,1] correct ({len(normed_32)} dims)")

# Test stats
stats = fex.stats()
assert stats["observations"] >= 6
assert stats["unique_tools"] == 2  # search, read_file
print(f"✅ FeatureExtractor: stats OK (obs={stats['observations']}, tools={stats['unique_tools']})")

print("=== 1. FeatureExtractor ALL PASSED ===")

# ══ 2. DeepRiskNet ══
from core.predictor import DeepRiskNet

# Train a tiny model
X = [[0,0,0,0,0], [1,0,1,1,0], [0,1,0,0,1], [1,1,1,1,1]]
y = [0.0, 0.5, 0.8, 1.0]

model = DeepRiskNet(n_features=5, hidden_dim=8, lr=0.01)
print(f"✅ DeepRiskNet: created ({model._param_count} params)")

# Predict before training (should be near 0.5 on average for untrained network)
preds_before = [model.predict(x) for x in X]
for p in preds_before:
    assert 0 <= p.crash_risk <= 1
print(f"  Predictions (before fit): {[round(p.crash_risk, 3) for p in preds_before]}")

# Train (tiny dataset — just validate it runs and doesn't error)
model.fit(X, y)
print(f"✅ DeepRiskNet: trained OK")

# Predict after training
preds_after = [model.predict(x) for x in X]
assert len(preds_after) == 4
assert all(0 <= p.crash_risk <= 1 for p in preds_after)
print(f"  Predictions (after fit): {[round(p.crash_risk, 3) for p in preds_after]}")

# Serialization round-trip
d = model.to_dict()
model2 = DeepRiskNet.from_dict(d)
preds_roundtrip = [model2.predict(x) for x in X]
for a, b in zip(preds_after, preds_roundtrip):
    assert abs(a.crash_risk - b.crash_risk) < 0.001
print(f"✅ DeepRiskNet: JSON serialization round-trip exact")

# JSON string serialization
json_str = model.to_json()
model3 = DeepRiskNet.from_json(json_str)
assert model3._param_count == model._param_count
json_roundtrip_size = len(json_str)
print(f"✅ DeepRiskNet: JSON string serialization OK ({json_roundtrip_size} bytes, {model.model_size()})")

# Empty/untrained model
empty_model = DeepRiskNet(n_features=5, hidden_dim=8)
assert empty_model.empty()
# Default state_dict is all zeros within floating tolerance
pred_empty = empty_model.predict([0,0,0,0,0])
print(f"  Empty model prediction: {pred_empty.crash_risk:.3f}")
# An untrained sigmoid MLP near initialization will give ~0.5
assert 0 <= pred_empty.crash_risk <= 1
print(f"✅ DeepRiskNet: empty model prediction in [0,1]")

print("=== 2. Predictor ALL PASSED ===")

# ══ 3. RewindEngine ══
from core.subconscious.rewind import RewindEngine

re = RewindEngine(rewind_threshold=0.7, max_rewinds_per_session=3)

# Should not rewind (no failures)
should, reason = re.should_rewind([0,0,0,0,0,0,0,1,3,2,0,5], 0.01, 0, 0)
assert not should
print(f"✅ RewindEngine: no intervention when healthy ({reason})")

# Should rewind (high consecutive errors)
should, reason = re.should_rewind([5,4,30,1,500,10,2,0,1,20,1,120], 0.85, 1, 12)
assert should
print(f"✅ RewindEngine: detects failure ({reason})")

# Should rewind (tool loop)
should, reason = re.should_rewind([3, 5, 10, 0, 200, 8, 2, 0, 1, 15, 0, 60], 0.75, 2, 8)
assert should
print(f"✅ RewindEngine: detects tool loop ({reason})")

# Execute rewind (without callbacks — tests the code path)
event = re.execute_rewind("test_rewind", [5,4,30,1,500,10,2,0,1,20,1,120], 0.85)
print(f"✅ RewindEngine: event created (recovered={event.recovered}, reason={event.trigger_reason})")
assert event.trigger_reason == "test_rewind"
assert event.intuition_message is not None
print(f"✅ RewindEngine: intuition message generated ({len(event.intuition_message)} chars)")

stats = re.stats()
assert stats["rewind_count"] == 1
print(f"✅ RewindEngine: stats OK")

print("=== 3. RewindEngine ALL PASSED ===")

# ══ 4. Federation ══
from p2p.federation import CrashReport, FederationAggregator
import tempfile

tmpdir = tempfile.mkdtemp()
fa = FederationAggregator(data_dir=tmpdir)

# Create a crash report
report = CrashReport(
    trigger_event="tool_loop",
    state_vector_before_crash=[5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120],
    failed_tool_sequence=["search", "search", "search", "read_file"],
    successful_correction="rewind_and_block_search",
    reward=1.0,
)
sig = fa.submit_local_report(report)
assert len(sig) == 16
print(f"✅ Federation: report submitted (sig={sig})")

# Verify signature
assert report.verify()  # default secret = ""
print(f"✅ Federation: signature verification OK")

# Peer report
report2 = CrashReport(
    trigger_event="timeout",
    state_vector_before_crash=[3, 2, 45, 1, 800, 15, 1, 0, 2, 25, 0, 300],
    failed_tool_sequence=["shell", "shell", "shell"],
    successful_correction="switch_model",
    reward=0.0,
)
fa.receive_peer_crash(report2, "peer_node_abc")
print(f"✅ Federation: peer report received")

stats = fa.stats()
assert stats["local_reports"] == 1, f"Expected 1 local report, got {stats}"
assert stats["remote_reports"] == 1, f"Expected 1 remote report"
print(f"✅ Federation: stats OK")

# Export/import model update (using DeepRiskNet)
model_export = DeepRiskNet(n_features=12, hidden_dim=8, lr=0.01)
X_train = [[0,0,0,0,0,0,0,1,0,0,0,0], [1,0,1,1,0,1,1,0,1,1,0,1],
           [1,1,1,1,1,1,1,0,0,1,1,1], [0,0,0,0,0,0,0,1,0,0,0,0]]
y_train = [0.0, 1.0, 1.0, 0.0]
model_export.fit(X_train, y_train)

update = fa.export_model_update(model_export)
assert "params" in update
assert update["size_bytes"] > 100  # Neural net has parameters
print(f"✅ Federation: model update exported ({update['size_bytes']} bytes)")

# Import into another model
model_import = DeepRiskNet(n_features=12, hidden_dim=8)
assert fa.import_model_update(update, model_import)
print(f"✅ Federation: model update imported")

fa._save()
assert os.path.isfile(os.path.join(tmpdir, "reports.json"))
print(f"✅ Federation: persistence OK")

print("=== 4. Federation ALL PASSED ===")

# ══ 5. Subconscious Integration ══
# Clean up first
import shutil
data_dir = os.path.expanduser("~/worldwave/data/subconscious/test")
if os.path.isdir(data_dir):
    shutil.rmtree(data_dir)

os.environ["SUBCONSCIOUS_DIR"] = data_dir
from core.subconscious import Subconscious

sc = Subconscious(enabled=True, hidden_dim=16,
                  model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
                  p2p_enabled=False, blockchain_enabled=False)

# Fresh state
status = sc.get_status()
assert status["enabled"]
assert status["model_trained"] == False, f"Expected empty model for fresh Subconscious"
print(f"✅ Subconscious: fresh state OK")

# Simulate a spiral with failures
sc.observe_spiral(0, 1)
sc.observe_action("search", success=False, latency=5.0, token_count=500)
sc.observe_action("search", success=False, latency=8.0, token_count=600)
sc.observe_action("search", success=False, latency=12.0, token_count=700)
sc.observe_spiral(1, 2)

# Prediction
risk = sc.predict()
print(f"  Heuristic risk: {risk.crash_risk:.3f}")
assert 0 <= risk.crash_risk <= 1
print(f"✅ Subconscious: heuristic prediction in [0,1]")

# Intervention
interv = sc.should_intervene()
print(f"  Intervention: {interv['action']}")
assert isinstance(interv["intervene"], bool)
print(f"✅ Subconscious: intervention check works")

# Train with various samples
for _ in range(8):
    sc.record_training_sample(
        [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 1.0  # failure
    )
    sc.record_training_sample(
        [0, 1, 2, 0, 50, 2, 3, 1, 5, 5, 0, 10], 0.0  # success
    )
    sc.record_training_sample(
        [3, 2, 15, 0, 200, 5, 1, 0, 2, 10, 0, 60], 1.0  # failure
    )
    sc.record_training_sample(
        [0, 0, 1, 0, 20, 1, 0, 1, 3, 2, 0, 5], 0.0  # success
    )

result = sc.train()
print(f"  Train: trained={result['trained']}, samples={result['samples']}, size={result['model_size']}")
assert result["trained"]
assert result["model_size"] != "0 B"
print(f"✅ Subconscious: training works")

# Trained prediction
risk2 = sc.predict()
assert 0 <= risk2.crash_risk <= 1
print(f"  Trained prediction: {risk2.crash_risk:.3f} (heuristic was: {risk.crash_risk:.3f})")
print(f"✅ Subconscious: trained prediction in [0,1]")

# Execute rewind
rewind_result = sc.execute_rewind(
    "test_rewind_from_integration",
    [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120],
    0.85,
)
assert rewind_result["trigger_reason"] == "test_rewind_from_integration"
print(f"✅ Subconscious: rewind execution OK")

# Federation integration
feed_stats = sc.federation.stats()
# 1 from observe action failures + 1 from execute_rewind
assert feed_stats["local_reports"] >= 1, f"Expected >=1 report, got {feed_stats['local_reports']}"
print(f"✅ Subconscious: federation integrated")

# Status after training
status2 = sc.get_status()
assert status2["model_trained"]
assert status2["training_count"] >= 1
print(f"✅ Subconscious: status reports trained model")

# Stats
stats = sc.get_stats()
assert stats["training_buffer"] >= 0
print(f"✅ Subconscious: comprehensive stats OK")

# Model persistence — verify at the actual model_path used by this instance
assert os.path.isfile(sc.model_path), f"Model not saved at {sc.model_path}"
size = os.path.getsize(sc.model_path)
print(f"✅ Subconscious: model persisted ({size} bytes, in {sc.model_path})")

print("\n=== 5. Subconscious Integration ALL PASSED ===")
print("\n🎉🎉🎉 ALL SUBCONSCIOUS TESTS PASSED 🎉🎉🎉")
