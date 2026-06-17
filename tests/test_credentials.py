"""Tests: credentials module — CredentialManager, CredentialPool, PooledKey, CredentialStore, utilities."""

import json
import os
import sys
import time


sys.path.insert(0, ".")

from core.credentials import (
    CredentialManager,
    CredentialPool,
    CredentialStore,
    KeyStatus,
    PooledKey,
    get_credential_manager,
    get_credential_store,
    mask_secret,
    sanitize_output,
)


# ── PooledKey ──


class TestPooledKey:
    def test_defaults(self):
        pk = PooledKey(key="sk-abc123")
        assert pk.key == "sk-abc123"
        assert pk.label == ""
        assert pk.status == KeyStatus.ACTIVE
        assert pk.provider == ""
        assert pk.priority == 0
        assert pk.models == []
        assert pk.rate_limit_rpm == 0
        assert pk.last_used == 0
        assert pk.error_count == 0
        assert pk.total_calls == 0

    def test_is_available_active(self):
        pk = PooledKey(key="k", status=KeyStatus.ACTIVE)
        assert pk.is_available is True

    def test_is_available_rate_limited(self):
        pk = PooledKey(key="k", status=KeyStatus.RATE_LIMITED)
        assert pk.is_available is True

    def test_is_available_exhausted(self):
        pk = PooledKey(key="k", status=KeyStatus.EXHAUSTED)
        assert pk.is_available is False

    def test_is_available_failed(self):
        pk = PooledKey(key="k", status=KeyStatus.FAILED)
        assert pk.is_available is False

    def test_is_available_disabled(self):
        pk = PooledKey(key="k", status=KeyStatus.DISABLED)
        assert pk.is_available is False

    def test_cooldown_remaining_active(self):
        pk = PooledKey(key="k", status=KeyStatus.ACTIVE)
        assert pk.cooldown_remaining == 0

    def test_cooldown_remaining_rate_limited_fresh(self):
        pk = PooledKey(key="k", status=KeyStatus.RATE_LIMITED, last_used=time.time())
        remaining = pk.cooldown_remaining
        assert 0 <= remaining <= 60

    def test_cooldown_remaining_rate_limited_expired(self):
        pk = PooledKey(
            key="k", status=KeyStatus.RATE_LIMITED, last_used=time.time() - 61
        )
        assert pk.cooldown_remaining == 0


# ── CredentialPool ──


