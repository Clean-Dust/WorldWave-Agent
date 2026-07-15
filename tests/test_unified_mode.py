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
import pytest
from unittest.mock import patch


# ══════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════

@pytest.fixture
def mock_env():
    """Set up mock WW environment variables."""
    old_home = os.environ.get("WW_HOME", "")
    os.environ["WW_HOME"] = os.path.expanduser("~/worldwave")
    # Ensure a dummy API key is set so pre-flight check passes
    old_key = os.environ.get("DEEPSEEK_API_KEY", "")
    os.environ["DEEPSEEK_API_KEY"] = "test-key-ci"
    yield
    if old_home:
        os.environ["WW_HOME"] = old_home
    else:
        del os.environ["WW_HOME"]
    if old_key:
        os.environ["DEEPSEEK_API_KEY"] = old_key
    else:
        del os.environ["DEEPSEEK_API_KEY"]


@pytest.fixture
def cli_module(mock_env):
    """Import the ww_cli module (must be after env setup)."""
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

    def test_run_in_commands_chat_not(self, cli_module):
        """'run' should be in COMMANDS (primary entry point), 'chat' should not."""
        assert "run" in cli_module.COMMANDS, "'run' must be in COMMANDS — it is the primary user entry point"
        assert "chat" not in cli_module.COMMANDS
        print("✅ CLI: run in commands, chat not")

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

    def test_cmd_run_no_api_key_early_exit(self, cli_module):
        """cmd_run should exit early without API key, never calling auto_start_server."""
        with patch.object(cli_module, "check_llm_api_key", return_value=None):
            with patch.object(cli_module, "auto_start_server", return_value=True) as mock_start:
                with patch.object(cli_module, "api_post", return_value={}) as mock_api:
                    args = type("Args", (), {"goal": ["test"], "spirals": None})()
                    cli_module.cmd_run(args)
                    mock_start.assert_not_called()
                    mock_api.assert_not_called()
        print("✅ CLI: exits early when no LLM API key")

    def test_auto_start_server_exists(self, cli_module):
        """auto_start_server() helper exists."""
        assert hasattr(cli_module, "auto_start_server")
        assert callable(cli_module.auto_start_server)
        print("✅ CLI: auto_start_server exists")

    def test_check_llm_api_key_exists(self, cli_module):
        """check_llm_api_key() helper exists."""
        assert hasattr(cli_module, "check_llm_api_key")
        assert callable(cli_module.check_llm_api_key)
        print("✅ CLI: check_llm_api_key exists")


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
# Tests: Interactive chat update interception
# ══════════════════════════════════════════════

