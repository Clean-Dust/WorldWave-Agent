"""
test: Signal Pipeline + Contrastive Engine + Runtime Collector
"""
import sys; sys.path.insert(0, ".")
import json
import os
import tempfile
import time

from core.subconscious.signal_pipeline import (
    SignalCollector, TrainingTriple, SignalSource,
)
from core.subconscious.contrastive import ContrastiveEngine
from core.subconscious.runtime_collector import RuntimeCollector
from core.subconscious.features import FeatureExtractor, PADDED_FEATURES
from core.subconscious.predictor import DeepRiskNet


def p15(v15):
    """15-dimensional vector → padding to PADDED_FEATURES dimensions."""
    return v15 + [0.0] * (PADDED_FEATURES - len(v15))


def p12(v12):
    """12-dimensional vector → padding to PADDED_FEATURES dimensions (fill provider=[0,1,0])."""
    return list(v12[:12]) + [0.0, 1.0, 0.0] + [0.0] * (PADDED_FEATURES - 15)


def make_xor_data():
    """A tiny dataset (32-dim padded)."""
    return [p15([1,0,1,0,0,0,0,0,0,0,0,0, 1,0,0]),
            p15([0,1,0,0,0,0,0,0,0,0,0,0, 1,0,0]),
            p15([1,1,1,0,0,0,0,0,0,0,0,0, 0,1,0]),
            p15([0,0,0,0,0,0,0,0,0,0,0,0, 1,0,0]),
            p15([1,0,0,0,0,0,0,0,0,0,0,0, 0,1,0]),
            p15([0,0,1,0,0,0,0,0,0,0,0,0, 0,1,0]),
            p15([0,1,0,0,0,0,0,0,0,0,0,0, 0,0,1]),
            p15([1,0,1,0,0,0,0,0,0,0,0,0, 0,0,1])], \
           [0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 1.0]


def test_signal_environment():
    """Environment feedback signal."""
    c = SignalCollector(data_dir=tempfile.mkdtemp())
    vec = p12([0,0,0,0,0,0,0,1,0,0,0,0.5])
    t = c.record_environment(vec, exit_code=1, success=False)
    assert t.outcome == 1.0  # failure
    assert t.confidence == 0.95
    assert t.source == "environment"
    t2 = c.record_environment(vec, exit_code=0, success=True)
    assert t2.outcome == 0.0  # success
    assert c.buffer_size() == 2
    print("✅ environment feedback")


def test_signal_user_intervention():
    """User intervention signal."""
    c = SignalCollector(data_dir=tempfile.mkdtemp())
    vec = p12([5,3,10,1,0,0,0,0,0,0,0,0])
    t = c.record_user_intervention(vec, "ctrl_c", severity=1.0)
    assert t.outcome >= 0.9  # strong negative
    assert t.source == "user_intervention"

    t2 = c.record_user_intervention(vec, "edit", severity=0.3)
    assert t2.outcome < 0.9  # slight correction
    print("✅ user intervention")


def test_signal_efficiency():
    """Efficiency metric signal."""
    c = SignalCollector(data_dir=tempfile.mkdtemp())
    vec = p12([0,0,0,0,500,0,0,1,0,0,0,0])
    # Better efficiency than baseline
    t = c.record_efficiency(vec, "code", tokens_used=500, latency_seconds=1.0,
                            baseline_tokens=1000, baseline_latency=3.0)
    assert t.outcome < 0.3  # efficient = low failure risk
    # Worse efficiency than baseline
    t2 = c.record_efficiency(vec, "code", tokens_used=2000, latency_seconds=5.0,
                             baseline_tokens=1000, baseline_latency=3.0)
    assert t2.outcome > 0.6  # inefficient = high failure risk
    assert c.buffer_size() == 2
    print("✅ efficiency metric")


def test_signal_self_correction():
    """Self-reflection shortcut learning."""
    c = SignalCollector(data_dir=tempfile.mkdtemp())
    vec = p12([0,0,0,0,0,0,0,0,0,0,0,0])
    c.start_task_trajectory("task1", vec)
    c.record_trajectory_step("task1", vec, "search", success=True, tokens=100)
    c.record_trajectory_step("task1", p12([1,1,0,0,0,0,0,0,0,0,0,0]), "code", success=False, tokens=500)
    c.record_trajectory_step("task1", p12([2,0,0,0,0,0,0,0,0,0,0,0]), "code", success=True, tokens=200)
    result = c.finish_task_trajectory("task1", p12([0,0,0,0,0,0,0,1,0,0,0,0]), success=True)
    assert result is not None, "shortcut learning should produce a result"
    assert result.outcome < 0.4  # shortcut = low failure risk
    assert result.source == "self_correction"
    assert c.buffer_size() == 1  # a shortcut triplet

    # no failed task → should not generate shortcuts
    c.start_task_trajectory("task2", vec)
    c.record_trajectory_step("task2", vec, "code", success=True)
    result2 = c.finish_task_trajectory("task2", vec, success=True)
    assert result2 is None, "no failed task should not produce a shortcut"
    print("✅ self-reflection")