class TestCredentialPool:
    def test_add_key(self):
        pool = CredentialPool(provider="deepseek")
        pk = pool.add_key("sk-abc", label="primary", priority=0)
        assert pk.key == "sk-abc"
        assert pk.label == "primary"
        assert pk.provider == "deepseek"
        assert len(pool.keys) == 1

    def test_add_key_auto_label(self):
        pool = CredentialPool(provider="openai")
        pk = pool.add_key("sk-xyz")
        assert pk.label == "key-1"

    def test_add_key_deduplicate(self):
        pool = CredentialPool(provider="deepseek")
        a = pool.add_key("sk-abc", label="first")
        b = pool.add_key("sk-abc", label="second")
        assert len(pool.keys) == 1
        assert b is a

    def test_add_key_sorts_by_priority(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", priority=2)
        pool.add_key("k2", priority=0)
        pool.add_key("k3", priority=1)
        assert [k.priority for k in pool.keys] == [0, 1, 2]

    def test_remove_key_by_label(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a")
        pool.add_key("k2", label="b")
        pool.remove_key("a")
        assert len(pool.keys) == 1
        assert pool.keys[0].label == "b"

    def test_remove_key_by_index(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a")
        pool.add_key("k2", label="b")
        pool.remove_key(0)
        assert len(pool.keys) == 1
        assert pool.keys[0].label == "b"

    def test_remove_key_invalid_index(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1")
        pool.remove_key(99)  # Should not raise
        assert len(pool.keys) == 1

    def test_get_key_round_robin(self):
        pool = CredentialPool(provider="p", _rotation_strategy="round_robin")
        pool.add_key("k1", label="a")
        pool.add_key("k2", label="b")
        assert pool.get_key().key == "k1"
        assert pool.get_key().key == "k2"
        assert pool.get_key().key == "k1"

    def test_get_key_priority(self):
        pool = CredentialPool(provider="p", _rotation_strategy="priority")
        pool.add_key("k1", label="a", priority=1)
        pool.add_key("k2", label="b", priority=0)
        # Priority strategy always returns lowest priority number
        for _ in range(3):
            assert pool.get_key().key == "k2"

    def test_get_key_model_filter(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a", models=["gpt-4o"])
        pool.add_key("k2", label="b", models=["claude-sonnet"])
        key = pool.get_key(model="claude-sonnet")
        assert key.label == "b"

    def test_get_key_no_model_match_falls_back(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a", models=["gpt-4o"])
        # No key has the model, but models=[], so all keys match
        key = pool.get_key(model="unknown-model")
        assert key is not None

    def test_get_key_no_available(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a")
        pool.mark_exhausted("a")
        assert pool.get_key() is None

    def test_get_key_rate_limited_is_available(self):
        """Rate-limited keys are considered available by design; get_key returns them."""
        pool = CredentialPool(provider="p")
        pk = pool.add_key("k1", label="a")
        pk.status = KeyStatus.RATE_LIMITED
        pk.last_used = time.time()  # fresh rate limit, still in cooldown
        key = pool.get_key()
        assert key is not None
        assert key.label == "a"
        # Status stays RATE_LIMITED — cooldown reset only triggers
        # when the available list is completely empty.
        assert pk.status == KeyStatus.RATE_LIMITED

    def test_mark_exhausted(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a")
        pool.mark_exhausted("a")
        assert pool.keys[0].status == KeyStatus.EXHAUSTED
        assert pool.keys[0].error_count == 1

    def test_mark_failed(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a")
        pool.mark_failed("a")
        assert pool.keys[0].status == KeyStatus.FAILED

    def test_mark_rate_limited(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a")
        pool.mark_rate_limited("a")
        assert pool.keys[0].status == KeyStatus.RATE_LIMITED
        assert pool.keys[0].last_used > 0

    def test_reset_key(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1", label="a")
        pool.mark_exhausted("a")
        pool.reset_key("a")
        assert pool.keys[0].status == KeyStatus.ACTIVE
        assert pool.keys[0].error_count == 0

    def test_reset_all(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1")
        pool.add_key("k2")
        pool.mark_exhausted("key-1")
        pool.mark_failed("key-2")
        pool.reset_all()
        for k in pool.keys:
            assert k.status == KeyStatus.ACTIVE
            assert k.error_count == 0

    def test_health_report(self):
        pool = CredentialPool(provider="deepseek")
        pool.add_key("k1", label="a", priority=0)
        pool.add_key("k2", label="b", priority=1)
        pool.mark_exhausted("b")
        report = pool.health_report()
        assert report["provider"] == "deepseek"
        assert report["total_keys"] == 2
        assert report["active"] == 1
        assert report["exhausted"] == 1
        assert report["health_pct"] == 50.0
        assert len(report["keys"]) == 2
        assert report["keys"][0]["label"] == "a"

    def test_health_report_empty_pool(self):
        pool = CredentialPool(provider="empty")
        report = pool.health_report()
        assert report["total_keys"] == 0
        assert report["health_pct"] == 0

    def test_get_key_increments_counters(self):
        pool = CredentialPool(provider="p")
        pool.add_key("k1")
        key = pool.get_key()
        assert key.total_calls == 1
        assert key.last_used > 0


# ── CredentialManager ──


class TestCredentialManager:
    def test_get_or_create_pool(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        pool = cm.get_or_create_pool("deepseek")
        assert pool.provider == "deepseek"
        assert "deepseek" in cm._pools
        # Second call returns same pool
        assert cm.get_or_create_pool("deepseek") is pool

    def test_add_key(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        pk = cm.add_key("deepseek", "sk-abc", label="main")
        assert pk.label == "main"
        assert len(cm._pools["deepseek"].keys) == 1

    def test_get_key_from_pool(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        cm.add_key("deepseek", "sk-abc", label="main")
        key = cm.get_key("deepseek")
        assert key == "sk-abc"

    def test_get_key_missing_provider_falls_back_to_env(self, monkeypatch):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        monkeypatch.setenv("MISSINGPROVIDER_API_KEY", "env-key-123")
        key = cm.get_key("missingprovider")
        assert key == "env-key-123"

    def test_get_key_missing_provider_no_env(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        key = cm.get_key("nonexistent")
        assert key is None

    def test_get_key_with_model(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        cm.add_key("openai", "sk-gpt", label="gpt", models=["gpt-4o"])
        cm.add_key("openai", "sk-claude", label="claude", models=["claude-sonnet"])
        key = cm.get_key("openai", model="claude-sonnet")
        assert key == "sk-claude"

    def test_handle_error_401(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        cm.add_key("p", "k", label="a")
        cm.handle_error("p", "a", 401)
        assert cm._pools["p"].keys[0].status == KeyStatus.FAILED

    def test_handle_error_429(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        cm.add_key("p", "k", label="a")
        cm.handle_error("p", "a", 429)
        assert cm._pools["p"].keys[0].status == KeyStatus.RATE_LIMITED

    def test_handle_error_unknown_pool(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        cm.handle_error("nonexistent", "label", 500)  # Should not raise

    def test_health_report(self):
        cm = CredentialManager(storage_path="/tmp/test_cred_mgr.json")
        cm.add_key("deepseek", "k1")
        cm.add_key("openai", "k2")
        report = cm.health_report()
        assert report["total_providers"] == 2
        assert "deepseek" in report["pools"]
        assert "openai" in report["pools"]

    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "creds.json")
        cm = CredentialManager(storage_path=path)
        cm.add_key("deepseek", "k1", label="primary", priority=0)
        cm.add_key("openai", "k2", label="main", priority=1)
        cm._pools["deepseek"].mark_exhausted("primary")
        cm.save()

        # Verify the file exists and does NOT contain raw keys
        with open(path) as f:
            data = json.load(f)
        assert "deepseek" in data
        assert "openai" in data
        for key_data in data["deepseek"]["keys"]:
            assert "key" not in key_data  # Keys are never persisted

        # Load into a new manager
        cm2 = CredentialManager(storage_path=path)
        cm2.add_key("deepseek", "k1", label="primary", priority=0)
        cm2.add_key("openai", "k2", label="main", priority=1)
        cm2.load()
        # Status should be restored
        deepseek_keys = cm2._pools["deepseek"].keys
        assert deepseek_keys[0].status == KeyStatus.EXHAUSTED

    def test_save_creates_directory(self, tmp_path):
        path = str(tmp_path / "subdir" / "creds.json")
        cm = CredentialManager(storage_path=path)
        cm.add_key("p", "k")
        cm.save()
        assert os.path.isfile(path)

    def test_load_no_file(self):
        cm = CredentialManager(storage_path="/tmp/nonexistent_creds.json")
        cm.load()  # Should not raise

    def test_default_storage_path(self):
        cm = CredentialManager()
        assert "credentials.json" in cm._storage_path

    def test_storage_path_with_directory(self, tmp_path):
        cm = CredentialManager(config_dir=str(tmp_path))
        assert cm._storage_path == os.path.join(str(tmp_path), "credentials.json")


# ── CredentialStore (backward-compat) ──


class TestCredentialStore:
    def test_set_and_get(self):
        cs = CredentialStore(config_dir="/tmp/test_cs_setget")
        cs.set("deepseek", "api_key", "sk-test1234567890")
        val = cs.get("deepseek", "api_key")
        assert val == "sk-test1234567890"

    def test_get_default(self):
        cs = CredentialStore(config_dir="/tmp/test_cs_default")
        val = cs.get("nonexistent", "key", "fallback")
        assert val == "fallback"

    def test_list_services(self):
        cs = CredentialStore(config_dir="/tmp/test_cs_svcs")
        cs.set("s1", "k", "v")
        cs.set("s2", "k", "v")
        assert "s1" in cs.list_services()
        assert "s2" in cs.list_services()

    def test_list_keys(self):
        cs = CredentialStore(config_dir="/tmp/test_cs_keys")
        cs.set("svc", "k1", "v1")
        cs.set("svc", "k2", "v2")
        keys = cs.list_keys("svc")
        assert "k1" in keys
        assert "k2" in keys

    def test_delete(self):
        cs = CredentialStore(config_dir="/tmp/test_cs_delete")
        cs.set("svc", "k", "v")
        assert cs.delete("svc", "k") is True
        assert cs.get("svc", "k") == ""

    def test_delete_nonexistent(self):
        cs = CredentialStore(config_dir="/tmp/test_cs_del_nx")
        assert cs.delete("svc", "nx") is False

    def test_persistence(self):
        cs = CredentialStore(config_dir="/tmp/test_cs_persist")
        cs.set("s", "k", "persisted-value")
        cs2 = CredentialStore(config_dir="/tmp/test_cs_persist")
        assert cs2.get("s", "k") == "persisted-value"

    def test_nonexistent_file(self):
        cs = CredentialStore(storage_path="/tmp/test_cs_nonexistent_file.json")
        assert cs.list_services() == []


# ── Utility functions ──


class TestMaskSecret:
    def test_normal_secret(self):
        result = mask_secret("sk-test1234567890")
        assert "****" in result
        assert result.endswith("7890")

    def test_short_secret(self):
        result = mask_secret("abc")
        assert result == "***"

    def test_empty_secret(self):
        assert mask_secret("") == ""

    def test_show_custom_chars(self):
        result = mask_secret("abcdefghij", show=2)
        assert result.endswith("ij")


class TestSanitizeOutput:
    def test_removes_sk_key(self):
        text = "My key is sk-test12345678901234567890 in the text"
        cleaned = sanitize_output(text)
        assert "sk-test" not in cleaned
        assert "My key is" in cleaned

    def test_removes_sk_or_key(self):
        text = "Key: sk-or-something12345678901234567"
        cleaned = sanitize_output(text)
        assert "sk-or-" not in cleaned

    def test_removes_colon_token(self):
        text = "Call with token: abcdef1234567890abcdef1234567890:secret1234567890secret1234567890"
        cleaned = sanitize_output(text)
        assert "secret1234567890" not in cleaned

    def test_preserves_normal_text(self):
        text = "Hello, this is a normal message with no secrets."
        assert sanitize_output(text) == text


class TestGetCredentialManager:
    def test_singleton(self):
        a = get_credential_manager()
        b = get_credential_manager()
        assert a is b

    def test_alias(self):
        mgr = get_credential_store()
        assert isinstance(mgr, CredentialManager)
