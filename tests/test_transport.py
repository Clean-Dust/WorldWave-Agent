"""Tests: LLM Transport module"""
import sys; sys.path.insert(0, ".")
import inspect

from core.transports.base import NormalizedResponse, ToolDef


def test_normalized_response():
    resp = NormalizedResponse(content="Hello")
    assert resp.content == "Hello"
    assert resp.finish_reason == "stop"
    assert resp.tool_calls == []


def test_normalized_response_with_tool_calls():
    resp2 = NormalizedResponse(content="", tool_calls=[{"name": "test"}])
    assert len(resp2.tool_calls) == 1


def test_tool_def():
    td = ToolDef(name="test_tool", description="A test", parameters={"type": "object"})
    assert td.name == "test_tool"
    assert td.parameters["type"] == "object"


def test_transport_registry():
    from core.transports import TransportRegistry
    tr = TransportRegistry()
    providers = tr.available()
    assert isinstance(providers, list)


def test_infer_provider():
    from core.transports.registry import infer_provider
    assert infer_provider("deepseek/deepseek-v4-flash") == "deepseek"
    assert infer_provider("openai/gpt-4o") == "openai"
    assert infer_provider("anthropic/claude-sonnet-4") == "anthropic"
    assert infer_provider("openrouter/anthropic/claude-sonnet-4") == "openrouter"


def test_chat_completions_transport():
    from core.transports.chat_completions import ChatCompletionsTransport
    sig = inspect.signature(ChatCompletionsTransport.__init__)
    params = list(sig.parameters.keys())
    assert "api_key_env" in params or "base_url_env" in params
