"""ww/core/subconscious complete test suite"""
import os
import random
import tempfile

random.seed(42)


# ══ 1. FeatureExtractor ══

def test_feature_extractor_dimensions():
    from core.features import FeatureExtractor

    fex = FeatureExtractor()
    vec = fex.extract()
    assert len(vec) == 32, f"Expected 32 features (padded), got {len(vec)}"
    assert vec[12] == 0.0 and vec[14] == 0.0, f"Provider one-hot should have one active dim, got {vec[12:15]}"
    assert all(v == 0.0 for v in vec[:12]), f"Expected first 12 dims zero, got {vec[:12]}"


def test_feature_extractor_error_counting():
    from core.features import FeatureExtractor

    fex = FeatureExtractor()
    for i in range(5):
        fex.observe_action("search", success=False, latency=3.0 * (i + 1), token_count=500)
    vec = fex.extract()
    assert vec[0] == 5, f"Expected 5 consecutive errors, got {vec[0]}"


def test_feature_extractor_error_reset_after_success():
    from core.features import FeatureExtractor

    fex = FeatureExtractor()
    for i in range(5):
        fex.observe_action("search", success=False, latency=3.0 * (i + 1), token_count=500)
    fex.observe_action("read_file", success=True, latency=0.5, token_count=100)
    vec = fex.extract()
    assert vec[0] == 0, f"After success, errors should reset, got {vec[0]}"


def test_feature_extractor_recall_counting():
    from core.features import FeatureExtractor

    fex = FeatureExtractor()
    for _ in range(5):
        fex.observe_memory_recall()
    vec = fex.extract()
    assert vec[9] == 5, f"Expected 5 recalls, got {vec[9]}"


def test_feature_extractor_normalization():
    from core.features import FeatureExtractor

    fex = FeatureExtractor()
    normed = fex.normalize([5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120, 1, 0, 0]
                           + [0.0] * 17)
    for v in normed:
        assert 0 <= v <= 1, f"Normalized value {v} out of range"
    assert len(normed) == 32


def test_feature_extractor_stats():
    from core.features import FeatureExtractor

    fex = FeatureExtractor()
    for i in range(5):
        fex.observe_action("search", success=False, latency=3.0 * (i + 1), token_count=500)
    fex.observe_action("read_file", success=True, latency=0.5, token_count=100)
    stats = fex.stats()
    assert stats["observations"] >= 6
    assert stats["unique_tools"] == 2


# ══ 2. DeepRiskNet ══

X_SMALL = [[0, 0, 0, 0, 0], [1, 0, 1, 1, 0], [0, 1, 0, 0, 1], [1, 1, 1, 1, 1]]
Y_SMALL = [0.0, 0.5, 0.8, 1.0]


def test_deep_risk_net_create():
    from core.predictor import DeepRiskNet
    model = DeepRiskNet(n_features=5, hidden_dim=8, lr=0.01)
    assert model._param_count > 0


def test_deep_risk_net_predict_before_fit():
    from core.predictor import DeepRiskNet
    model = DeepRiskNet(n_features=5, hidden_dim=8, lr=0.01)
    for x in X_SMALL:
        p = model.predict(x)
        assert 0 <= p.crash_risk <= 1


def test_deep_risk_net_fit_and_predict():
    from core.predictor import DeepRiskNet
    model = DeepRiskNet(n_features=5, hidden_dim=8, lr=0.01)
    model.fit(X_SMALL, Y_SMALL)
    preds = [model.predict(x) for x in X_SMALL]
    assert len(preds) == 4
    assert all(0 <= p.crash_risk <= 1 for p in preds)


def test_deep_risk_net_serialization():
    from core.predictor import DeepRiskNet
    model = DeepRiskNet(n_features=5, hidden_dim=8, lr=0.01)
    model.fit(X_SMALL, Y_SMALL)
    preds_before = [model.predict(x) for x in X_SMALL]

    model2 = DeepRiskNet.from_dict(model.to_dict())
    preds_after = [model2.predict(x) for x in X_SMALL]
    for a, b in zip(preds_before, preds_after):
        assert abs(a.crash_risk - b.crash_risk) < 0.001


def test_deep_risk_net_json_serialization():
    from core.predictor import DeepRiskNet
    model = DeepRiskNet(n_features=5, hidden_dim=8, lr=0.01)
    model.fit(X_SMALL, Y_SMALL)

    json_str = model.to_json()
    model3 = DeepRiskNet.from_json(json_str)
    assert model3._param_count == model._param_count


