"""
ww/core/transports/registry.py — Transport registration table

manage all available Transport, provide provider discovery, inference, failover.
"""

from __future__ import annotations
from typing import Dict, List, Optional

from .base import ProviderTransport
from .chat_completions import ChatCompletionsTransport
from .anthropic import AnthropicTransport


# ── default Failover chain ──
FAILOVER_CHAIN = ["deepseek", "openrouter", "openai", "anthropic"]


def default_transports() -> Dict[str, ProviderTransport]:
    """create default Transport registration table"""
    return {
        "deepseek": ChatCompletionsTransport(
            name="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            base_url_env="DEEPSEEK_BASE_URL",
            default_base_url="https://api.deepseek.com/v1",
            models=[
                "deepseek-chat", "deepseek-reasoner",
                "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro",
            ],
        ),
        "openrouter": ChatCompletionsTransport(
            name="openrouter",
            api_key_env="OPENROUTER_API_KEY",
            base_url_env="OPENROUTER_BASE_URL",
            default_base_url="https://openrouter.ai/api/v1",
            models=[
                "anthropic/claude-sonnet-4", "anthropic/claude-opus-4",
                "openai/gpt-4o", "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro", "google/gemini-2.0-flash",
            ],
            extra_headers={
                "HTTP-Referer": "https://github.com/worldwave",
                "X-Title": "Worldwave",
            },
        ),
        "openai": ChatCompletionsTransport(
            name="openai",
            api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_BASE_URL",
            default_base_url="https://api.openai.com/v1",
            models=["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"],
        ),
        "anthropic": AnthropicTransport(),
        "custom": ChatCompletionsTransport(
            name="custom",
            api_key_env="CUSTOM_API_KEY",
            base_url_env="CUSTOM_BASE_URL",
            default_base_url="http://localhost:11434/v1",  # Ollama default
            models=["custom/*"],  # Wildcard — accept any model name
        ),
    }


def infer_provider(model: str) -> str:
    """infer provider based on model name"""
    model_lower = model.lower()

    # OpenRouter style: provider/model
    if "/" in model_lower:
        if model_lower.startswith("deepseek/"):
            return "deepseek"
        if model_lower.startswith("openai/"):
            return "openai"
        if model_lower.startswith("anthropic/"):
            return "anthropic"
        if model_lower.startswith("custom/"):
            return "custom"
        return "openrouter"
    if model_lower in ("deepseek-chat", "deepseek-reasoner"):
        return "deepseek"
    if model_lower.startswith("claude"):
        return "anthropic"
    if model_lower.startswith(("gpt", "o1", "o3")):
        return "openai"
    if model_lower.startswith("gemini"):
        return "openrouter"

    return "deepseek"


def resolve_api_model(model: str, provider: str) -> str:
    """will convert WW model name to provider accepted API model name"""
    if provider == "deepseek":
        if "flash" in model.lower() or "pro" in model.lower():
            return "deepseek-chat"
        if "reasoner" in model.lower():
            return "deepseek-reasoner"
        if "/" in model:
            return "deepseek-chat"
    if provider == "custom":
        # Strip custom/ prefix: custom/llama3.1-8b → llama3.1-8b
        if model.startswith("custom/"):
            return model[7:]
    return model


def find_available_providers(transports: Dict[str, ProviderTransport]) -> List[str]:
    """list providers with available API key"""
    available = []
    for name, transport in transports.items():
        if transport.get_api_key():
            available.append(name)
    return available


class TransportRegistry:
    """Transport register and route"""

    def __init__(self, transports: Dict[str, ProviderTransport] = None):
        self._transports = transports or default_transports()

    def get(self, name: str) -> Optional[ProviderTransport]:
        return self._transports.get(name)

    def register(self, name: str, transport: ProviderTransport):
        self._transports[name] = transport

    def infer(self, model: str) -> ProviderTransport:
        provider = infer_provider(model)
        return self._transports.get(provider)

    def available(self) -> List[str]:
        return find_available_providers(self._transports)

    def failover_chain(self, primary: str) -> List[str]:
        """generate failover order"""
        chain = [primary]
        for p in FAILOVER_CHAIN:
            if p != primary and p not in chain:
                chain.append(p)
        # Filter to only available providers
        available = self.available()
        return [p for p in chain if p in available]
