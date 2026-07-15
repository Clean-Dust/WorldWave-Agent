"""CLI: gateway default action, empty list UX, ww help (not LLM goal)."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def cli():
    import ww_cli

    return ww_cli


def _route_gateway_action(cli_module, argv_tail):
    """Parse argv like main() and return the action that would be passed to cmd_gateway."""
    parser = cli_module.build_parser()
    args, extra = parser.parse_known_args(argv_tail)
    assert args.command == "gateway"
    pos = list(getattr(args, "goal", []) or []) + list(extra or [])
    return pos[0] if pos else None


def test_bare_gateway_action_is_none(cli):
    """Bare `ww gateway` must not default to list (setup/status path)."""
    assert _route_gateway_action(cli, ["gateway"]) is None


def test_gateway_setup_action(cli):
    assert _route_gateway_action(cli, ["gateway", "setup"]) == "setup"


def test_gateway_list_action(cli):
    assert _route_gateway_action(cli, ["gateway", "list"]) == "list"


def test_gateway_start_with_platform(cli):
    parser = cli.build_parser()
    args, extra = parser.parse_known_args(["gateway", "start", "telegram"])
    pos = list(args.goal or []) + list(extra or [])
    assert pos[0] == "start"
    assert pos[1] == "telegram"


def test_empty_gateway_list_message(cli, capsys):
    """Empty gateways dict must print setup hint, not a blank Gateway: header."""
    with patch.object(cli, "load_or_create_api_key", return_value="k"):
        with patch.object(cli, "auto_start_server", return_value=True):
            with patch.object(
                cli, "api_get", return_value={"gateways": {}}
            ):
                cli.cmd_gateway(cli.ArgsObj(action="list", platform=None))

    out = capsys.readouterr().out
    assert "no gateway configured" in out
    assert "gateway setup" in out
    # Must not leave users with a lone empty header
    assert "Gateway:" not in out or "no gateway configured" in out


def test_empty_gateway_list_when_api_none(cli, capsys):
    with patch.object(cli, "load_or_create_api_key", return_value="k"):
        with patch.object(cli, "auto_start_server", return_value=True):
            with patch.object(cli, "api_get", return_value=None):
                cli.cmd_gateway(cli.ArgsObj(action="list", platform=None))

    out = capsys.readouterr().out
    assert "no gateway configured" in out
    assert "gateway setup" in out


def test_setup_non_tty_skips_input(cli, capsys):
    """Non-interactive stdin must not call input()."""
    with patch.object(cli, "load_or_create_api_key", return_value="k"):
        with patch.object(cli, "auto_start_server", return_value=True):
            with patch.object(cli, "api_get", return_value={"gateways": {}}):
                with patch.object(sys.stdin, "isatty", return_value=False):
                    with patch("builtins.input") as mock_input:
                        cli.cmd_gateway(cli.ArgsObj(action="setup", platform=None))

    mock_input.assert_not_called()
    out = capsys.readouterr().out
    assert "interactive terminal" in out.lower() or "real TTY" in out or "tty" in out.lower()
    assert "ww gateway setup" in out


def test_help_command_not_run_goal(cli, capsys):
    """`ww help` must print CLI help and not route to cmd_run."""
    with patch.object(cli, "cmd_run") as mock_run:
        with patch.object(cli, "cmd_help", wraps=cli.cmd_help) as mock_help:
            # Simulate main routing for command=help
            parser = cli.build_parser()
            args, extra = parser.parse_known_args(["help"])
            assert args.command == "help"
            # Same branch as main()
            cli.cmd_help(args)
            mock_run.assert_not_called()

    out = capsys.readouterr().out
    assert "ww <command>" in out or "gateway setup" in out
    assert "bash" in out.lower() or "ww help" in out or "--help" in out


def test_help_text_mentions_gateway_setup_and_bash(cli, capsys):
    cli.cmd_help(SimpleNamespace())
    out = capsys.readouterr().out
    assert "gateway setup" in out
    assert "ww help" in out or "--help" in out
    assert "bash" in out.lower()


def test_main_help_flag(cli, capsys):
    with patch.object(sys, "argv", ["ww", "--help"]):
        cli.main()
    out = capsys.readouterr().out
    assert "gateway setup" in out


def test_main_ww_help_not_llm(cli, capsys):
    with patch.object(sys, "argv", ["ww", "help"]):
        with patch.object(cli, "cmd_run") as mock_run:
            cli.main()
    mock_run.assert_not_called()
    out = capsys.readouterr().out
    assert "gateway" in out.lower()


def test_main_gateway_setup_routes_action(cli):
    """main() must pass action=setup (goal nargs consumed correctly)."""
    mock_gw = MagicMock()
    with patch.object(sys, "argv", ["ww", "gateway", "setup"]):
        with patch.dict(cli.COMMANDS, {"gateway": mock_gw}):
            cli.main()
    mock_gw.assert_called_once()
    assert mock_gw.call_args[0][0].action == "setup"


def test_main_bare_gateway_routes_none(cli):
    mock_gw = MagicMock()
    with patch.object(sys, "argv", ["ww", "gateway"]):
        with patch.dict(cli.COMMANDS, {"gateway": mock_gw}):
            cli.main()
    mock_gw.assert_called_once()
    assert mock_gw.call_args[0][0].action is None
