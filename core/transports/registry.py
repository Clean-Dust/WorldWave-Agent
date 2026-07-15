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
# Only providers with a configured key (or opted-in local ollama) are used.
FAILOVER_CHAIN = [
    "deepseek",
    "openrouter",
    "openai",
    "anthropic",
    "gemini",
    "xai",
    "groq",
    "fireworks",
    "together",
    "mistral",
    "moonshot",
    "deepinfra",
    "ollama",
    "custom",
]

# Native OpenAI-compat providers that accept bare model ids (strip provider/ prefix).
_STRIP_PREFIX_PROVIDERS = frozenset({
    "gemini", "xai", "groq", "fireworks", "together", "mistral",
    "ollama", "moonshot", "deepinfra", "custom", "openai", "anthropic",
})

# TODO: OAuth / subscription providers (Claude Max OAuth, OpenAI Codex device
# code, GitHub Copilot, Nous Portal, Vertex ADC) — out of scope for the
# first-class API-key / local path. Register here when implemented.


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
        # Google AI Studio OpenAI-compat:
        # https://generativelanguage.googleapis.com/v1beta/openai/
        "gemini": ChatCompletionsTransport(
            name="gemini",
            api_key_env="GEMINI_API_KEY",
            base_url_env="GEMINI_BASE_URL",
            default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            models=["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro"],
            api_key_env_fallbacks=["GOOGLE_API_KEY"],
        ),
        # xAI Grok — OpenAI-compat at https://api.x.ai/v1
        "xai": ChatCompletionsTransport(
            name="xai",
            api_key_env="XAI_API_KEY",
            base_url_env="XAI_BASE_URL",
            default_base_url="https://api.x.ai/v1",
            models=["grok-3", "grok-3-mini", "grok-2-latest"],
        ),
        "groq": ChatCompletionsTransport(
            name="groq",
            api_key_env="GROQ_API_KEY",
            base_url_env="GROQ_BASE_URL",
            default_base_url="https://api.groq.com/openai/v1",
            models=["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        ),
        # Fireworks inference OpenAI-compat:
        # https://api.fireworks.ai/inference/v1
        "fireworks": ChatCompletionsTransport(
            name="fireworks",
            api_key_env="FIREWORKS_API_KEY",
            base_url_env="FIREWORKS_BASE_URL",
            default_base_url="https://api.fireworks.ai/inference/v1",
            models=["accounts/fireworks/models/llama-v3p1-70b-instruct"],
        ),
        "together": ChatCompletionsTransport(
            name="together",
            api_key_env="TOGETHER_API_KEY",
            base_url_env="TOGETHER_BASE_URL",
            default_base_url="https://api.together.xyz/v1",
            models=["meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"],
        ),
        "mistral": ChatCompletionsTransport(
            name="mistral",
            api_key_env="MISTRAL_API_KEY",
            base_url_env="MISTRAL_BASE_URL",
            default_base_url="https://api.mistral.ai/v1",
            models=["mistral-small-latest", "mistral-large-latest", "codestral-latest"],
        ),
        # Local Ollama OpenAI-compat (key optional). Available only when
        # OLLAMA_API_KEY / OLLAMA_BASE_URL / OLLAMA_HOST / WW_USE_OLLAMA is set.
        "ollama": ChatCompletionsTransport(
            name="ollama",
            api_key_env="OLLAMA_API_KEY",
            base_url_env="OLLAMA_BASE_URL",
            default_base_url="http://127.0.0.1:11434/v1",
            models=["llama3.2", "llama3.1", "qwen2.5", "mistral"],
            allow_missing_key=True,
        ),
        # Moonshot (Kimi) — https://api.moonshot.cn/v1
        "moonshot": ChatCompletionsTransport(
            name="moonshot",
            api_key_env="MOONSHOT_API_KEY",
            base_url_env="MOONSHOT_BASE_URL",
            default_base_url="https://api.moonshot.cn/v1",
            models=["moonshot-v1-8k", "moonshot-v1-32k", "kimi-latest"],
        ),
        "deepinfra": ChatCompletionsTransport(
            name="deepinfra",
            api_key_env="DEEPINFRA_API_KEY",
            base_url_env="DEEPINFRA_BASE_URL",
            default_base_url="https://api.deepinfra.com/v1/openai",
            models=["meta-llama/Meta-Llama-3.1-8B-Instruct"],
        ),
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
    model_lower = (model or "").lower().strip()
    if not model_lower:
        return "deepseek"

    # ollama:model bare form
    if model_lower.startswith("ollama:"):
        return "ollama"

    # Prefixed / multi-segment ids
    if "/" in model_lower:
        if model_lower.startswith("deepseek/"):
            return "deepseek"
        if model_lower.startswith("openai/"):
            return "openai"
        if model_lower.startswith("anthropic/"):
            return "anthropic"
        if model_lower.startswith("custom/"):
            return "custom"
        # Native Gemini prefix (not OpenRouter's google/gemini-*)
        if model_lower.startswith("gemini/"):
            return "gemini"
        if model_lower.startswith("xai/"):
            return "xai"
        if model_lower.startswith("groq/"):
            return "groq"
        if model_lower.startswith("fireworks/") or model_lower.startswith("accounts/fireworks"):
            return "fireworks"
        if model_lower.startswith("mistral/"):
            return "mistral"
        if model_lower.startswith("moonshot/") or model_lower.startswith("kimi/"):
            return "moonshot"
        if model_lower.startswith("ollama/"):
            return "ollama"
        if model_lower.startswith("together/"):
            return "together"
        if model_lower.startswith("deepinfra/"):
            return "deepinfra"
        # google/gemini-* and other third-party multi-segment → OpenRouter
        return "openrouter"

    # Bare model names
    if model_lower in ("deepseek-chat", "deepseek-reasoner") or model_lower.startswith("deepseek"):
        return "deepseek"
    if model_lower.startswith("claude"):
        return "anthropic"
    if model_lower.startswith(("gpt", "o1", "o3")):
        return "openai"
    # bare gemini-* → native Gemini (not OpenRouter)
    if model_lower.startswith("gemini"):
        return "gemini"
    if model_lower.startswith("grok"):
        return "xai"
    if model_lower.startswith("mistral-") or model_lower in (
        "mistral-small-latest", "mistral-large-latest", "codestral-latest",
    ):
        return "mistral"
    if model_lower.startswith("kimi") or model_lower.startswith("moonshot"):
        return "moonshot"

    return "deepseek"


def resolve_api_model(model: str, provider: str) -> str:
    """Convert WW model name to provider-accepted API model name."""
    if not model:
        return model

    if provider == "deepseek":
        # Keep existing API mapping used by chat/completions transport.
        lower = model.lower()
        if "reasoner" in lower:
            return "deepseek-reasoner"
        if "flash" in lower or "pro" in lower:
            return "deepseek-v4-flash"
        if "/" in model:
            return "deepseek-v4-flash"
        return model

    if provider == "ollama":
        if model.lower().startswith("ollama/"):
            return model[7:]
        if model.lower().startswith("ollama:"):
            return model.split(":", 1)[1]
        return model

    if provider == "custom":
        # Strip custom/ prefix: custom/llama3.1-8b → llama3.1-8b
        if model.startswith("custom/"):
            return model[7:]
        return model

    if provider in _STRIP_PREFIX_PROVIDERS:
        prefix = f"{provider}/"
        if model.lower().startswith(prefix):
            return model[len(prefix):]
        return model

    return model


def find_available_providers(transports: Dict[str, ProviderTransport]) -> List[str]:
    """list providers with available API key (or opted-in local ollama)."""
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
