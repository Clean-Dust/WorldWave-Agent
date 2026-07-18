"""Tests: LLM Transport module"""
import sys; sys.path.insert(0, ".")
import inspect
import os

from core.transports.base import NormalizedResponse, ToolDef


def test_normalized_response():
    resp = NormalizedResponse(content="Hello")
    assert resp.content == "Hello"
    assert resp.finish_reason == "stop"
    assert resp.tool_calls == []
    assert resp.reasoning_content == ""
    d = resp.to_dict()
    assert d["content"] == "Hello"
    assert d.get("reasoning_content") == ""


def test_normalized_response_with_tool_calls():
    resp2 = NormalizedResponse(content="", tool_calls=[{"name": "test"}])
    assert len(resp2.tool_calls) == 1


def test_normalized_response_reasoning_content():
    """DeepSeek thinking mode: reasoning_content must survive to_dict round-trip."""
    resp = NormalizedResponse(
        content="I'll call a tool",
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": "coding_grep", "arguments": {"pattern": "foo"}},
        }],
        reasoning_content="Need to search for the bug first.",
        finish_reason="tool_calls",
        model="deepseek-v4-flash",
        provider="deepseek",
    )
    assert resp.reasoning_content == "Need to search for the bug first."
    d = resp.to_dict()
    assert d["reasoning_content"] == "Need to search for the bug first."
    assert d["content"] == "I'll call a tool"
    assert len(d["tool_calls"]) == 1


def test_tool_def():
    td = ToolDef(name="test_tool", description="A test", parameters={"type": "object"})
    assert td.name == "test_tool"
    assert td.parameters["type"] == "object"


def test_transport_registry():
    from core.transports import TransportRegistry
    tr = TransportRegistry()
    providers = tr.available()
    assert isinstance(providers, list)


def test_default_transports_contains_all_providers():
    from core.transports.registry import default_transports, FAILOVER_CHAIN
    expected = {
        "deepseek", "openrouter", "openai", "anthropic", "gemini", "xai",
        "groq", "fireworks", "together", "mistral", "ollama", "moonshot",
        "deepinfra", "custom",
    }
    transports = default_transports()
    assert expected.issubset(set(transports.keys()))
    for pid in expected:
        assert pid in FAILOVER_CHAIN, f"{pid} missing from FAILOVER_CHAIN"


def test_infer_provider():
    from core.transports.registry import infer_provider
    assert infer_provider("deepseek/deepseek-v4-flash") == "deepseek"
    assert infer_provider("openai/gpt-4o") == "openai"
    assert infer_provider("anthropic/claude-sonnet-4") == "anthropic"
    assert infer_provider("openrouter/anthropic/claude-sonnet-4") == "openrouter"
    # New native providers
    assert infer_provider("gemini/gemini-2.0-flash") == "gemini"
    assert infer_provider("gemini-2.0-flash") == "gemini"
    assert infer_provider("google/gemini-2.0-flash") == "openrouter"
    assert infer_provider("xai/grok-3") == "xai"
    assert infer_provider("grok-3-mini") == "xai"
    assert infer_provider("groq/llama-3.3-70b-versatile") == "groq"
    assert infer_provider("fireworks/accounts/fireworks/models/x") == "fireworks"
    assert infer_provider("accounts/fireworks/models/llama-v3p1-70b-instruct") == "fireworks"
    assert infer_provider("mistral/mistral-small-latest") == "mistral"
    assert infer_provider("mistral-small-latest") == "mistral"
    assert infer_provider("moonshot/moonshot-v1-8k") == "moonshot"
    assert infer_provider("kimi-latest") == "moonshot"
    assert infer_provider("ollama/llama3.2") == "ollama"
    assert infer_provider("ollama:llama3.2") == "ollama"
    assert infer_provider("together/meta-llama/Meta-Llama-3.1-8B") == "together"
    assert infer_provider("deepinfra/meta-llama/Meta-Llama-3.1-8B") == "deepinfra"
    assert infer_provider("custom/llama3") == "custom"
    assert infer_provider("claude-sonnet-4") == "anthropic"
    assert infer_provider("gpt-4o") == "openai"
    # Unknown vendor/model → openrouter
    assert infer_provider("unknown-vendor/some-model") == "openrouter"


def test_resolve_api_model_strips_prefixes():
    from core.transports.registry import resolve_api_model
    assert resolve_api_model("gemini/gemini-2.0-flash", "gemini") == "gemini-2.0-flash"
    assert resolve_api_model("xai/grok-3", "xai") == "grok-3"
    assert resolve_api_model("groq/llama-3.3-70b-versatile", "groq") == "llama-3.3-70b-versatile"
    assert resolve_api_model("ollama/llama3.2", "ollama") == "llama3.2"
    assert resolve_api_model("ollama:llama3.2", "ollama") == "llama3.2"
    assert resolve_api_model("mistral/mistral-small-latest", "mistral") == "mistral-small-latest"
    assert resolve_api_model("custom/llama3.1-8b", "custom") == "llama3.1-8b"
    assert resolve_api_model("openai/gpt-4o", "openai") == "gpt-4o"
    # deepseek mapping unchanged in spirit
    assert resolve_api_model("deepseek/deepseek-v4-flash", "deepseek") == "deepseek-v4-flash"
    assert resolve_api_model("deepseek-reasoner", "deepseek") == "deepseek-reasoner"