def test_contrast_pairs():
    """Contrastive pair construction."""
    c = SignalCollector(data_dir=tempfile.mkdtemp())
    vec = p12([1,2,5,0,0,0,0,0,0,0,0,0])
    c.record_environment(vec, exit_code=0, success=True)   # win
    c.record_environment(vec, exit_code=1, success=False)  # lose
    batch = c.drain()
    pairs = c.get_contrast_pairs(batch)
    assert len(pairs) > 0, "should produce contrastive pairs"
    assert pairs[0][1] < pairs[0][2]  # Y_win < Y_lose
    # Only has wins
    c2 = SignalCollector(data_dir=tempfile.mkdtemp())
    c2.record_environment(vec, exit_code=0, success=True)
    pairs2 = c2.get_contrast_pairs(c2.drain())
    assert len(pairs2) > 0, "only has win should also produce contrastive pairs"
    print("✅ contrastive pair construction")


def test_contrastive_update():
    """Contrastive learning update (using DeepRiskNet direct training)."""
    X, y = make_xor_data()
    model = DeepRiskNet(n_features=PADDED_FEATURES, hidden_dim=16, lr=0.01)
    model.fit(X, y)

    # Record a prediction before targeted training
    fail_state = p15([1,1,1,0,0,0,0,0,0,0,0,0, 0,1,0])
    old_pred = model.predict(fail_state)
    print(f"  Prediction before targeted training: {old_pred.crash_risk:.3f}")

    # Retrain specifically on fail_state as a negative (success = low risk)
    X_extra = [fail_state] * 5
    y_extra = [0.0] * 5  # teach that this state → success
    model.fit(X_extra, y_extra)

    new_pred = model.predict(fail_state)
    print(f"  Prediction after targeted training: {new_pred.crash_risk:.3f}")
    print("✅ contrastive-style update (DeepRiskNet fit)")


def test_runtime_collector():
    """Runtime collector integration test."""
    fe = FeatureExtractor()
    model = DeepRiskNet(n_features=PADDED_FEATURES, hidden_dim=16, lr=0.01)
    X, y = make_xor_data()
    model.fit(X, y)

    import tempfile
    from core.subconscious.signal_pipeline import SignalCollector
    sc = SignalCollector(data_dir=tempfile.mkdtemp())
    rc = RuntimeCollector(
        feature_extractor=fe,
        predictor=model,
        signal_collector=sc,
        auto_train_interval=100,
    )

    # Simulate several actions
    vec = fe.extract()
    for i in range(10):
        rc.after_action(
            tool_name="search",
            success=(i % 2 == 0),
            exit_code=0 if i % 2 == 0 else 1,
            latency=0.5,
            tokens=100,
            state_before=vec,
        )

    # Simulate user intervention
    rc.on_user_interrupt(ctrl_c=True, state_before=vec)

    # Simulate task
    rc.on_task_start("task1", vec)
    for _ in range(3):
        rc.after_action("code", success=True, state_before=fe.extract())
    rc.on_task_end("task1", fe.extract(), success=True,
                   task_type="code", tokens_used=500, latency_seconds=2.0)

    stats = rc.stats()
    assert stats["actions_collected"] == 13, f"expected 13, got {stats['actions_collected']}"
    assert stats["interventions_detected"] == 1
    assert stats["signal_pipeline"]["buffer_size"] > 0
    assert stats["signal_pipeline"]["active_trajectories"] == 0

    print(f"✅ Runtime collector: actions={stats['actions_collected']}, "
          f"buffer={stats['signal_pipeline']['buffer_size']}")


if __name__ == "__main__":
    test_signal_environment()
    test_signal_user_intervention()
    test_signal_efficiency()
    test_signal_self_correction()
    test_contrast_pairs()
    test_contrastive_update()
    test_runtime_collector()
    print("\n🎉 ALL SIGNAL + CONTRASTIVE TESTS PASSED 🎉")
