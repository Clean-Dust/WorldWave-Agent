"""Unit tests for CLI API key loading priority (env over ~/.ww/api_key)."""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")


@pytest.fixture
def cli_with_temp_config(tmp_path, monkeypatch):
    """Import ww_cli with WW_CONFIG pointed at a temp dir (no network)."""
    config_dir = tmp_path / "ww_config"
    config_dir.mkdir()
    monkeypatch.setenv("WW_CONFIG", str(config_dir))
    # Clear any pre-existing WW_API_KEY so tests control it
    monkeypatch.delenv("WW_API_KEY", raising=False)
    if "ww_cli" in sys.modules:
        del sys.modules["ww_cli"]
    import ww_cli

    # Module-level WW_CONFIG is set at import; force to temp
    ww_cli.WW_CONFIG = str(config_dir)
    return ww_cli, config_dir


class TestLoadOrCreateApiKey:
    def test_env_wins_over_mismatched_file(self, cli_with_temp_config, monkeypatch):
        """WW_API_KEY from env must beat a different key in the key file."""
        ww_cli, config_dir = cli_with_temp_config
        key_file = config_dir / "api_key"
        key_file.write_text("file-loses-wrong-key")
        monkeypatch.setenv("WW_API_KEY", "env-wins-key-1234567890")

        key = ww_cli.load_or_create_api_key()

        assert key == "env-wins-key-1234567890"
        assert key_file.read_text().strip() == "env-wins-key-1234567890"
        assert os.environ["WW_API_KEY"] == "env-wins-key-1234567890"
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_env_strips_whitespace(self, cli_with_temp_config, monkeypatch):
        ww_cli, config_dir = cli_with_temp_config
        monkeypatch.setenv("WW_API_KEY", "  env-key-with-spaces  ")

        key = ww_cli.load_or_create_api_key()

        assert key == "env-key-with-spaces"
        assert (config_dir / "api_key").read_text().strip() == "env-key-with-spaces"

    def test_file_used_when_env_missing(self, cli_with_temp_config, monkeypatch):
        ww_cli, config_dir = cli_with_temp_config
        key_file = config_dir / "api_key"
        key_file.write_text("file-only-key-abc")
        monkeypatch.delenv("WW_API_KEY", raising=False)

        key = ww_cli.load_or_create_api_key()

        assert key == "file-only-key-abc"
        assert os.environ["WW_API_KEY"] == "file-only-key-abc"

    def test_empty_env_falls_through_to_file(self, cli_with_temp_config, monkeypatch):
        ww_cli, config_dir = cli_with_temp_config
        key_file = config_dir / "api_key"
        key_file.write_text("from-file-when-env-blank")
        monkeypatch.setenv("WW_API_KEY", "   ")

        key = ww_cli.load_or_create_api_key()

        assert key == "from-file-when-env-blank"

    def test_generates_when_missing(self, cli_with_temp_config, monkeypatch):
        ww_cli, config_dir = cli_with_temp_config
        monkeypatch.delenv("WW_API_KEY", raising=False)
        key_file = config_dir / "api_key"
        assert not key_file.exists()

        key = ww_cli.load_or_create_api_key()

        assert key
        assert len(key) >= 16
        assert key_file.read_text().strip() == key
        assert os.environ["WW_API_KEY"] == key
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_env_matching_file_unchanged(self, cli_with_temp_config, monkeypatch):
        ww_cli, config_dir = cli_with_temp_config
        key_file = config_dir / "api_key"
        key_file.write_text("same-key-everywhere")
        monkeypatch.setenv("WW_API_KEY", "same-key-everywhere")
        mtime_before = key_file.stat().st_mtime_ns

        key = ww_cli.load_or_create_api_key()

        assert key == "same-key-everywhere"
        # No need to rewrite when already in sync (mtime may still match)
        assert key_file.read_text().strip() == "same-key-everywhere"
        assert key_file.stat().st_mtime_ns == mtime_before