def test_chat_completions_transport():
    from core.transports.chat_completions import ChatCompletionsTransport
    sig = inspect.signature(ChatCompletionsTransport.__init__)
    params = list(sig.parameters.keys())
    assert "api_key_env" in params or "base_url_env" in params
    assert "allow_missing_key" in params
    assert "api_key_env_fallbacks" in params


def test_gemini_api_key_fallback(monkeypatch):
    from core.transports.chat_completions import ChatCompletionsTransport
    t = ChatCompletionsTransport(
        name="gemini",
        api_key_env="GEMINI_API_KEY",
        base_url_env="GEMINI_BASE_URL",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env_fallbacks=["GOOGLE_API_KEY"],
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-fallback-key")
    assert t.get_api_key() == "google-fallback-key"
    monkeypatch.setenv("GEMINI_API_KEY", "primary-gemini-key")
    assert t.get_api_key() == "primary-gemini-key"


def test_ollama_allow_missing_key(monkeypatch):
    from core.transports.chat_completions import ChatCompletionsTransport
    t = ChatCompletionsTransport(
        name="ollama",
        api_key_env="OLLAMA_API_KEY",
        base_url_env="OLLAMA_BASE_URL",
        default_base_url="http://127.0.0.1:11434/v1",
        allow_missing_key=True,
    )
    # Not opted in → empty key (not always in failover)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("WW_USE_OLLAMA", raising=False)
    assert t.get_api_key() == ""

    # Opt-in via WW_USE_OLLAMA → placeholder for Authorization
    monkeypatch.setenv("WW_USE_OLLAMA", "1")
    assert t.get_api_key() == "ollama"

    # Explicit key wins
    monkeypatch.setenv("OLLAMA_API_KEY", "real-ollama-key")
    assert t.get_api_key() == "real-ollama-key"


def test_ollama_not_always_available(monkeypatch):
    from core.transports.registry import default_transports, find_available_providers
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("WW_USE_OLLAMA", raising=False)
    # Clear common keys so only ollama would appear if always-on
    for var in (
        "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "XAI_API_KEY", "GROQ_API_KEY", "CUSTOM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    available = find_available_providers(default_transports())
    assert "ollama" not in available

    monkeypatch.setenv("WW_USE_OLLAMA", "1")
    available2 = find_available_providers(default_transports())
    assert "ollama" in available2


def test_gemini_base_url_default():
    from core.transports.registry import default_transports
    t = default_transports()["gemini"]
    assert "generativelanguage.googleapis.com" in t.get_base_url()
    assert t.get_base_url().endswith("/openai") or "/openai" in t.get_base_url()


def test_chat_completions_extracts_reasoning_content(monkeypatch):
    """Fixture message with reasoning_content must land on NormalizedResponse."""
    import json
    from core.transports.chat_completions import ChatCompletionsTransport

    fixture = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": "I should grep for the failing symbol.",
                "tool_calls": [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "coding_grep",
                        "arguments": '{"pattern": "broken"}',
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(fixture).encode("utf-8")

    def _fake_urlopen(req, timeout=120):
        return _FakeResp()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "core.transports.chat_completions.urllib.request.urlopen",
        _fake_urlopen,
    )

    t = ChatCompletionsTransport(
        name="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
    )
    resp = t.chat(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "fix it"}],
        tools=[{"type": "function", "function": {"name": "coding_grep"}}],
    )
    assert resp.reasoning_content == "I should grep for the failing symbol."
    assert resp.content == ""
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["function"]["name"] == "coding_grep"
    # Internal parse may be dict; message builders re-stringify for the API
    assert resp.tool_calls[0]["function"]["arguments"]["pattern"] == "broken"
    assert resp.to_dict()["reasoning_content"] == resp.reasoning_content


def test_chat_completions_reasoning_alias(monkeypatch):
    """Some providers use 'reasoning' instead of 'reasoning_content'."""
    import json
    from core.transports.chat_completions import ChatCompletionsTransport

    fixture = {
        "choices": [{
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "done",
                "reasoning": "step by step plan",
            },
        }],
        "usage": {},
    }

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(fixture).encode("utf-8")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "core.transports.chat_completions.urllib.request.urlopen",
        lambda req, timeout=120: _FakeResp(),
    )
    t = ChatCompletionsTransport(
        name="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
    )
    resp = t.chat(model="deepseek-v4-flash", messages=[{"role": "user", "content": "hi"}])
    assert resp.reasoning_content == "step by step plan"
    assert resp.content == "done"