def test_deep_risk_net_empty_model():
    from core.predictor import DeepRiskNet
    model = DeepRiskNet(n_features=5, hidden_dim=8)
    assert model.empty()
    pred = model.predict([0, 0, 0, 0, 0])
    assert 0 <= pred.crash_risk <= 1


# ══ 3. RewindEngine ══

def test_rewind_no_intervention_when_healthy():
    from core.subconscious.rewind import RewindEngine
    re = RewindEngine(rewind_threshold=0.7, max_rewinds_per_session=3)
    should, reason = re.should_rewind([0, 0, 0, 0, 0, 0, 0, 1, 3, 2, 0, 5], 0.01, 0, 0)
    assert not should


def test_rewind_detects_high_errors():
    from core.subconscious.rewind import RewindEngine
    re = RewindEngine(rewind_threshold=0.7, max_rewinds_per_session=3)
    should, reason = re.should_rewind([5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 0.85, 1, 12)
    assert should


def test_rewind_detects_tool_loop():
    from core.subconscious.rewind import RewindEngine
    re = RewindEngine(rewind_threshold=0.7, max_rewinds_per_session=3)
    should, reason = re.should_rewind([3, 5, 10, 0, 200, 8, 2, 0, 1, 15, 0, 60], 0.75, 2, 8)
    assert should


def test_rewind_execute_creates_event():
    from core.subconscious.rewind import RewindEngine
    re = RewindEngine(rewind_threshold=0.7, max_rewinds_per_session=3)
    event = re.execute_rewind("test_rewind", [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 0.85)
    assert event.trigger_reason == "test_rewind"
    assert event.intuition_message is not None


def test_rewind_stats():
    from core.subconscious.rewind import RewindEngine
    re = RewindEngine(rewind_threshold=0.7, max_rewinds_per_session=3)
    re.execute_rewind("test_rewind", [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 0.85)
    stats = re.stats()
    assert stats["rewind_count"] == 1


# ══ 4. Federation ══

def test_federation_submit_local_report():
    from p2p.federation import CrashReport, FederationAggregator
    fa = FederationAggregator(data_dir=tempfile.mkdtemp())
    report = CrashReport(
        trigger_event="tool_loop",
        state_vector_before_crash=[5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120],
        failed_tool_sequence=["search", "search", "search", "read_file"],
        successful_correction="rewind_and_block_search",
        reward=1.0,
    )
    sig = fa.submit_local_report(report)
    assert len(sig) == 16


def test_federation_signature_verification():
    from p2p.federation import CrashReport, FederationAggregator
    fa = FederationAggregator(data_dir=tempfile.mkdtemp())
    report = CrashReport(
        trigger_event="tool_loop",
        state_vector_before_crash=[5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120],
        failed_tool_sequence=["search", "search", "search", "read_file"],
        successful_correction="rewind_and_block_search",
        reward=1.0,
    )
    fa.submit_local_report(report)
    assert report.verify()


def test_federation_receive_peer_report():
    from p2p.federation import CrashReport, FederationAggregator
    fa = FederationAggregator(data_dir=tempfile.mkdtemp())
    report = CrashReport(
        trigger_event="timeout",
        state_vector_before_crash=[3, 2, 45, 1, 800, 15, 1, 0, 2, 25, 0, 300],
        failed_tool_sequence=["shell", "shell", "shell"],
        successful_correction="switch_model",
        reward=0.0,
    )
    fa.receive_peer_crash(report, "peer_node_abc")
    stats = fa.stats()
    assert stats["remote_reports"] == 1


def test_federation_stats():
    from p2p.federation import CrashReport, FederationAggregator
    fa = FederationAggregator(data_dir=tempfile.mkdtemp())
    report = CrashReport(
        trigger_event="tool_loop",
        state_vector_before_crash=[5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120],
        failed_tool_sequence=["search", "search", "search", "read_file"],
        successful_correction="rewind_and_block_search",
        reward=1.0,
    )
    fa.submit_local_report(report)
    stats = fa.stats()
    assert stats["local_reports"] == 1


def test_federation_model_update_export_import():
    from p2p.federation import FederationAggregator
    from core.predictor import DeepRiskNet
    fa = FederationAggregator(data_dir=tempfile.mkdtemp())

    model_export = DeepRiskNet(n_features=12, hidden_dim=8, lr=0.01)
    X_train = [[0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0], [1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1],
               [1, 1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1], [0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0]]
    y_train = [0.0, 1.0, 1.0, 0.0]
    model_export.fit(X_train, y_train)

    update = fa.export_model_update(model_export)
    assert "params" in update
    assert update["size_bytes"] > 100

    model_import = DeepRiskNet(n_features=12, hidden_dim=8)
    assert fa.import_model_update(update, model_import)


def test_federation_persistence():
    from p2p.federation import FederationAggregator
    tmpdir = tempfile.mkdtemp()
    fa = FederationAggregator(data_dir=tmpdir)
    fa._save()
    assert os.path.isfile(os.path.join(tmpdir, "reports.json"))


# ══ 5. Subconscious Integration ══

def test_subconscious_fresh_state():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    status = sc.get_status()
    assert status["enabled"]
    assert status["model_trained"] is False


def test_subconscious_heuristic_prediction():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)
    sc.observe_action("search", success=False, latency=8.0, token_count=600)
    sc.observe_action("search", success=False, latency=12.0, token_count=700)
    sc.observe_spiral(1, 2)

    risk = sc.predict()
    assert 0 <= risk.crash_risk <= 1


def test_subconscious_intervention():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)
    sc.observe_spiral(1, 2)

    interv = sc.should_intervene()
    assert isinstance(interv["intervene"], bool)


def test_subconscious_training():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)

    for _ in range(8):
        sc.record_training_sample(
            [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 1.0,
        )
        sc.record_training_sample(
            [0, 1, 2, 0, 50, 2, 3, 1, 5, 5, 0, 10], 0.0,
        )
        sc.record_training_sample(
            [3, 2, 15, 0, 200, 5, 1, 0, 2, 10, 0, 60], 1.0,
        )
        sc.record_training_sample(
            [0, 0, 1, 0, 20, 1, 0, 1, 3, 2, 0, 5], 0.0,
        )

    result = sc.train()
    assert result["trained"]
    assert result["model_size"] != "0 B"


def test_subconscious_trained_prediction():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)

    for _ in range(8):
        sc.record_training_sample(
            [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 1.0,
        )
        sc.record_training_sample(
            [0, 1, 2, 0, 50, 2, 3, 1, 5, 5, 0, 10], 0.0,
        )
        sc.record_training_sample(
            [3, 2, 15, 0, 200, 5, 1, 0, 2, 10, 0, 60], 1.0,
        )
        sc.record_training_sample(
            [0, 0, 1, 0, 20, 1, 0, 1, 3, 2, 0, 5], 0.0,
        )
    sc.train()

    risk = sc.predict()
    assert 0 <= risk.crash_risk <= 1


def test_subconscious_execute_rewind():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)

    result = sc.execute_rewind(
        "test_rewind_from_integration",
        [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120],
        0.85,
    )
    assert result["trigger_reason"] == "test_rewind_from_integration"


