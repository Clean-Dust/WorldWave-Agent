"""
ww/core/transports/anthropic.py — Anthropic Claude Transport

supports：
- Anthropic Messages API (claude-sonnet-4, claude-opus-4, etc.)
- System message separation (Anthropic format)
- Prompt caching（ephemeral）
- Extended thinking
"""

from __future__ import annotations
import json
import os
import urllib.request
import urllib.error
from typing import Dict, List, Optional

from .base import ProviderTransport, NormalizedResponse


ANTHROPIC_VERSION = "2023-06-01"


class AnthropicTransport(ProviderTransport):
    """Anthropic Messages API Transport"""

    def __init__(self):
        self._api_key_env = "ANTHROPIC_API_KEY"
        self._base_url_env = "ANTHROPIC_BASE_URL"
        self._default_base_url = "https://api.anthropic.com/v1"

    @property
    def name(self) -> str:
        return "anthropic"

    def get_api_key(self) -> str:
        return os.environ.get(self._api_key_env, "")

    def get_base_url(self) -> str:
        return os.environ.get(self._base_url_env, self._default_base_url).rstrip("/")

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
        api_key = self.get_api_key()
        if not api_key:
            raise RuntimeError("[anthropic] No API key")

        base_url = self.get_base_url()
        endpoint = f"{base_url}/messages"

        # separate system message (Anthropic system message at top level)
        system_content = ""
        anon_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_content = msg.get("content", "")
            else:
                # convert OpenAI format → Anthropic format
                anon_msg = self._to_anthropic_msg(msg)
                anon_messages.append(anon_msg)

        payload = {
            "model": self._resolve_model(model),
            "messages": anon_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # System
        if system_content:
            payload["system"] = [{"type": "text", "text": system_content, "cache_control": {"type": "ephemeral"}}]

        # Tools
        if tools and self.supports_tools():
            payload["tools"] = self._convert_tools(tools)

        # JSON mode (Anthropic guided by hint)
        if json_mode:
            if system_content:
                payload["system"][0]["text"] += "\n\nCRITICAL: You MUST output valid JSON only."
            else:
                payload["system"] = [{"type": "text", "text": "CRITICAL: You MUST output valid JSON only."}]

        # Extended thinking for Claude 4 models
        if "thinking" in kwargs:
            payload["thinking"] = kwargs["thinking"]

        # Streaming
        if stream:
            payload["stream"] = True

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data, headers=headers, method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"[anthropic] HTTP {e.code}: {body}")
        except Exception as e:
            raise RuntimeError(f"[anthropic] Request failed: {e}")

        content = ""
        tool_calls = []
        usage = {}

        # Parse Anthropic response format
        for block in result.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tc_id = block.get("id", "")
                tc_name = block.get("name", "")
                tc_input = block.get("input", {})
                tool_calls.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc_name,
                        "arguments": tc_input if isinstance(tc_input, dict) else {},
                    }
                })

        # Usage
        usage_raw = result.get("usage", {})
        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_creation_input_tokens": usage_raw.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage_raw.get("cache_read_input_tokens", 0),
        }

        content = self._clean_markdown(content)

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=result.get("stop_reason", "end_turn"),
            usage=usage,
            model=model,
            provider="anthropic",
            cached=usage.get("cache_read_input_tokens", 0) > 0,
        )

    def _to_anthropic_msg(self, msg: Dict) -> Dict:
        """Convert OpenAI message to Anthropic format"""
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Anthropic uses "assistant" and "user" (no "tool" role)
        if role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": content,
                    }
                ],
            }

        # Assistant with tool_calls
        tool_calls = msg.get("tool_calls", [])
        if role == "assistant" and tool_calls:
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in tool_calls:
                fn = tc.get("function", {})
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": fn.get("arguments", {}),
                })
            return {"role": "assistant", "content": blocks}

        return {"role": role, "content": content}

    def _convert_tools(self, tools: List[Dict]) -> List[Dict]:
        """Convert OpenAI tools to Anthropic format"""
        anon_tools = []
        for tool in tools:
            fn = tool.get("function", tool)
            anon_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return anon_tools

    def _resolve_model(self, model: str) -> str:
        """Normalize model name for Anthropic API"""
        # Remove provider prefix
        model = model.replace("anthropic/", "")
        # Known models
        known = {
            "claude-sonnet-4": "claude-sonnet-4-20250514",
            "claude-opus-4": "claude-opus-4-20250514",
            "claude-haiku-3.5": "claude-3-5-haiku-20241022",
        }
        return known.get(model, model)

    def _clean_markdown(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if len(lines) > 1 and lines[-1].strip() == "```":
                lines = lines[1:-1] if lines[0].strip().startswith("```") else lines[:-1]
            elif len(lines) == 1:
                lines = [lines[0].lstrip("`")]
            text = "\n".join(lines).strip()
        return text

    def supports_json_mode(self) -> bool:
        return False  # Claude doesn't support response_format

    def supports_tools(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return True
