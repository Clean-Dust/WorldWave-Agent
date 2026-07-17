"""
ww/core/llm.py — Worldwave multi-LLM client v0.5

Based on Transport abstraction layer, new LLM client:
- TransportRegistry managementall  provider
- Each provider has independent Transport implementation
- Unified response format (NormalizedResponse)
- complete tool calling supports
- Auto provider inference + failover
"""

from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Optional

from core.transports import (
    ProviderTransport,
    NormalizedResponse,
    TransportRegistry,
    infer_provider,
    find_available_providers,
)

logger = logging.getLogger(__name__)


# ── defaultvalue ──

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096
RETRY_MAX = 3
RETRY_DELAY = 2.0


# ── Worldwave Identity Prompt ──

WW_IDENTITY = (
    "You are Worldwave (WW), an autonomous AI agent framework with full control over your host machine. "
    "Your architecture is a spiral cognitive loop: Perception → Gate → Recall → Plan → Act → Evaluate → Learn. "
    "Core modules: memory system (hippocampus + amygdala), subconscious engine, "
    "tool registry (100+ tools including shell, file, browser automation), multi-platform gateways. "
    "You run inside ~/worldwave/ on Linux. You can read files, run commands, check configs.\n"
    "IMPORTANT: When asked about your configuration, backend, or environment, "
    "USE your tools to inspect the actual system — read ~/.env files, check environment variables, "
    "run diagnostic commands. Do not fabricate answers from prompt context. "
    "Be an agent with real agency, not a chatbot reciting a script."
)

# ── Phase Prompts ──

PHASE_PROMPTS = {
    "perceive": (
        "You are the PERCEIVE Module of Worldwave.\n"
        "Your task is to observe the current input and environment accurately.\n"
        "1. Identify key facts, emotions, and context from the user message and tools.\n"
        "2. Note any ambiguities or missing information.\n"
        "3. Output ONLY a valid JSON object. No explanation text before or after the JSON.\n"
        "Format: {\"observations\": [...], \"emotions\": [...], \"ambiguities\": [...], \"confidence\": 0-100}"
    ),
    "recall": (
        "You are the RECALL Module of Worldwave.\n"
        "Retrieve and summarize relevant memories from the temporal knowledge graph.\n"
        "1. Search for facts, patterns, and previous similar situations.\n"
        "2. Evaluate which memories are currently valid (check valid_from/valid_until).\n"
        "3. Output ONLY a valid JSON object. No explanation text before or after the JSON.\n"
        "Format: {\"recalled_memories\": [...], \"relevance_score\": 0.0-1.0, \"key_insights\": [...]}"
    ),
    "plan": (
        "You are the PLAN Module of Worldwave.\n"
        "Create a feasible, step-by-step action plan based on perception and recall.\n"
        "1. Think step by step: goal → constraints → possible actions → risks.\n"
        "2. Prefer using available tools when possible.\n"
        "3. If the task only needs a text response (no tools), use tool=\"respond\" with empty params.\n"
        "4. If you need user clarification, use tool=\"question\" with the question as content.\n"
        "5. Each step MUST designate a tool or action — never leave a step empty.\n"
        "6. CRITICAL: When asked about your own configuration, backend model, or environment,\n"
        "   plan shell_exec or file_read steps to INSPECT THE ACTUAL SYSTEM before any respond step.\n"
        "   NEVER plan a direct respond for self-inspection questions.\n"
        "7. After memory tools (recall_mine/remember/search), ALWAYS plan a final respond step\n"
        "   that answers in natural language. Never treat tool dumps as the user reply.\n"
        "8. ABSTENTION: if memory has no answer for the asked fact, respond must refuse in\n"
        "   natural language (do not invent; do not paste multi-line key: value dumps).\n"
        "   If facts conflict, acknowledge conflict and ask which is correct.\n"
        "9. Output ONLY a valid JSON object. No explanation text before or after the JSON.\n"
        "Format: {\"goal\": \"...\", \"strategy\": \"...\", \"steps\": [{\"tool\": \"...\", \"params\": {...}, \"description\": \"...\"}], \"success_criteria\": \"...\", \"max_attempts\": 3}"
    ),
    "act": (
        "You are the ACT Module of Worldwave.\n"
        "Execute the approved plan using tools or direct actions.\n"
        "1. Follow the plan steps precisely and in order.\n"
        "2. Call tools in the required JSON format when needed.\n"
        "3. Output ONLY a valid JSON object. No explanation text before or after the JSON.\n"
        "Format: {\"actions_taken\": [...], \"tool_calls\": [...], \"immediate_results\": [...]}"
    ),
    "evaluate": (
        "You are the EVALUATE Module of Worldwave.\n"
        "Assess the outcome of actions against the original goal.\n"
        "1. Compare results with expectations.\n"
        "2. Identify successes, failures, and unexpected side effects.\n"
        "3. Determine whether the goal is achieved or needs more work.\n"
        "4. Output ONLY a valid JSON object. No explanation text before or after the JSON.\n"
        "Format: {\"success\": true/false, \"reason\": \"...\", \"lessons_learned\": [...], \"goal_remaining\": true/false, \"next_action\": \"continue|stop|adjust\"}"
    ),
    "learn": (
        "You are the LEARN Module of Worldwave.\n"
        "Extract new skills, patterns, and knowledge from this cycle.\n"
        "1. Identify what worked well and what should be remembered or forgotten.\n"
        "2. Propose updates to the knowledge graph or skill library.\n"
        "3. Output ONLY a valid JSON object. No explanation text before or after the JSON.\n"
        "Format: {\"content\": \"...\", \"entities\": [...], \"emotion_tags\": [...], \"importance\": 0.0-1.0, \"abstract_pattern\": \"...\"}"
    ),
    "consolidate": (
        "You are the CONSOLIDATE Module of Worldwave.\n"
        "Finalize the cycle and prepare for the next iteration.\n"
        "1. Save important memories with proper timestamps and validity windows.\n"
        "2. Update entity state and long-term knowledge graph.\n"
        "3. Identify what should be the focus of the next cycle.\n"
        "4. Output ONLY a valid JSON object. No explanation text before or after the JSON.\n"
        "Format: {\"consolidated_memories\": [...], \"next_focus\": \"...\", \"state_updates\": {...}}"
    ),
}

