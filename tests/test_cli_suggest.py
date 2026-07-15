"""CLI: typo suggestions for unknown short commands (not LLM goals)."""

from __future__ import annotations

import pytest


@pytest.fixture
def cli():
    import ww_cli

    return ww_cli


# ── suggest_cli_commands ──────────────────────────────────────────────


def test_suggest_updat_includes_update(cli):
    suggestions = cli.suggest_cli_commands("updat")
    assert any(s.endswith("update") for s in suggestions)
    assert "ww update" in suggestions


def test_suggest_gataway_includes_gateway(cli):
    suggestions = cli.suggest_cli_commands("gataway")
    assert "ww gateway" in suggestions


def test_suggest_gataway_setup_phrase(cli):
    suggestions = cli.suggest_cli_commands("gataway", ["setup"])
    assert "ww gateway setup" in suggestions
    assert "ww gateway" in suggestions


def test_suggest_hepl_includes_help(cli):
    suggestions = cli.suggest_cli_commands("hepl")
    assert "ww help" in suggestions


def test_suggest_returns_ww_prefixed_strings(cli):
    for s in cli.suggest_cli_commands("updat"):
        assert s.startswith("ww ")


# ── is_likely_command_typo ────────────────────────────────────────────


def test_typo_single_token(cli):
    assert cli.is_likely_command_typo("updat") is True
    assert cli.is_likely_command_typo("gataway") is True
    assert cli.is_likely_command_typo("hepl") is True


def test_typo_with_one_rest_token(cli):
    assert cli.is_likely_command_typo("gataway", ["setup"]) is True


def test_natural_language_not_typo(cli):
    """Multi-word free text with no close command match → LLM path."""
    # "write" is not close to any COMMANDS key at cutoff 0.55
    assert cli.is_likely_command_typo("write", ["a", "hello", "world", "script"]) is False


def test_long_rest_even_with_help_match_goes_llm(cli):
    """`ww hepl me write a long essay` — rest > 2 → not typo path."""
    assert (
        cli.is_likely_command_typo("hepl", ["me", "write", "a", "long", "essay"])
        is False
    )


def test_empty_or_long_token_not_typo(cli):
    assert cli.is_likely_command_typo("") is False
    assert cli.is_likely_command_typo("x" * 25) is False
    assert cli.is_likely_command_typo("has space") is False


def test_unrelated_token_no_match(cli):
    """Random gibberish far from vocabulary should not suggest."""
    assert cli.is_likely_command_typo("zzzzzzzz") is False
    assert cli.suggest_cli_commands("zzzzzzzz") == []


# ── Integration: print + main routing ─────────────────────────────────


def test_print_command_suggestions(cli, capsys):
    cli.print_command_suggestions("updat")
    out = capsys.readouterr().out
    assert "Unknown command: updat" in out
    assert "Did you mean:" in out
    assert "ww update" in out
    assert "ww help" in out
    assert "write a script" in out


def test_main_typo_exits_without_cmd_run(cli, capsys, monkeypatch):
    """Unrecognized close match must exit 1 and never call cmd_run."""
    called = {"run": False}

    def _fake_run(args):
        called["run"] = True

    monkeypatch.setattr(cli, "cmd_run", _fake_run)
    monkeypatch.setattr(
        "sys.argv", ["ww", "updat"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert called["run"] is False
    out = capsys.readouterr().out
    assert "Unknown command: updat" in out
    assert "ww update" in out


def test_main_natural_task_calls_cmd_run(cli, monkeypatch):
    """Natural multi-word goal still routes to cmd_run."""
    seen = {}

    def _fake_run(args):
        seen["goal"] = list(args.goal)

    monkeypatch.setattr(cli, "cmd_run", _fake_run)
    monkeypatch.setattr(
        "sys.argv",
        ["ww", "write", "a", "hello", "world", "script"],
    )
    cli.main()
    assert seen["goal"] == ["write", "a", "hello", "world", "script"]


def test_gateway_unknown_subaction_suggests(cli, capsys):
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        cli, "load_or_create_api_key", return_value="k"
    ):
        cli.cmd_gateway(cli.ArgsObj(action="strat", platform=None))
    out = capsys.readouterr().out
    assert "Unknown gateway action: strat" in out
    assert "Did you mean:" in out
    assert "start" in out


def test_help_mentions_typo_suggestions(cli, capsys):
    cli.cmd_help(cli.ArgsObj())
    out = capsys.readouterr().out
    assert "Did you mean" in out or "Typos:" in out
