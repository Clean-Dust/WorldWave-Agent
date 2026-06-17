"""
ww/core/transports/chat_completions.py — OpenAI-compatible Transport

supports：DeepSeek API、OpenRouter、OpenAI、any  OpenAI-compatible endpoint
Features:
- standard chat/completions format
- complete function calling supports
- streaming support
- Prompt caching headers（OpenAI）
- tool calls (tool_calls)
"""

from __future__ import annotations
import json
import os
import urllib.request
import urllib.error
from typing import Dict, List, Optional

from .base import ProviderTransport, NormalizedResponse


class ChatCompletionsTransport(ProviderTransport):
    """OpenAI-compatible chat/completions Transport"""

    def __init__(
        self,
        name: str,
        api_key_env: str,
        base_url_env: str,
        default_base_url: str,
        models: List[str] = None,
        extra_headers: Dict[str, str] = None,
    ):
        self._name = name
        self._api_key_env = api_key_env
        self._base_url_env = base_url_env
        self._default_base_url = default_base_url
        self._models = models or []
        self._extra_headers = extra_headers or {}

    @property
    def name(self) -> str:
        return self._name

    def get_api_key(self) -> str:
        return os.environ.get(self._api_key_env, "")

    def get_base_url(self) -> str:
        return os.environ.get(self._base_url_env, self._default_base_url).rstrip("/")

    def models(self) -> List[str]:
        return self._models

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
            raise RuntimeError(f"[{self._name}] No API key (env: {self._api_key_env})")

        base_url = self.get_base_url()
        endpoint = f"{base_url}/chat/completions"

        # Map WW model names to API model names
        api_model = self._resolve_model(model)

        payload = {
            "model": api_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Tools
        if tools:
            payload["tools"] = tools

        # JSON mode
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        # Streaming
        if stream and self.supports_streaming():
            payload["stream"] = True

        # Extra params
        payload.update(kwargs)

        data = json.dumps(payload).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        headers.update(self._extra_headers)

        # Prompt caching for OpenAI
        if self._name == "openai":
            headers["OpenAI-Beta"] = "assistants=v2"

        req = urllib.request.Request(
            endpoint,
            data=data,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"[{self._name}] HTTP {e.code}: {body}")
        except Exception as e:
            raise RuntimeError(f"[{self._name}] Request failed: {e}")

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError(f"[{self._name}] No choices in response: {result}")

        choice = choices[0]
        message = choice.get("message", {})

        # Extract content
        content = message.get("content", "") or ""

        # Extract tool calls
        tool_calls = []
        for tc in message.get("tool_calls", []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {"raw": fn.get("arguments", "")}
            tool_calls.append({
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": args,
                }
            })

        # Clean markdown from content
        content = self._clean_markdown(content)

        # Usage
        usage = result.get("usage", {})

        finish_reason = choice.get("finish_reason", "stop")

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=api_model,
            provider=self._name,
            cached=usage.get("prompt_cache_hit_tokens", 0) > 0,
        )

    def _resolve_model(self, model: str) -> str:
        """Map WW model name to API model name"""
        if self._name == "deepseek":
            if "flash" in model.lower() or "pro" in model.lower():
                return "deepseek-v4-flash"
            if "reasoner" in model.lower():
                return "deepseek-reasoner"
            if "/" in model:
                return "deepseek-v4-flash"
        return model

    def _clean_markdown(self, text: str) -> str:
        """Remove markdown code fences from output"""
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
        return True

    def supports_tools(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return True

    def chat_stream_iter(self, model: str, messages: List[Dict],
                         tools: Optional[List[Dict]] = None,
                         temperature: float = 0.7,
                         max_tokens: int = 4096,
                         json_mode: bool = False,
                         **kwargs):
        """Yields token chunks from the LLM as they arrive (SSE stream).

        Returns an iterator of (delta_text, finish_reason) tuples.
        finish_reason is None until the final chunk.
        """
        import http.client
        import ssl

        api_key = self.get_api_key()
        if not api_key:
            raise RuntimeError(f"[{self._name}] No API key")

        base_url = self.get_base_url()
        # Parse URL
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        host = parsed.netloc
        path = parsed.path.rstrip("/") + "/chat/completions"

        api_model = self._resolve_model(model)

        payload = {
            "model": api_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        if json_mode and self.supports_json_mode():
            payload["response_format"] = {"type": "json_object"}
        payload.update(kwargs)

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
        }

        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, context=ctx, timeout=120)
        try:
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()

            if resp.status != 200:
                body = resp.read().decode(errors="replace")[:200]
                raise RuntimeError(f"[{self._name}] HTTP {resp.status}: {body}")

            # Read SSE stream line by line
            buffer = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            return
                        try:
                            obj = json.loads(data_str)
                            choices = obj.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                finish = choices[0].get("finish_reason")
                                if content:
                                    yield (content, finish)
                        except json.JSONDecodeError:
                            continue
        finally:
            conn.close()
