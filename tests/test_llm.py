"""Tests: LLM client module — provider resolution, chat, failover, phase prompts."""

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, ".")

from core.llm import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    LLMClient,
    PHASE_PROMPTS,
    WW_IDENTITY,
    create_llm,
)
from core.transports.base import NormalizedResponse, ProviderTransport


# ── helpers ──


def make_mock_transport(name="mock", api_key="sk-test", base_url="https://mock.api/v1"):
    """Create a mock ProviderTransport."""
    t = MagicMock()
    t.name = name
    t.get_api_key.return_value = api_key
    t.get_base_url.return_value = base_url
    t.chat.return_value = NormalizedResponse(
        content='{"result": "ok"}',
        provider=name,
        model="mock-model",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    t.supports_json_mode.return_value = True
    t.supports_tools.return_value = True
    t.supports_streaming.return_value = True
    t.chat_stream_iter.return_value = iter([("Hello", None), (" world", "stop")])
    return t


def make_mock_registry(transports=None):
    """Create a mock TransportRegistry."""
    registry = MagicMock()
    if transports:
        registry._transports = transports
        registry.get.side_effect = lambda name: transports.get(name)
        registry.available.return_value = list(transports.keys())
        registry.failover_chain.side_effect = (
            lambda primary: [primary] + [n for n in transports if n != primary]
        )
    else:
        registry._transports = {}
        registry.get.return_value = None
        registry.available.return_value = []
        registry.failover_chain.return_value = []
    return registry


# ── LLMClient init ──


class TestLLMClientInit:
    def test_defaults(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        assert client.model == DEFAULT_MODEL
        assert client.temperature == DEFAULT_TEMPERATURE
        assert client.max_tokens == DEFAULT_MAX_TOKENS
        assert client.failover is False
        assert client._provider == "deepseek"

    def test_custom_model_and_temperature(self):
        t = make_mock_transport("openai")
        reg = make_mock_registry({"openai": t})
        client = LLMClient(
            model="gpt-4o",
            temperature=0.3,
            max_tokens=2048,
            provider="openai",
            transports=reg,
        )
        assert client.model == "gpt-4o"
        assert client.temperature == 0.3
        assert client.max_tokens == 2048

    def test_explicit_provider(self):
        t = make_mock_transport("openrouter")
        reg = make_mock_registry({"openrouter": t})
        client = LLMClient(provider="openrouter", transports=reg)
        assert client._provider == "openrouter"

    def test_override_key_and_base(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(
            api_key="override-key",
            api_base="https://custom.api/v1",
            transports=reg,
        )
        assert client._override_key == "override-key"
        assert client._override_base == "https://custom.api/v1"

    def test_failover_enabled(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(failover=True, transports=reg)
        assert client.failover is True


# ── switch_provider ──


class TestSwitchProvider:
    def test_switch_to_registered_provider(self):
        t1 = make_mock_transport("deepseek")
        t2 = make_mock_transport("openai")
        reg = make_mock_registry({"deepseek": t1, "openai": t2})
        client = LLMClient(transports=reg)
        assert client._provider == "deepseek"
        client.switch_provider("openai")
        assert client._provider == "openai"
        assert client._transport is t2

    def test_switch_to_unknown_provider_noop(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        client.switch_provider("nonexistent")
        assert client._provider == "deepseek"
        assert client._transport is t


# ── _resolve_api_key / _resolve_api_base ──


class TestResolveKeyAndBase:
    def test_resolve_key_from_transport(self):
        t = make_mock_transport("deepseek", api_key="sk-from-transport")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        assert client._resolve_api_key() == "sk-from-transport"

    def test_resolve_key_override(self):
        t = make_mock_transport("deepseek", api_key="sk-from-transport")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(api_key="sk-override", transports=reg)
        assert client._resolve_api_key() == "sk-override"

    def test_resolve_key_none(self):
        reg = make_mock_registry({})
        client = LLMClient(provider="deepseek", transports=reg)
        assert client._resolve_api_key() == ""

    def test_resolve_base_from_transport(self):
        t = make_mock_transport("deepseek", base_url="https://api.ds.com/v1")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        assert client._resolve_api_base() == "https://api.ds.com/v1"

    def test_resolve_base_override(self):
        t = make_mock_transport("deepseek", base_url="https://api.ds.com/v1")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(api_base="https://custom/v1", transports=reg)
        assert client._resolve_api_base() == "https://custom/v1"

    def test_resolve_base_fallback(self):
        reg = make_mock_registry({})
        client = LLMClient(provider="deepseek", transports=reg)
        assert "api.deepseek.com" in client._resolve_api_base()


# ── _build_provider_chain ──


class TestBuildProviderChain:
    def test_single_provider_without_failover(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t, "openai": make_mock_transport("openai")})
        client = LLMClient(transports=reg)
        chain = client._build_provider_chain()
        assert chain == ["deepseek"]

    def test_failover_chain(self):
        t1 = make_mock_transport("deepseek")
        t2 = make_mock_transport("openai")
        reg = make_mock_registry({"deepseek": t1, "openai": t2})
        reg.failover_chain.return_value = ["deepseek", "openai"]
        client = LLMClient(failover=True, transports=reg)
        chain = client._build_provider_chain()
        assert len(chain) >= 1
        assert "deepseek" in chain


# ── _inject_phase_prompt ──


class TestInjectPhasePrompt:
    def test_injects_identity_when_no_system_message(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        msgs = [{"role": "user", "content": "Hello"}]
        result = client._inject_phase_prompt(msgs, phase="")
        assert result[0]["role"] == "system"
        assert "Worldwave" in result[0]["content"]

    def test_injects_phase_prompt(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        msgs = [{"role": "user", "content": "Plan this"}]
        result = client._inject_phase_prompt(msgs, phase="plan")
        system = result[0]["content"]
        assert "Worldwave" in system
        assert "strategic planning" in system.lower() or "plan" in system.lower()

    def test_prepends_to_existing_system_message(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        msgs = [{"role": "system", "content": "Custom instructions"}]
        result = client._inject_phase_prompt(msgs, phase="perceive")
        system = result[0]["content"]
        assert "Worldwave" in system
        assert "Custom instructions" in system

    def test_no_phase_no_identity_when_system_present(self):
        """With no phase, WW_IDENTITY is still injected; only phase prompt is skipped."""
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        msgs = [{"role": "system", "content": "Custom"}]
        result = client._inject_phase_prompt(msgs, phase="")
        assert "Worldwave" in result[0]["content"]

    def test_preserves_non_system_messages(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        msgs = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ]
        result = client._inject_phase_prompt(msgs, phase="")
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "Q1"
        assert result[2]["role"] == "assistant"


# ── chat ──


class TestChat:
    def test_chat_returns_content_string(self):
        t = make_mock_transport("deepseek")
        t.chat.return_value = NormalizedResponse(
            content="response text",
            provider="deepseek",
            model="deepseek-chat",
            usage={"input_tokens": 5, "output_tokens": 3},
        )
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        result = client.chat([{"role": "user", "content": "Hi"}])
        assert result == "response text"
        assert client.total_input_tokens == 5
        assert client.total_output_tokens == 3
        assert client.last_provider == "deepseek"

    def test_chat_with_tools(self):
        t = make_mock_transport("deepseek")
        t.chat.return_value = NormalizedResponse(
            content="",
            tool_calls=[{"function": {"name": "shell", "arguments": {}}}],
            provider="deepseek",
            model="deepseek-chat",
        )
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        resp = client.chat_with_tools(
            [{"role": "user", "content": "Run ls"}],
            tools=[{"type": "function", "function": {"name": "shell"}}],
        )
        assert isinstance(resp, NormalizedResponse)
        assert len(resp.tool_calls) == 1

    def test_chat_passes_correct_params_to_transport(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(temperature=0.5, max_tokens=1024, transports=reg)
        client.chat([{"role": "user", "content": "Hi"}])
        call_kwargs = t.chat.call_args.kwargs
        assert call_kwargs["temperature"] == 0.5
        assert call_kwargs["max_tokens"] == 1024
        assert call_kwargs["json_mode"] is True

    def test_chat_override_temp_and_tokens(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        client.chat(
            [{"role": "user", "content": "Hi"}],
            temperature=0.1,
            max_tokens=512,
        )
        call_kwargs = t.chat.call_args.kwargs
        assert call_kwargs["temperature"] == 0.1
        assert call_kwargs["max_tokens"] == 512

    def test_chat_all_providers_fail_raises(self):
        t = make_mock_transport("deepseek")
        t.chat.side_effect = RuntimeError("API down")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        with pytest.raises(RuntimeError, match="All provider calls failed"):
            client.chat([{"role": "user", "content": "Hi"}])


# ── chat_json ──


class TestChatJson:
    def test_chat_json_parses_response(self):
        t = make_mock_transport("deepseek")
        t.chat.return_value = NormalizedResponse(
            content='{"key": "value", "num": 42}',
            provider="deepseek",
            model="deepseek-chat",
        )
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        result = client.chat_json([{"role": "user", "content": "Give me JSON"}])
        assert result == {"key": "value", "num": 42}

    def test_chat_json_invalid_json_returns_error_dict(self):
        t = make_mock_transport("deepseek")
        t.chat.return_value = NormalizedResponse(
            content="not valid json at all",
            provider="deepseek",
            model="deepseek-chat",
        )
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        result = client.chat_json([{"role": "user", "content": "Give me JSON"}])
        assert result["parse_error"] is True
        assert result["raw"] == "not valid json at all"


# ── chat_with_tools ──


class TestChatWithTools:
    def test_returns_normalized_response(self):
        t = make_mock_transport("deepseek")
        t.chat.return_value = NormalizedResponse(
            content="Using tool",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": {"path": "/tmp/x"}},
                }
            ],
            provider="deepseek",
            model="deepseek-chat",
        )
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        resp = client.chat_with_tools(
            [{"role": "user", "content": "Read /tmp/x"}],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
        )
        assert isinstance(resp, NormalizedResponse)
        assert resp.tool_calls[0]["function"]["name"] == "read_file"
        # json_mode should be False for tool calls
        call_kwargs = t.chat.call_args.kwargs
        assert call_kwargs["json_mode"] is False


# ── failover ──


class TestFailover:
    def test_failover_tries_next_provider(self):
        t1 = make_mock_transport("deepseek")
        t1.chat.side_effect = RuntimeError("deepseek down")
        t2 = make_mock_transport("openai")
        t2.chat.return_value = NormalizedResponse(
            content="fallback response",
            provider="openai",
            model="gpt-4o",
            usage={"input_tokens": 3, "output_tokens": 1},
        )
        reg = make_mock_registry({"deepseek": t1, "openai": t2})
        reg.failover_chain.return_value = ["deepseek", "openai"]
        client = LLMClient(failover=True, transports=reg)
        result = client.chat([{"role": "user", "content": "Hi"}])
        assert result == "fallback response"
        assert client.last_provider == "openai"

    def test_failover_skips_providers_without_key(self):
        t1 = make_mock_transport("deepseek")
        t1.chat.side_effect = RuntimeError("down")
        t2 = make_mock_transport("openai", api_key="")  # no key
        t3 = make_mock_transport("openrouter")
        t3.chat.return_value = NormalizedResponse(
            content="third provider", provider="openrouter", model="test"
        )
        reg = make_mock_registry({"deepseek": t1, "openai": t2, "openrouter": t3})
        reg.failover_chain.return_value = ["deepseek", "openai", "openrouter"]
        client = LLMClient(failover=True, transports=reg)
        result = client.chat([{"role": "user", "content": "Hi"}])
        assert result == "third provider"


# ── usage_stats / available_providers ──


class TestUsageStats:
    def test_initial_stats(self):
        t = make_mock_transport("deepseek")
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        stats = client.usage_stats()
        assert stats["total_input_tokens"] == 0
        assert stats["total_output_tokens"] == 0
        assert stats["total_tokens"] == 0

    def test_stats_after_chat(self):
        t = make_mock_transport("deepseek")
        t.chat.return_value = NormalizedResponse(
            content="ok",
            provider="deepseek",
            model="deepseek-chat",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        reg = make_mock_registry({"deepseek": t})
        client = LLMClient(transports=reg)
        client.chat([{"role": "user", "content": "Hi"}])
        stats = client.usage_stats()
        assert stats["total_input_tokens"] == 100
        assert stats["total_output_tokens"] == 50
        assert stats["total_tokens"] == 150
        assert stats["last_provider"] == "deepseek"
        assert stats["last_model"] == "deepseek-chat"


class TestAvailableProviders:
    def test_lists_available_providers(self):
        t1 = make_mock_transport("deepseek")
        t2 = make_mock_transport("openai")
        reg = make_mock_registry({"deepseek": t1, "openai": t2})
        client = LLMClient(transports=reg)
        providers = client.available_providers()
        assert "deepseek" in providers
        assert "openai" in providers


# ── create_llm ──


class TestCreateLLM:
    def test_empty_config_returns_default_client(self):
        client = create_llm()
        assert isinstance(client, LLMClient)
        assert client.model == DEFAULT_MODEL

    def test_config_overrides_defaults(self):
        client = create_llm({"model": "gpt-4o", "temperature": 0.2, "failover": True})
        assert client.model == "gpt-4o"
        assert client.temperature == 0.2
        assert client.failover is True

    def test_none_config(self):
        client = create_llm(None)
        assert isinstance(client, LLMClient)


# ── constants ──


class TestConstants:
    def test_ww_identity_is_string(self):
        assert isinstance(WW_IDENTITY, str)
        assert len(WW_IDENTITY) > 50

    def test_phase_prompts_have_five_phases(self):
        assert set(PHASE_PROMPTS.keys()) == {"perceive", "recall", "plan", "evaluate", "learn"}

    def test_default_model_is_deepseek(self):
        assert "deepseek" in DEFAULT_MODEL