def test_subconscious_federation_integration():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)
    sc.observe_action("search", success=False, latency=8.0, token_count=600)
    sc.observe_action("search", success=False, latency=12.0, token_count=700)
    sc.observe_spiral(1, 2)
    sc.execute_rewind(
        "test_rewind_from_integration",
        [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120],
        0.85,
    )

    stats = sc.federation.stats()
    assert stats["local_reports"] >= 1


def test_subconscious_status_after_training():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)

    for _ in range(8):
        sc.record_training_sample(
            [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 1.0,
        )
        sc.record_training_sample(
            [0, 1, 2, 0, 50, 2, 3, 1, 5, 5, 0, 10], 0.0,
        )
    sc.train()

    status = sc.get_status()
    assert status["model_trained"]
    assert status["training_count"] >= 1


def test_subconscious_stats():
    from core.subconscious import Subconscious
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=os.path.join(tempfile.mkdtemp(), "test_model.json"),
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)
    stats = sc.get_stats()
    assert stats["training_buffer"] >= 0


def test_subconscious_model_persistence():
    from core.subconscious import Subconscious
    model_path = os.path.join(tempfile.mkdtemp(), "test_model.json")
    sc = Subconscious(
        enabled=True, hidden_dim=16,
        model_path=model_path,
        p2p_enabled=False, blockchain_enabled=False,
    )
    sc.observe_spiral(0, 1)
    sc.observe_action("search", success=False, latency=5.0, token_count=500)

    for _ in range(8):
        sc.record_training_sample(
            [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120], 1.0,
        )
        sc.record_training_sample(
            [0, 1, 2, 0, 50, 2, 3, 1, 5, 5, 0, 10], 0.0,
        )
    sc.train()

    assert os.path.isfile(sc.model_path), f"Model not saved at {sc.model_path}"
    size = os.path.getsize(sc.model_path)
    assert size > 0
