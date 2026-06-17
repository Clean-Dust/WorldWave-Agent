"""core/computer_use/vision.py — Screen visual understanding + action decision engine

Two vision modes, auto-selected based on main LLM capability:

Mode A — Main LLM is multimodal (GPT-4o, Claude 3.5/4, Gemini):
  Screenshot → main LLM directly → decide next action
  (No external vision API needed)

Mode B — Main LLM doesn't support vision (DeepSeek, etc.):
  Screenshot → external vision API (Qwen2.5-VL) → decide next action
  (Original behavior)

Tier support (both modes):
  Tier 1-2: Direct coordinate guessing
  Tier 3+:  Set-of-Mark numbered elements (95%+ accuracy)
  Tier 4+:  Post-action pixel diff verification
  Tier 5+:  Spatio-temporal memory (stub)
"""

from __future__ import annotations
import base64
import json
import os
import time
import urllib.request

from core.computer_use.config import get_config


# ── Known vision-supporting models ───────────────────────────────

VISION_MODELS = {
    # OpenAI
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4-vision",
    "o1", "o1-preview", "o1-mini",
    # Anthropic
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku",
    "claude-3.5-sonnet", "claude-3.5-haiku",
    "claude-sonnet-4", "claude-opus-4",
    # OpenRouter (full paths)
    "openai/gpt-4o", "openai/gpt-4o-mini", "openai/o1",
    "anthropic/claude-3.5-sonnet", "anthropic/claude-3.5-haiku",
    "anthropic/claude-sonnet-4", "anthropic/claude-opus-4",
    "google/gemini-2.0-flash", "google/gemini-2.0-flash-lite",
    "google/gemini-2.5-pro", "google/gemini-2.5-flash",
    "qwen/qwen2.5-vl-72b-instruct", "qwen/qwen-vl-plus",
    # Gemini short names
    "gemini-2.0-flash", "gemini-2.5-pro", "gemini-2.5-flash",
}


def supports_vision(model: str) -> bool:
    """Check if a model name supports image input.

    Fast path: exact match against known list.
    Fallback: heuristic — 'vl-' or '-vl-' or 'vision' in name.
    """
    model_lower = model.lower().strip()
    if model_lower in VISION_MODELS:
        return True
    # Heuristic: models with 'vl-' or 'vision' in name are vision models
    if "-vl-" in model_lower or model_lower.startswith("vl-"):
        return True
    if "vision" in model_lower:
        return True
    return False


# ── Config / env ─────────────────────────────────────────────────

VISION_API_KEY = os.environ.get("WW_VISION_API_KEY", "")
VISION_BASE_URL = "https://openrouter.ai/api/v1"

MAX_STEPS = 20

# ── System prompts ───────────────────────────────────────────────

# Tier 1-2: Direct coordinate mode
SYSTEM_PROMPT_DIRECT = """You are a computer operation AI. Your task is to understand what's on screen and decide the next action to accomplish a given task.

You have access to these tools:
- mouse_move(x, y) — Move cursor to coordinates
- mouse_click(x, y, button="left") — Click at position (default left)
- mouse_doubleclick(x, y) — Double click
- mouse_drag(x1, y1, x2, y2) — Drag from (x1,y1) to (x2,y2)
- scroll(direction="down", amount=3) — Scroll wheel
- key_type(text) — Type text at focused element
- key_press(keys) — Press keyboard shortcut (e.g. ["ctrl","c"])
- done(summary) — Task complete, provide summary of what was done

CRITICAL COORDINATE RULES:
1. Coordinates are absolute pixel positions on screen (1920x1080)
2. Guess coordinates based on what you see. If you see a button at the top-left of a section, estimate its position
3. When you're off-target, observe the new screenshot and adjust
4. For text fields: click first, then type

RESPONSE FORMAT (JSON only):
{
  "analysis": "...",
  "reasoning": "...",
  "action": {
    "tool": "mouse_move|mouse_click|mouse_doubleclick|mouse_drag|scroll|key_type|key_press|done",
    "params": { ... }
  },
  "done": false,
  "summary": ""
}"""

