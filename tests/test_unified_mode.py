"""
Tests: Worldwave CLI v0.4 — Single 'ww' command

Verifies:
- Bare 'ww' enters interactive mode
- 'ww <task>' runs a one-shot task
- Subcommands (config, status, etc.) route correctly
- No 'run'/'chat' subcommands exist
"""

import sys; sys.path.insert(0, ".")
import os
import json
import tempfile
import pytest
from unittest.mock import patch, MagicMock


# ══════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════

@pytest.fixture
def mock_env():
    """Set up mock WW environment variables."""
    old_home = os.environ.get("WW_HOME", "")
    os.environ["WW_HOME"] = os.path.expanduser("~/worldwave")
    yield
    if old_home:
        os.environ["WW_HOME"] = old_home
    else:
        del os.environ["WW_HOME"]


@pytest.fixture
def cli_module(mock_env):
    """Import the ww_cli module (must be after env setup)."""
    import importlib
    if "ww_cli" in sys.modules:
        del sys.modules["ww_cli"]
    import ww_cli
    return ww_cli


# ══════════════════════════════════════════════
# Tests: Configuration & initialization
# ══════════════════════════════════════════════

class TestCLIConfig:
    def test_config_functions(self, cli_module):
        """Config handling functions should exist."""
        assert hasattr(cli_module, "cmd_config")
        assert hasattr(cli_module, "cmd_status")
        assert hasattr(cli_module, "cmd_help")
        print("✅ CLI: config functions exist")

    def test_colors(self, cli_module):
        """Color utilities should work."""
        red = cli_module.Colors.red("test")
        assert "test" in red
        green = cli_module.Colors.green("ok")
        assert "ok" in green
        print("✅ CLI: color utilities OK")


# ══════════════════════════════════════════════
# Tests: Single 'ww' command (no run/chat subcommands)
# ══════════════════════════════════════════════

class TestSingleCommand:
    def test_cmd_run_exists(self, cli_module):
        """cmd_run should exist as the unified entry point."""
        assert hasattr(cli_module, "cmd_run")
        assert callable(cli_module.cmd_run)
        print("✅ CLI: cmd_run exists (unified entry point)")

    def test_no_run_chat_subcommands(self, cli_module):
        """COMMANDS should NOT include 'run' or 'chat'."""
        assert "run" not in cli_module.COMMANDS
        assert "chat" not in cli_module.COMMANDS
        print("✅ CLI: no run/chat subcommands")

    def test_cmd_run_calls_auto_start_server(self, cli_module):
        """cmd_run should call auto_start_server() first."""
        with patch.object(cli_module, "auto_start_server", return_value=False) as mock_start:
            with patch.object(cli_module, "api_post", return_value={}) as mock_api:
                args = type("Args", (), {"goal": ["test"], "spirals": None})()
                cli_module.cmd_run(args)
                mock_start.assert_called_once()
                mock_api.assert_not_called()
        print("✅ CLI: calls auto_start_server (and stops on failure)")

    def test_cmd_run_calls_api_on_success(self, cli_module):
        """cmd_run should call API when server starts successfully."""
        with patch.object(cli_module, "auto_start_server", return_value=True) as mock_start:
            with patch.object(cli_module, "api_post", return_value={
                "status": "completed", "spirals_completed": 3, "results": []
            }) as mock_api:
                args = type("Args", (), {"goal": ["do", "something"], "spirals": None})()
                cli_module.cmd_run(args)
                mock_start.assert_called_once()
                mock_api.assert_called_once_with("/ww/run", {"goal": "do something", "max_spirals": 5})
        print("✅ CLI: calls API with correct endpoint")

    def test_cmd_run_default_spirals(self, cli_module):
        """Default spirals should be 5."""
        with patch.object(cli_module, "auto_start_server", return_value=True):
            with patch.object(cli_module, "api_post", return_value={
                "status": "completed", "spirals_completed": 5, "results": []
            }) as mock_api:
                args = type("Args", (), {"goal": ["task"], "spirals": None})()
                cli_module.cmd_run(args)
                mock_api.assert_called_once_with("/ww/run", {"goal": "task", "max_spirals": 5})
        print("✅ CLI: default spirals=5")

    def test_cmd_run_custom_spirals(self, cli_module):
        """Custom spirals parameter should pass through."""
        with patch.object(cli_module, "auto_start_server", return_value=True):
            with patch.object(cli_module, "api_post", return_value={
                "status": "completed", "spirals_completed": 10, "results": []
            }) as mock_api:
                args = type("Args", (), {"goal": ["task"], "spirals": 10})()
                cli_module.cmd_run(args)
                mock_api.assert_called_once_with("/ww/run", {"goal": "task", "max_spirals": 10})
        print("✅ CLI: custom spirals=10")

    def test_cmd_run_empty_goal(self, cli_module):
        """Empty goal enters interactive mode (starts server)."""
        with patch.object(cli_module, "auto_start_server", return_value=True) as mock_start:
            with patch("builtins.input", side_effect=EOFError()):
                args = type("Args", (), {"goal": [], "spirals": None})()
                cli_module.cmd_run(args)
                mock_start.assert_called_once()
        print("✅ CLI: empty goal enters interactive mode")

    def test_cmd_run_api_failure(self, cli_module):
        """API failure should not crash the CLI."""
        with patch.object(cli_module, "auto_start_server", return_value=True):
            with patch.object(cli_module, "api_post", return_value=None) as mock_api:
                args = type("Args", (), {"goal": ["task"], "spirals": None})()
                cli_module.cmd_run(args)
        print("✅ CLI: handles API failure gracefully")

    def test_auto_start_server_exists(self, cli_module):
        """auto_start_server() helper exists."""
        assert hasattr(cli_module, "auto_start_server")
        assert callable(cli_module.auto_start_server)
        print("✅ CLI: auto_start_server exists")


# ══════════════════════════════════════════════
# Tests: Subcommand routing
# ══════════════════════════════════════════════

class TestSubcommandRouting:
    def test_admin_subcommands_exist(self, cli_module):
        """Admin subcommands should be in COMMANDS."""
        for cmd in ("config", "status", "server", "tools", "init"):
            assert cmd in cli_module.COMMANDS, f"missing subcommand: {cmd}"
        print(f"✅ CLI: {len(cli_module.COMMANDS)} admin subcommands")

    def test_help_does_not_crash(self, cli_module):
        """Help should render without errors."""
        parser = cli_module.build_parser()
        help_text = parser.format_help()
        assert "usage:" in help_text.lower()
        print(f"✅ CLI: help renders ({len(help_text)} chars)")


# ══════════════════════════════════════════════
# Tests: Version mismatch notification
# ══════════════════════════════════════════════

class TestVersionNotification:
    def test_update_notifier_exists(self, cli_module):
        """Update check function should exist."""
        assert hasattr(cli_module, "_notify_if_update") or hasattr(cli_module, "check_for_update")
        print("✅ CLI: update notification exists")


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
