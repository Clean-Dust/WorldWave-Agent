"""Tests for WW local HTTP API key persistence (not LLM keys).

Server and CLI must share ~/.ww/api_key so update/restart does not 401.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from core.ww_api_key import api_key_path, resolve_ww_api_key


@pytest.fixture
def key_config(tmp_path, monkeypatch):
    """Isolated WW_CONFIG; clear WW_API_KEY so tests control resolution."""
    config_dir = tmp_path / "ww_config"
    config_dir.mkdir()
    monkeypatch.setenv("WW_CONFIG", str(config_dir))
    monkeypatch.delenv("WW_API_KEY", raising=False)
    return config_dir


class TestResolveWwApiKey:
    def test_env_wins_over_file(self, key_config, monkeypatch):
        key_file = key_config / "api_key"
        key_file.write_text("file-loses")
        monkeypatch.setenv("WW_API_KEY", "env-wins-key-1234567890")

        key = resolve_ww_api_key(str(key_config))

        assert key == "env-wins-key-1234567890"
        assert key_file.read_text().strip() == "env-wins-key-1234567890"
        assert os.environ["WW_API_KEY"] == "env-wins-key-1234567890"
        assert (key_file.stat().st_mode & 0o777) == 0o600

    def test_file_used_when_env_missing(self, key_config, monkeypatch):
        key_file = key_config / "api_key"
        key_file.write_text("stable-file-key-abc")
        monkeypatch.delenv("WW_API_KEY", raising=False)

        key = resolve_ww_api_key(str(key_config))

        assert key == "stable-file-key-abc"
        assert os.environ["WW_API_KEY"] == "stable-file-key-abc"

    def test_second_load_same_value(self, key_config, monkeypatch):
        """Missing env → generate once; second resolve returns same key (no rotate)."""
        monkeypatch.delenv("WW_API_KEY", raising=False)
        key_file = key_config / "api_key"
        assert not key_file.exists()

        first = resolve_ww_api_key(str(key_config))
        # Clear env to force file path (simulates new process without .env key)
        monkeypatch.delenv("WW_API_KEY", raising=False)
        second = resolve_ww_api_key(str(key_config))

        assert first
        assert len(first) >= 16
        assert first == second
        assert key_file.read_text().strip() == first
        assert (key_file.stat().st_mode & 0o777) == 0o600

    def test_empty_env_falls_through_to_file(self, key_config, monkeypatch):
        key_file = key_config / "api_key"
        key_file.write_text("from-file")
        monkeypatch.setenv("WW_API_KEY", "   ")

        key = resolve_ww_api_key(str(key_config))

        assert key == "from-file"

    def test_generates_when_missing(self, key_config, monkeypatch):
        monkeypatch.delenv("WW_API_KEY", raising=False)
        key_file = key_config / "api_key"
        assert not key_file.exists()

        key = resolve_ww_api_key(str(key_config))

        assert key
        assert key_file.read_text().strip() == key
        assert os.environ["WW_API_KEY"] == key
        assert (key_file.stat().st_mode & 0o777) == 0o600

    def test_api_key_path_respects_config_dir(self, key_config):
        assert api_key_path(str(key_config)) == str(key_config / "api_key")