# Tier 3+: Set-of-Mark mode (numbered elements on screenshot)
SYSTEM_PROMPT_SOM = """You are a computer operation AI. Your task is to understand what's on screen and decide the next action.

INTERACTIVE ELEMENTS are numbered on the screenshot with green bounding boxes and white numbers like [1], [2], [3] etc.
Use these numbered targets instead of guessing coordinates.

Available tools:
- click(target_id) — Click the numbered element on screen
- doubleclick(target_id) — Double-click the numbered element
- rightclick(target_id) — Right-click the numbered element
- type(target_id, text) — Click element then type text into it
- scroll(direction="down", amount=3) — Scroll wheel
- key_press(keys) — Keyboard shortcut (e.g. ["ctrl","c"])
- done(summary) — Task complete

RESPONSE FORMAT (JSON only):
{
  "analysis": "Brief description of what's on screen",
  "reasoning": "Why this action is the right next step",
  "action": {
    "tool": "click|doubleclick|rightclick|type|scroll|key_press|done",
    "params": { ... }
  },
  "done": false,
  "summary": ""
}

IMPORTANT:
- Always use target_id when clicking — do NOT guess pixel coordinates
- If you need to click something that isn't numbered, describe what you need
- For type: first click the target, then type. The "type" tool handles both steps."""  # noqa: E501


# ── Vision API call (external, for non-vision main LLMs) ────────

def _call_external_vision(messages: list, temperature: float = 0.1) -> dict:
    """Call external vision API (OpenRouter Qwen2.5-VL)."""
    cfg = get_config()
    model = cfg.vision_model

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 1024,
    }).encode()

    req = urllib.request.Request(
        f"{VISION_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {VISION_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://worldwave.ai",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            content = result["choices"][0]["message"]["content"]
            # Remove markdown JSON wrapper
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3].strip()
            return json.loads(content)
    except json.JSONDecodeError:
        return {"analysis": content, "action": None, "done": False, "reasoning": "parse_failed"}
    except Exception as e:
        return {"analysis": f"API error: {e}", "action": None, "done": False, "reasoning": str(e)}


# ── Vision via main LLM (for multimodal main LLMs) ──────────────

def _call_main_llm_for_vision(
    task: str,
    screenshot_path: str,
    history: list[str] | None = None,
    elements: list[dict] | None = None,
) -> dict:
    """Use the main LLM as the vision engine.

    Creates a temporary LLM client matching the current config,
    sends screenshot + context, and asks for the next action.
    """
    if history is None:
        history = []

    cfg = get_config()
    img_url = _img_to_data_url(screenshot_path)

    # Choose prompt based on tier
    if cfg.use_som:
        system_prompt = SYSTEM_PROMPT_SOM
        context_extra = ""
        if elements:
            lookup = {str(e["id"]): f'{e.get("label","")} ({e.get("type","")}) at ({e.get("x",0)},{e.get("y",0)})'
                      for e in elements if e.get("id")}
            context_extra = "\n\nElement reference:\n" + "\n".join(
                f"  [{kid}] {desc}" for kid, desc in lookup.items()
            )
    else:
        system_prompt = SYSTEM_PROMPT_DIRECT
        context_extra = ""

    action_history = "\n".join(history[-10:]) if history else "No actions yet."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {
                "type": "text",
                "text": (
                    f"Task: {task}\n\n"
                    f"Actions taken so far:\n{action_history}\n"
                    f"{context_extra}\n\n"
                    "Look at the screenshot and decide the next action. "
                    "Respond with JSON only."
                ),
            },
            {"type": "image_url", "image_url": {"url": img_url}},
        ]},
    ]

    try:
        from core.llm import create_llm

        # Build a config matching the current main LLM
        model = cfg.main_llm_model or os.environ.get("WW_MAIN_MODEL", "deepseek/deepseek-v4-flash")
        provider = os.environ.get("WW_MAIN_PROVIDER", "")

        llm = create_llm({
            "model": model,
            "provider": provider,
            "temperature": 0.1,
            "max_tokens": 1024,
        })

        content = ""
        content = llm.chat(
            messages=messages,
            json_mode=True,
            temperature=0.1,
            max_tokens=1024,
        )

        # Parse JSON from response
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3].strip()
        return json.loads(content)
    except json.JSONDecodeError:
        return {"analysis": content, "action": None, "done": False, "reasoning": "parse_failed"}
    except Exception as e:
        return {"analysis": f"Main LLM vision error: {e}", "action": None, "done": False, "reasoning": content}


