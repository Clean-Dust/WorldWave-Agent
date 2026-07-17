"""coding/model_route.py — Resolve preferred model for coding mode.

Env / config:
  WW_CODING_MODEL      — preferred model id (e.g. deepseek-v4-flash, claude-…)
  WW_CODING_PROVIDER   — optional provider override
  config.coding_model / coding_provider when a dict is passed

Fallback: main agent model / DEFAULT_MODEL when coding model unset or invalid.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("ww.coding.model_route")

# Soft default when no coding-specific or main model is set
_FALLBACK_MODEL = os.environ.get("WW_MODEL", "deepseek-v4-flash")


def _infer_provider(model: str) -> str:
    try:
        from core.transports.registry import infer_provider
        return infer_provider(model) or ""
    except Exception:
        # Lightweight local heuristic (no hard deps)
        m = (model or "").lower()
        if "claude" in m or "anthropic" in m:
            return "anthropic"
        if "gpt" in m or "o1" in m or "o3" in m or "o4" in m:
            return "openai"
        if "deepseek" in m:
            return "deepseek"
        if "gemini" in m:
            return "google"
        if m.startswith("custom/"):
            return "custom"
        return ""


def resolve_coding_model(
    config: Optional[Dict[str, Any]] = None,
    main_model: str = None,
    main_provider: str = None,
    prefer_coding: bool = True,
) -> Dict[str, Any]:
    """Resolve which model coding mode should use.

    Returns:
      {
        model: str,
        provider: str,
        source: "WW_CODING_MODEL" | "config" | "main" | "fallback",
        fallback: bool,          # True when coding-specific route was not used
        coding_preferred: bool,  # True when a coding-specific model was selected
        log: str,                # short log line
      }
    """
    config = config or {}
    env_model = (os.environ.get("WW_CODING_MODEL") or "").strip()
    env_provider = (os.environ.get("WW_CODING_PROVIDER") or "").strip()
    cfg_model = str(config.get("coding_model") or config.get("WW_CODING_MODEL") or "").strip()
    cfg_provider = str(
        config.get("coding_provider") or config.get("WW_CODING_PROVIDER") or ""
    ).strip()

    chosen_model = ""
    chosen_provider = ""
    source = "fallback"
    coding_preferred = False

    if prefer_coding and env_model:
        chosen_model = env_model
        chosen_provider = env_provider
        source = "WW_CODING_MODEL"
        coding_preferred = True
    elif prefer_coding and cfg_model:
        chosen_model = cfg_model
        chosen_provider = cfg_provider
        source = "config"
        coding_preferred = True
    elif main_model:
        chosen_model = str(main_model).strip()
        chosen_provider = (main_provider or "").strip()
        source = "main"
    else:
        chosen_model = _FALLBACK_MODEL
        source = "fallback"

    if not chosen_model:
        chosen_model = _FALLBACK_MODEL
        source = "fallback"
        coding_preferred = False

    if not chosen_provider:
        chosen_provider = _infer_provider(chosen_model)

    fallback = not coding_preferred
    log_line = (
        f"coding_model_route model={chosen_model} provider={chosen_provider or '-'} "
        f"source={source} fallback={fallback}"
    )
    if coding_preferred:
        logger.info(log_line)
    else:
        logger.debug(log_line)

    return {
        "model": chosen_model,
        "provider": chosen_provider,
        "source": source,
        "fallback": fallback,
        "coding_preferred": coding_preferred,
        "log": log_line,
    }


def apply_coding_model_to_client(client: Any, route: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
    """Best-effort apply resolved coding model onto an LLMClient-like object.

    Does not raise on missing attributes. Returns the route used.
    """
    route = route or resolve_coding_model(**kwargs)
    model = route.get("model")
    provider = route.get("provider") or ""
    if client is None:
        return route
    try:
        if model and hasattr(client, "model"):
            client.model = model
        if provider and hasattr(client, "switch_provider"):
            try:
                client.switch_provider(provider)
            except Exception:
                # Provider may be unavailable offline — keep model preference
                pass
        elif provider and hasattr(client, "_provider"):
            client._provider = provider
        if hasattr(client, "last_model") and model:
            client.last_model = model
    except Exception as e:
        logger.debug("apply_coding_model_to_client: %s", e)
        route = dict(route)
        route["apply_error"] = str(e)
    return route
