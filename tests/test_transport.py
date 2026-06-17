"""Tests: LLM Transport module"""
import sys; sys.path.insert(0, ".")

from core.transports.base import NormalizedResponse, ToolDef

# NormalizedResponse
resp = NormalizedResponse(content="Hello")
assert resp.content == "Hello"
assert resp.finish_reason == "stop"
assert resp.tool_calls == []
print("NormalizedResponse: OK")

resp2 = NormalizedResponse(content="", tool_calls=[{"name": "test"}])
assert len(resp2.tool_calls) == 1
print("Tool calls: OK")

# ToolDef
td = ToolDef(name="test_tool", description="A test", parameters={"type": "object"})
assert td.name == "test_tool"
assert td.parameters["type"] == "object"
print("ToolDef: OK")

# TransportRegistry
from core.transports import TransportRegistry
tr = TransportRegistry()
providers = tr.available()
assert isinstance(providers, list)
print(f"TransportRegistry: {len(providers)} providers registered")

# Infer provider from model
from core.transports.registry import infer_provider
assert infer_provider("deepseek/deepseek-v4-flash") == "deepseek"
assert infer_provider("openai/gpt-4o") == "openai"
assert infer_provider("anthropic/claude-sonnet-4") == "anthropic"
assert infer_provider("openrouter/anthropic/claude-sonnet-4") == "openrouter"
print("Provider inference: OK")

# Try to instantiate ChatCompletions transport directly
from core.transports.chat_completions import ChatCompletionsTransport
# Check available kwargs
import inspect
sig = inspect.signature(ChatCompletionsTransport.__init__)
params = list(sig.parameters.keys())
print(f"ChatCompletionsTransport params: {params}")

print("ALL TRANSPORT TESTS PASSED")