class TestChatUpdateIntercept:
    """REPL must not send 'ww update' / '/update' as LLM goals."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("/update", ""),
            ("update", ""),
            ("ww update", ""),
            ("WW Update", ""),
            ("  /UPDATE  ", ""),
            ("/update status", "status"),
            ("update status", "status"),
            ("ww update status", "status"),
            ("/update --dry-run", "--dry-run"),
            ("ww update --dry-run", "--dry-run"),
            ("update dry-run", "--dry-run"),
            # upgrade alias
            ("/upgrade", ""),
            ("upgrade", ""),
            ("ww upgrade", ""),
            ("WW Upgrade", ""),
            ("upgrade status", "status"),
            ("ww upgrade --dry-run", "--dry-run"),
            # exit must never parse as update
            ("/exit", None),
            ("exit", None),
            ("/EXIT", None),
            ("quit", None),
            ("q", None),
            ("update my blog", None),
            ("please ww update something", None),
            ("hello", None),
            ("", None),
        ],
    )
    def test_parse_chat_update_command(self, cli_module, line, expected):
        assert cli_module.parse_chat_update_command(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("/exit", True),
            ("/quit", True),
            ("/q", True),
            ("exit", True),
            ("quit", True),
            ("q", True),
            ("/EXIT", True),
            ("EXIT", True),
            ("  exit  ", True),
            ("exit\r", True),
            ("／exit", True),  # fullwidth solidus
            ("／QUIT", True),
            ("/update", False),
            ("upgrade", False),
            ("ww update", False),
            ("hello", False),
            ("", False),
            ("exit now", False),
        ],
    )
    def test_is_chat_exit_command(self, cli_module, line, expected):
        assert cli_module.is_chat_exit_command(line) is expected

    def test_repl_update_does_not_call_api(self, cli_module):
        """Typing 'ww update' in chat must run handle_chat_update, not api_post."""
        inputs = iter(["ww update", "/exit"])

        with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
            with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                with patch.object(cli_module, "auto_start_server", return_value=True):
                    with patch.object(cli_module, "api_post") as mock_api:
                        with patch.object(cli_module, "handle_chat_update") as mock_upd:
                            with patch("builtins.input", side_effect=lambda *_a, **_k: next(inputs)):
                                args = type("Args", (), {"goal": [], "spirals": None})()
                                cli_module.cmd_run(args)

        mock_upd.assert_called_once_with("")
        mock_api.assert_not_called()
        print("✅ CLI: ww update in REPL does not call api_post")

    def test_repl_upgrade_does_not_call_api(self, cli_module):
        """Typing 'ww upgrade' in chat must run handle_chat_update, not api_post."""
        inputs = iter(["ww upgrade", "exit"])

        with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
            with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                with patch.object(cli_module, "auto_start_server", return_value=True):
                    with patch.object(cli_module, "api_post") as mock_api:
                        with patch.object(cli_module, "handle_chat_update") as mock_upd:
                            with patch("builtins.input", side_effect=lambda *_a, **_k: next(inputs)):
                                args = type("Args", (), {"goal": [], "spirals": None})()
                                cli_module.cmd_run(args)

        mock_upd.assert_called_once_with("")
        mock_api.assert_not_called()
        print("✅ CLI: ww upgrade in REPL does not call api_post")

    def test_repl_exit_variants_do_not_call_api(self, cli_module, capsys):
        """Bare exit /EXIT /q must leave REPL with Bye. — never api_post."""
        for exit_line in ("exit", "/EXIT", "q", "／exit"):
            inputs = iter([exit_line])
            with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
                with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                    with patch.object(cli_module, "auto_start_server", return_value=True):
                        with patch.object(cli_module, "api_post") as mock_api:
                            with patch.object(cli_module, "handle_chat_update") as mock_upd:
                                with patch(
                                    "builtins.input",
                                    side_effect=lambda *_a, **_k: next(inputs),
                                ):
                                    args = type("Args", (), {"goal": [], "spirals": None})()
                                    cli_module.cmd_run(args)
            mock_api.assert_not_called()
            mock_upd.assert_not_called()
            out = capsys.readouterr().out
            assert "Bye." in out
        print("✅ CLI: exit variants leave REPL without api_post")

    def test_repl_slash_update_status(self, cli_module):
        inputs = iter(["/update status", "/exit"])

        with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
            with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                with patch.object(cli_module, "auto_start_server", return_value=True):
                    with patch.object(cli_module, "api_post") as mock_api:
                        with patch.object(cli_module, "handle_chat_update") as mock_upd:
                            with patch("builtins.input", side_effect=lambda *_a, **_k: next(inputs)):
                                args = type("Args", (), {"goal": [], "spirals": None})()
                                cli_module.cmd_run(args)

        mock_upd.assert_called_once_with("status")
        mock_api.assert_not_called()
        print("✅ CLI: /update status intercepted")

    def test_help_lists_update(self, cli_module, capsys):
        inputs = iter(["/help", "/exit"])

        with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
            with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                with patch.object(cli_module, "auto_start_server", return_value=True):
                    with patch.object(cli_module, "api_post") as mock_api:
                        with patch("builtins.input", side_effect=lambda *_a, **_k: next(inputs)):
                            args = type("Args", (), {"goal": [], "spirals": None})()
                            cli_module.cmd_run(args)

        out = capsys.readouterr().out
        assert "/update" in out
        assert "/exit" in out
        mock_api.assert_not_called()
        print("✅ CLI: /help lists /update")

    def test_update_notification_mentions_chat_path(self, cli_module):
        """Update-available copy must mention /update for chat users."""
        import inspect
        from core import updater

        src = inspect.getsource(updater.check_for_update)
        assert "/update" in src
        assert "chat" in src
        assert "shell" in src
        print("✅ CLI: update notification mentions /update (chat)")


# ══════════════════════════════════════════════
# Tests: Interactive chat gateway interception
# ══════════════════════════════════════════════

class TestChatGatewayIntercept:
    """REPL must not send '/ww gateway setup' / '/gateway' as LLM goals."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("/gateway", ("", None)),
            ("gateway", ("", None)),
            ("ww gateway", ("", None)),
            ("/ww gateway", ("", None)),
            ("WW Gateway", ("", None)),
            ("  /gateway  ", ("", None)),
            ("/gateway setup", ("setup", None)),
            ("gateway setup", ("setup", None)),
            ("ww gateway setup", ("setup", None)),
            ("/ww gateway setup", ("setup", None)),  # exact screenshot form
            ("/WW gateway SETUP", ("setup", None)),
            ("gateway list", ("list", None)),
            ("/gateway list", ("list", None)),
            ("ww gateway list", ("list", None)),
            ("/ww gateway list", ("list", None)),
            ("gateway start", ("start", None)),
            ("gateway start telegram", ("start", "telegram")),
            ("/gateway stop", ("stop", None)),
            ("ww gateway stop telegram", ("stop", "telegram")),
            ("／gateway setup", ("setup", None)),  # fullwidth solidus
            # must not steal update / exit / normal goals
            ("/update", None),
            ("/exit", None),
            ("exit", None),
            ("gateway my bot", None),
            ("please gateway setup something", None),
            ("hello", None),
            ("", None),
        ],
    )
    def test_parse_chat_gateway_command(self, cli_module, line, expected):
        assert cli_module.parse_chat_gateway_command(line) == expected

    def test_repl_ww_gateway_setup_does_not_call_api(self, cli_module):
        """Typing '/ww gateway setup' in chat must run cmd_gateway, not api_post."""
        inputs = iter(["/ww gateway setup", "/exit"])

        with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
            with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                with patch.object(cli_module, "auto_start_server", return_value=True):
                    with patch.object(cli_module, "api_post") as mock_api:
                        with patch.object(cli_module, "cmd_gateway") as mock_gw:
                            with patch(
                                "builtins.input",
                                side_effect=lambda *_a, **_k: next(inputs),
                            ):
                                args = type("Args", (), {"goal": [], "spirals": None})()
                                cli_module.cmd_run(args)

        mock_gw.assert_called_once()
        call_args = mock_gw.call_args[0][0]
        assert call_args.action == "setup"
        mock_api.assert_not_called()
        print("✅ CLI: /ww gateway setup in REPL does not call api_post")

    def test_repl_gateway_list_does_not_call_api(self, cli_module):
        inputs = iter(["gateway list", "/exit"])

        with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
            with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                with patch.object(cli_module, "auto_start_server", return_value=True):
                    with patch.object(cli_module, "api_post") as mock_api:
                        with patch.object(cli_module, "cmd_gateway") as mock_gw:
                            with patch(
                                "builtins.input",
                                side_effect=lambda *_a, **_k: next(inputs),
                            ):
                                args = type("Args", (), {"goal": [], "spirals": None})()
                                cli_module.cmd_run(args)

        mock_gw.assert_called_once()
        assert mock_gw.call_args[0][0].action == "list"
        mock_api.assert_not_called()
        print("✅ CLI: gateway list in REPL does not call api_post")

    def test_help_lists_gateway(self, cli_module, capsys):
        inputs = iter(["/help", "/exit"])

        with patch.object(cli_module, "check_llm_api_key", return_value="deepseek"):
            with patch.object(cli_module, "load_or_create_api_key", return_value="k"):
                with patch.object(cli_module, "auto_start_server", return_value=True):
                    with patch.object(cli_module, "api_post") as mock_api:
                        with patch(
                            "builtins.input",
                            side_effect=lambda *_a, **_k: next(inputs),
                        ):
                            args = type("Args", (), {"goal": [], "spirals": None})()
                            cli_module.cmd_run(args)

        out = capsys.readouterr().out
        assert "/gateway" in out
        assert "/gateway setup" in out
        mock_api.assert_not_called()
        print("✅ CLI: /help lists /gateway")


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