# ── Route: choose vision engine ──────────────────────────────────

def _call_vision(messages: list, temperature: float = 0.1) -> dict:
    """Route to the appropriate vision engine.

    Called by plan_next_action with pre-built messages.
    When using main LLM mode, plan_next_action calls _call_main_llm_for_vision directly
    instead of going through this function.
    """
    return _call_external_vision(messages, temperature)


def _img_to_data_url(img_path: str) -> str:
    """Convert image file to data URL for vision API."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:image/png;base64,{b64}"


# ── Action planning ──────────────────────────────────────────────

def plan_next_action(
    task: str,
    screenshot_path: str,
    history: list[str] = None,
    elements: list[dict] = None,
    use_main_llm: bool = False,
) -> dict:
    """Analyze screen and decide next action.

    Args:
        task: User's task description
        screenshot_path: Path to screenshot (annotated with SoM if Tier 3+)
        history: Actions taken so far
        elements: UI elements (for lookup table, Tier 3+)
        use_main_llm: If True, use the main LLM as vision engine

    Returns:
        {"analysis": ..., "action": {"tool": ..., "params": {...}}, "done": bool, "summary": str}
    """
    if history is None:
        history = []

    # Use main LLM as vision engine
    if use_main_llm:
        return _call_main_llm_for_vision(task, screenshot_path, history, elements)

    # Legacy: external vision API
    cfg = get_config()
    img_url = _img_to_data_url(screenshot_path)

    # Choose prompt based on tier
    if cfg.use_som:
        system_prompt = SYSTEM_PROMPT_SOM
        context_extra = ""
        if elements:
            lookup = {str(e["id"]): f'{e.get("label","")} ({e.get("type","")}) at ({e.get("x",0)},{e.get("y",0)})'
                      for e in elements if e.get("id")}
            context_extra = "\n\nElement reference:\n" + "\n".join(
                f"  [{kid}] {desc}" for kid, desc in lookup.items()
            )
    else:
        system_prompt = SYSTEM_PROMPT_DIRECT
        context_extra = ""

    action_history = "\n".join(history[-10:]) if history else "No actions yet."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {
                "type": "text",
                "text": (
                    f"Task: {task}\n\n"
                    f"Actions taken so far:\n{action_history}\n"
                    f"{context_extra}\n\n"
                    "Look at the screenshot and decide the next action."
                ),
            },
            {"type": "image_url", "image_url": {"url": img_url}},
        ]},
    ]

    result = _call_vision(messages)

    # Ensure complete structure
    for key in ("action", "done", "summary", "analysis"):
        if key not in result:
            result[key] = "" if key in ("summary", "analysis") else (None if key == "action" else False)

    return result


# ── Action execution ─────────────────────────────────────────────

def execute_action(action: dict, cu) -> str:
    """Execute the action decided by the vision model.

    Supports both direct-coordinate mode (Tier 1-2) and
    Set-of-Mark target_id mode (Tier 3+).
    """
    if action is None:
        return "[no action from vision]"

    tool = action.get("tool")
    params = action.get("params", {})
    if params is None:
        params = {}

    # ── Set-of-Mark mode (target_id based) ──
    target_id = params.get("target_id", params.get("target", params.get("id")))

    if tool == "click" and target_id is not None:
        # Use lookup table — click_element will resolve coordinates
        from core.computer_use.elements import click_by_id
        result = click_by_id(target_id, cu)
        return f"click([{target_id}]) -> {result.get('msg', 'ok')}"

    if tool == "doubleclick" and target_id is not None:
        from core.computer_use.elements import click_by_id
        result = click_by_id(target_id, cu)
        cu.mouse_click()  # Second click for double
        return f"doubleclick([{target_id}]) -> {result.get('msg', 'ok')}"

    if tool == "rightclick" and target_id is not None:
        from core.computer_use.elements import click_by_id
        result = click_by_id(target_id, cu, button="right")
        return f"rightclick([{target_id}]) -> {result.get('msg', 'ok')}"

    if tool == "type" and target_id is not None:
        text = params.get("text", params.get("content", ""))
        from core.computer_use.elements import click_by_id
        click_by_id(target_id, cu)
        time.sleep(0.1)
        cu.key_type(text)
        return f"type([{target_id}], '{text[:30]}')"

    # ── Direct coordinate mode (fallback, Tier 1-2) ──
    if tool == "mouse_move":
        x = params.get("x", params.get("X", 0))
        y = params.get("y", params.get("Y", 0))
        cu.mouse_move(x, y)
        return f"mouse_move({x}, {y})"

    elif tool == "mouse_click":
        x = params.get("x", params.get("X"))
        y = params.get("y", params.get("Y"))
        btn = params.get("button", params.get("btn", "left"))
        cu.mouse_click(x, y, btn)
        loc = f" at ({x},{y})" if x else ""
        return f"mouse_click({btn}{loc})"

    elif tool == "mouse_doubleclick":
        x = params.get("x", params.get("X"))
        y = params.get("y", params.get("Y"))
        cu.mouse_doubleclick(x, y)
        loc = f" at ({x},{y})" if x else ""
        return f"mouse_doubleclick{loc}"

    elif tool == "mouse_drag":
        cu.mouse_drag(params.get("x1", 0), params.get("y1", 0),
                      params.get("x2", 0), params.get("y2", 0))
        return f"mouse_drag({params.get('x1')},{params.get('y1')}->{params.get('x2')},{params.get('y2')})"

    elif tool in ("scroll", "mouse_scroll"):
        cu.scroll(params.get("direction", "down"), params.get("amount", 3))
        return f"scroll({params.get('direction', 'down')})"

    elif tool == "key_type":
        text = params.get("text", params.get("content", ""))
        cu.key_type(text)
        preview = text[:30]
        return f"key_type('{preview}')"

    elif tool == "key_press":
        keys = params.get("keys", params.get("key", params.get("combo", ["enter"])))
        if isinstance(keys, str):
            keys = [keys]
        cu.key_press(keys)
        return f"key_press({'+'.join(keys)})"

    else:
        return f"[unknown tool: {tool}]"


# ── High-level vision closed loop ───────────────────────────────

def do_task(task: str, cu=None, max_steps: int = None) -> dict:
    """Execute visual closed-loop task: see -> think -> do -> see.

    Auto-selects vision engine based on main LLM capability:
    - If main LLM is multimodal (GPT-4o, Claude 3.5/4): uses main LLM directly
    - If main LLM is text-only (DeepSeek, etc.): uses external vision API

    Tier behavior is preserved regardless of vision engine choice.

    Args:
        task: Task description
        cu: ComputerUse instance (auto-get if None)
        max_steps: Max steps (default: from config)

    Returns:
        {"success": bool, "summary": str, "steps": int, "actions_taken": [...]}
    """
    from core.computer_use import get as _get_cu
    if cu is None:
        cu = _get_cu()

    cfg = get_config()
    if max_steps is None:
        max_steps = cfg.vision_max_steps

    # ── Auto-detect vision engine ──
    use_main_llm = _resolve_vision_mode(cfg)
    if use_main_llm:
        engine = f"main LLM ({cfg.main_llm_model})"
    else:
        engine = f"external ({cfg.vision_model})"

    # Ensure screen size cached
    try:
        cu.screen_size()
    except Exception:
        pass

    history = []
    all_actions = []
    before_screenshot = None

    for step in range(1, max_steps + 1):
        # ── 1. Capture ──
        from core.computer_use.capture import screenshot as cap_screenshot
        raw_path = f"/tmp/cu_raw_{int(time.time() * 1000)}.png"
        cap_screenshot(raw_path)

        # ── 2. Extract UI elements (Tier 2+) ──
        elements = []
        som_path = raw_path  # May be replaced by annotated version
        if cfg.use_uia:
            from core.computer_use.uia import get_interactive_elements
            elements = get_interactive_elements()
            # Cache on cu for click_by_id lookup
            if hasattr(cu, '_last_uia_elements'):
                cu._last_uia_elements = elements

            # ── 3. SoM annotation (Tier 3+) ──
            if cfg.use_som and elements:
                from core.computer_use.som import annotate
                som_path = f"/tmp/cu_som_{int(time.time() * 1000)}.png"
                annotate(raw_path, elements, som_path)

        # ── 4. Decide next action ──
        decision = plan_next_action(task, som_path, history, elements,
                                    use_main_llm=use_main_llm)
        analysis = decision.get("analysis", "")
        action = decision.get("action")

        # ── 5. Check done ──
        if decision.get("done", False):
            summary = decision.get("summary", "Task completed")
            return {
                "success": True,
                "summary": summary,
                "analysis": analysis,
                "steps": step,
                "actions_taken": all_actions,
                "vision_engine": engine,
            }

        if action is None:
            return {
                "success": False,
                "summary": f"Vision model returned no action at step {step}. Analysis: {analysis}",
                "steps": step,
                "actions_taken": all_actions,
                "vision_engine": engine,
            }

        # ── 6. Execute ──
        before_screenshot = raw_path
        action_desc = execute_action(action, cu)
        history.append(action_desc)
        all_actions.append({
            "step": step,
            "tool": action.get("tool"),
            "params": action.get("params"),
            "analysis": analysis,
        })
        time.sleep(0.3)

        # ── 7. Visual verification (Tier 4+) ──
        if cfg.verify_action and before_screenshot:
            time.sleep(cfg.verify_timeout)
            after_path = f"/tmp/cu_verify_{int(time.time() * 1000)}.png"
            cap_screenshot(after_path)
            from core.computer_use.capture import delta_pixels
            delta = delta_pixels(before_screenshot, after_path)
            if delta < cfg.verify_change_threshold:
                # Screen didn't change — action might have failed
                retry = all_actions[-1]
                history.append(f"[WARN] No visual change detected (delta={delta:.4f}) — retrying")
                # Retry the same action
                action_desc = execute_action(retry, cu)
                history.append(f"[RETRY] {action_desc}")
                time.sleep(0.3)
                # Check again after retry
                after_path2 = f"/tmp/cu_verify2_{int(time.time() * 1000)}.png"
                cap_screenshot(after_path2)
                delta2 = delta_pixels(before_screenshot, after_path2)
                if delta2 < cfg.verify_change_threshold:
                    history.append(f"[FAIL] Still no change after retry (delta={delta2:.4f})")
                    return {
                        "success": False,
                        "summary": f"Action failed verification at step {step}: no visual change after retry",
                        "steps": step,
                        "actions_taken": all_actions,
                        "vision_engine": engine,
                    }

    # Timeout
    return {
        "success": False,
        "summary": f"Reached max {max_steps} steps without completion",
        "steps": max_steps,
        "actions_taken": all_actions,
        "vision_engine": engine,
    }


def _resolve_vision_mode(cfg) -> bool:
    """Determine whether to use main LLM as vision engine.

    Priority:
      1. cfg.use_main_llm_vision explicitly set (True/False)
      2. Auto-detect: check if cfg.main_llm_model supports vision
      3. Fallback: False (external vision API)
    """
    if cfg.use_main_llm_vision is True:
        return True
    if cfg.use_main_llm_vision is False:
        return False

    # Auto-detect: read model from env if not in config
    model = cfg.main_llm_model or os.environ.get("WW_MAIN_MODEL", "")
    if not model:
        # Try to read from config.json
        try:
            config_path = os.path.join(
                os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww")),
                "config.json",
            )
            if os.path.exists(config_path):
                with open(config_path) as f:
                    cfg_data = json.load(f)
                model = cfg_data.get("model", "")
        except Exception:
            pass

    if model and supports_vision(model):
        return True
    return False
