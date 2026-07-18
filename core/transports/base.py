"""
ww/core/transports/base.py — Provider Transport abstraction layer

each provider implements a Transport, responsible for:
- message format conversion (OpenAI format → provider-native format)
- tool definition conversion
- responseregularization
- Stream process

LLMClient no longer directly calls API, but is via Transport proxy.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class ToolDef:
    """tool definition (OpenAI function calling format)"""
    def __init__(self, name: str, description: str = "", parameters: Dict = None):
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}

    def to_openai(self) -> Dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }


class NormalizedResponse:
    """standardized response — all Transport output unified to this format"""
    def __init__(
        self,
        content: str = "",
        tool_calls: Optional[List[Dict]] = None,
        finish_reason: str = "stop",
        usage: Optional[Dict] = None,
        model: str = "",
        provider: str = "",
        cached: bool = False,
        streaming: bool = False,
        reasoning_content: str = "",
    ):
        self.content = content or ""
        self.tool_calls = tool_calls or []
        self.finish_reason = finish_reason
        self.usage = usage or {}
        self.model = model
        self.provider = provider
        self.cached = cached
        self.streaming = streaming
        # DeepSeek thinking / reasoner models return reasoning_content that
        # MUST be echoed back on subsequent assistant messages in tool loops.
        self.reasoning_content = reasoning_content or ""

    def to_dict(self) -> Dict:
        return {
            "content": self.content,
            "tool_calls": self.tool_calls,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "model": self.model,
            "provider": self.provider,
            "cached": self.cached,
            "streaming": self.streaming,
            "reasoning_content": self.reasoning_content,
        }


class ProviderTransport(ABC):
    """Provider Transport abstract base class"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (deepseek, openai, anthropic, etc.)"""
        ...

    @abstractmethod
    def get_api_key(self) -> str:
        ...

    @abstractmethod
    def get_base_url(self) -> str:
        ...

    @abstractmethod
    def chat(
        self,
        model: str,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False,
        stream: bool = False,
        **kwargs,
    ) -> NormalizedResponse:
        ...

    def supports_json_mode(self) -> bool:
        return True

    def supports_tools(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return True

    def estimate_tokens(self, text: str) -> int:
        """roughly estimate token count (~4 chars/token)"""
        return len(text) // 4 + 10