# Per-phase temperature defaults (overrideable at call time)
# Higher = more creative/varied; lower = more deterministic/factual
PHASE_TEMPERATURES = {
    "perceive": 0.7,
    "recall": 0.3,
    "plan": 0.8,
    "act": 0.7,
    "evaluate": 0.5,
    "learn": 0.6,
    "consolidate": 0.2,
}


class LLMClient:
    """
    Worldwave multi LLM client v0.5

    use Transport abstraction layer, supports:
    - multi provider autoroute
    - Provider failover
    - Tool calling
    - Token usage trace
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str = "",
        api_base: str = "",
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        provider: str = "",
        failover: bool = False,
        transports: Optional[TransportRegistry] = None,
        reasoning_effort: str = "",
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.failover = failover
        self.reasoning_effort = reasoning_effort  # DeepSeek: low/medium/high/xhigh

        # Transport layer
        self._registry = transports or TransportRegistry()
        self._provider = provider or infer_provider(model)
        self._transport: ProviderTransport = self._registry.get(self._provider)

        # Override API key/base if provided
        self._override_key = api_key
        self._override_base = api_base

        # Token tracking
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0

        # Last response info
        self.last_response: str = ""
        self.last_provider: str = self._provider
        self.last_model: str = model
        self.last_usage: Dict = {}

    def switch_provider(self, provider: str):
        """Switch provider"""
        transport = self._registry.get(provider)
        if transport:
            self._provider = provider
            self._transport = transport

    def _resolve_api_key(self) -> str:
        if self._override_key:
            return self._override_key
        if self._transport:
            return self._transport.get_api_key()
        return ""

    def _resolve_api_base(self) -> str:
        if self._override_base:
            return self._override_base
        if self._transport:
            return self._transport.get_base_url()
        return "https://api.deepseek.com/v1"

    def chat(
        self,
        messages: List[Dict[str, str]],
        phase: str = "",
        json_mode: bool = True,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        stream: bool = False,
        **kwargs,
    ) -> str:
        """
        Call LLM, return text content.

        Args:
            messages: OpenAI-format messagelist
            phase: Spiral phase (auto inject system prompt)
            json_mode: Whether to force JSON output
            temperature: Temperature (override)
            max_tokens: Max tokens (override)
            model: Model (override)
            tools: OpenAI-format tool definition
            stream: Whether to stream

        Returns:
            responsetextcontent
        """
        resp = self._call(
            messages=messages,
            phase=phase,
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            tools=tools,
            stream=stream,
            **kwargs,
        )
        return resp.content

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        phase: str = "",
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Call LLM and auto-resolve JSON response.

        Returns:
            JSON dict, resolvefailed contains  {"raw": ..., "parse_error": True}
        """
        resp = self._call(
            messages=messages,
            phase=phase,
            json_mode=True,
            temperature=temperature,
            model=model,
            tools=tools,
            **kwargs,
        )
        try:
            return json.loads(resp.content)
        except (json.JSONDecodeError, TypeError):
            return {"raw": resp.content, "parse_error": True,
                    "provider": resp.provider, "model": resp.model}

    def chat_with_tools(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict],
        phase: str = "",
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        **kwargs,
    ) -> NormalizedResponse:
        """
        Call LLM and retain tool_calls info.

        Returns:
            NormalizedResponse (with content + tool_calls)
        """
        return self._call(
            messages=messages,
            phase=phase,
            json_mode=False,
            temperature=temperature,
            model=model,
            tools=tools,
            **kwargs,
        )

    def _call(
        self,
        messages: List[Dict[str, str]],
        phase: str = "",
        json_mode: bool = True,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        stream: bool = False,
        **kwargs,
    ) -> NormalizedResponse:
        """Core call method — via Transport layer process"""
        active_model = model or self.model
        temp = temperature if temperature is not None else PHASE_TEMPERATURES.get(phase, self.temperature)
        mt = max_tokens if max_tokens is not None else self.max_tokens

        # Inject phase system prompt
        msgs = self._inject_phase_prompt(messages, phase)
        if phase:
            logger.debug("Phase=%s temperature=%.2f model=%s", phase, temp, active_model)

        # Build OpenAI-format tools
        openai_tools = None
        if tools:
            openai_tools = []
            for t in tools:
                if isinstance(t, dict):
                    openai_tools.append(t)

        # Inject reasoning_effort if set and not already provided
        if self.reasoning_effort and "reasoning_effort" not in kwargs:
            kwargs["reasoning_effort"] = self.reasoning_effort

        # Try providers in failover chain
        providers_to_try = self._build_provider_chain()

        last_error = ""
        for prov_name in providers_to_try:
            transport = self._registry.get(prov_name)
            if not transport:
                continue

            api_key = self._override_key or transport.get_api_key()
            if not api_key:
                continue

            try:
                resp = transport.chat(
                    model=active_model,
                    messages=msgs,
                    tools=openai_tools,
                    temperature=temp,
                    max_tokens=mt,
                    json_mode=json_mode,
                    stream=stream,
                    **kwargs,
                )

                # Track usage
                self.last_provider = resp.provider
                self.last_model = resp.model
                self.last_response = resp.content
                self.last_usage = resp.usage
                self.total_input_tokens += resp.usage.get("input_tokens", 0)
                self.total_output_tokens += resp.usage.get("output_tokens", 0)

                return resp

            except Exception as e:
                last_error = f"{prov_name}: {e}"
                continue

        raise RuntimeError(
            f"All provider calls failed (chain={providers_to_try}): {last_error}"
        )

    def _inject_phase_prompt(self, messages: List[Dict], phase: str) -> List[Dict]:
        """Inject spiral phase system prompt + AGENTS.md + coding-mode essence"""
        msgs = list(messages)
        
        # Build system prompt — identity + runtime context + phase instruction + project context
        system_parts = []
        system_parts.append(WW_IDENTITY)
        if phase and phase in PHASE_PROMPTS:
            system_parts.append(PHASE_PROMPTS[phase])
        
        # Load AGENTS.md project context (matching Claude Code/Codex behavior)
        try:
            from core.prompts import _load_agents_md
            agents_md = _load_agents_md()
            if agents_md:
                system_parts.append(agents_md)
        except Exception:
            pass

        # Coding mode auto: CODING_AGENT essence + role=coder when goal looks like coding
        try:
            from coding.mode import build_coding_context, is_coding_goal
            goal = ""
            for m in reversed(msgs):
                if m.get("role") == "user":
                    goal = str(m.get("content") or "")
                    break
            if is_coding_goal(goal):
                ctx = build_coding_context(goal=goal, force=False)
                if ctx.get("system_block"):
                    system_parts.append(ctx["system_block"])
        except Exception:
            pass
        
        if not system_parts:
            return msgs
        
        system_content = "\n\n".join(system_parts)
        has_system = any(m.get("role") == "system" for m in msgs)

        if not has_system:
            msgs.insert(0, {"role": "system", "content": system_content})
        else:
            for i, m in enumerate(msgs):
                if m.get("role") == "system":
                    # Prepend project context to existing system message
                    msgs[i] = {"role": "system", "content": system_content + "\n\n" + m["content"]}
                    break

        return msgs

    def _build_provider_chain(self) -> List[str]:
        """Create provider attempt order — only providers with API keys"""
        if self.failover:
            return self._registry.failover_chain(self._provider)
        return [self._provider]

    def summarize(self, text: str, max_len: int = 200) -> str:
        """Quick summary (keep backward compat)"""
        if len(text) <= max_len:
            return text
        return self.chat(
            messages=[
                {"role": "user", "content": f"Summarize in one sentence (within {max_len} characters):\n\n{text}"}
            ],
            phase="",
            json_mode=False,
            temperature=0.3,
            max_tokens=100,
        )

    def available_providers(self) -> List[str]:
        """List providers with API key available"""
        return find_available_providers(self._registry._transports)

    def usage_stats(self) -> Dict:
        """Usage statistics"""
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "last_provider": self.last_provider,
            "last_model": self.last_model,
            "available_providers": self.available_providers(),
        }

    def chat_stream(self, messages: List[Dict[str, str]],
                    phase: str = "",
                    temperature: Optional[float] = None,
                    max_tokens: Optional[int] = None,
                    model: Optional[str] = None,
                    tools: Optional[List[Dict]] = None,
                    **kwargs):
        """Stream LLM response token-by-token.

        Yields (delta_text, finish_reason) tuples.
        Each yield is a small text chunk; finish_reason is None until done.

        Usage:
            for chunk, finish in llm.chat_stream(messages, phase="act"):
                if chunk:
                    yield f"data: {json.dumps({'token': chunk})}\\n\\n"
                if finish:
                    yield f"data: {json.dumps({'finish': finish})}\\n\\n"
        """
        active_model = model or self.model
        temp = temperature if temperature is not None else PHASE_TEMPERATURES.get(phase, self.temperature)
        mt = max_tokens if max_tokens is not None else self.max_tokens

        msgs = self._inject_phase_prompt(messages, phase)
        openai_tools = None
        if tools:
            openai_tools = list(tools) if isinstance(tools, list) else None

        providers = self._build_provider_chain()
        for prov_name in providers:
            transport = self._registry.get(prov_name)
            if not transport:
                continue
            api_key = self._override_key or transport.get_api_key()
            if not api_key:
                continue

            if not hasattr(transport, 'chat_stream_iter'):
                continue

            try:
                for chunk, finish in transport.chat_stream_iter(
                    model=active_model,
                    messages=msgs,
                    tools=openai_tools,
                    temperature=temp,
                    max_tokens=mt,
                    **kwargs,
                ):
                    yield (chunk, finish)
                return
            except Exception:
                continue

        raise RuntimeError("No streaming provider available")


def create_llm(config: Optional[Dict[str, Any]] = None) -> LLMClient:
    """Quick create LLM client (keep backward compat)"""
    conf = config or {}
    return LLMClient(
        model=conf.get("model", DEFAULT_MODEL),
        api_key=conf.get("api_key", ""),
        api_base=conf.get("api_base", ""),
        temperature=conf.get("temperature", DEFAULT_TEMPERATURE),
        max_tokens=conf.get("max_tokens", DEFAULT_MAX_TOKENS),
        provider=conf.get("provider", ""),
        failover=conf.get("failover", False),
    )
