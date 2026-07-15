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


# ── suggest_chat_commands (interactive REPL) ──────────────────────────


def test_chat_suggest_updat_to_update(cli):
    suggestions = cli.suggest_chat_commands("/updat")
    assert suggestions is not None
    assert "/update" in suggestions
    for s in suggestions:
        assert s.startswith("/")


def test_chat_suggest_gataway_setup_phrase(cli):
    suggestions = cli.suggest_chat_commands("gataway setup")
    assert suggestions is not None
    assert "/gateway setup" in suggestions
    assert "/gateway" in suggestions


def test_chat_suggest_ww_shell_form(cli):
    suggestions = cli.suggest_chat_commands("ww updat")
    assert suggestions is not None
    assert "/update" in suggestions


def test_chat_suggest_fullwidth_slash(cli):
    suggestions = cli.suggest_chat_commands("／updat")
    assert suggestions is not None
    assert "/update" in suggestions


def test_chat_natural_language_returns_none(cli):
    """Natural free text without slash/close match → LLM path."""
    assert cli.suggest_chat_commands("你好") is None
    assert cli.suggest_chat_commands("write a hello world script") is None
    assert cli.suggest_chat_commands("please help me draft an email") is None


def test_chat_slash_unknown_skips_llm(cli):
    """Slash with no close match still skips LLM (empty suggestion list)."""
    sugs = cli.suggest_chat_commands("/zzzzzzzz")
    assert sugs is not None
    assert sugs == []


def test_chat_print_suggestions(cli, capsys):
    cli.print_chat_command_suggestions("/updat", ["/update"])
    out = capsys.readouterr().out
    assert "Unknown command: /updat" in out
    assert "Did you mean:" in out
    assert "/update" in out
    assert "/help" in out


def test_chat_repl_typo_does_not_call_api(cli, capsys, monkeypatch):
    """`/updat` in chat must print Did you mean and never call api_post."""
    inputs = iter(["/updat", "/exit"])

    with monkeypatch.context() as m:
        m.setattr(cli, "check_llm_api_key", lambda: "deepseek")
        m.setattr(cli, "load_or_create_api_key", lambda: "k")
        m.setattr(cli, "auto_start_server", lambda: True)
        mock_api = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        m.setattr(cli, "api_post", mock_api)
        m.setattr("builtins.input", lambda *_a, **_k: next(inputs))
        args = type("Args", (), {"goal": [], "spirals": None})()
        cli.cmd_run(args)

    mock_api.assert_not_called()
    out = capsys.readouterr().out
    assert "Unknown command: /updat" in out
    assert "Did you mean:" in out
    assert "/update" in out


def test_chat_help_mentions_typo_line(cli, capsys, monkeypatch):
    inputs = iter(["/help", "/exit"])
    with monkeypatch.context() as m:
        m.setattr(cli, "check_llm_api_key", lambda: "deepseek")
        m.setattr(cli, "load_or_create_api_key", lambda: "k")
        m.setattr(cli, "auto_start_server", lambda: True)
        m.setattr(cli, "api_post", lambda *a, **k: None)
        m.setattr("builtins.input", lambda *_a, **_k: next(inputs))
        args = type("Args", (), {"goal": [], "spirals": None})()
        cli.cmd_run(args)
    out = capsys.readouterr().out
    assert "Did you mean" in out


# ── Telegram direct-command suggestions ───────────────────────────────


def test_telegram_suggest_staus_to_status():
    from gateway.adapters.telegram import suggest_telegram_commands

    hits = suggest_telegram_commands("staus")
    assert "status" in hits


def test_telegram_suggest_hepl_to_help():
    from gateway.adapters.telegram import suggest_telegram_commands

    hits = suggest_telegram_commands("hepl")
    assert "help" in hits


def test_telegram_suggest_unrelated_empty():
    from gateway.adapters.telegram import suggest_telegram_commands

    assert suggest_telegram_commands("zzzzzzzz") == []
    assert suggest_telegram_commands("你好") == []


def test_telegram_handle_typo_sends_did_you_mean(monkeypatch):
    from gateway.adapters.telegram import TelegramAdapter

    sent = []

    class _T(TelegramAdapter):
        def __init__(self):
            # Minimal stub — skip real __init__ network/config
            pass

        def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return True

    adapter = _T()
    handled = adapter._handle_direct_command("123", "staus", "")
    assert handled is True
    assert sent
    body = sent[0][1]
    assert "Unknown command: /staus" in body
    assert "Did you mean:" in body
    assert "/status" in body


def test_telegram_handle_exact_status_not_suggestion(monkeypatch):
    from gateway.adapters.telegram import TelegramAdapter

    sent = []

    class _T(TelegramAdapter):
        def __init__(self):
            self._ww_api = "http://127.0.0.1:9"
            self._ww_key = "k"

        def send_message(self, chat_id, text, **kwargs):
            sent.append(text)
            return True

    adapter = _T()
    # Exact status path should not emit Did you mean
    handled = adapter._handle_direct_command("1", "status", "")
    assert handled is True
    assert sent
    assert "Did you mean" not in sent[0]


def test_telegram_unknown_far_returns_false():
    from gateway.adapters.telegram import TelegramAdapter

    class _T(TelegramAdapter):
        def __init__(self):
            pass

        def send_message(self, chat_id, text, **kwargs):
            raise AssertionError("should not send for far mismatches")

    adapter = _T()
    assert adapter._handle_direct_command("1", "zzzzzzzz", "") is False
