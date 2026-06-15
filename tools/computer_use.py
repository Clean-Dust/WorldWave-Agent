"""ww/tools/computer_use.py — Computer Use tool

Benchmarked against Claude Code / Codex Computer Use feature.
Allows WW agent to see and control the Windows desktop.

Dependencies:
  - Windows Agent (computer_use.ps1) running on Windows side
  - core/computer_use module
"""

from __future__ import annotations
import json
import os
import time
from typing import Optional

from tools.registry import ToolRegistry, ToolDef


# ── toolprocess function ──────────────────────────────────

def _get_cu():
    from core.computer_use import get as _cu_get
    return _cu_get()

def _available() -> bool:
    from core.computer_use import check_available as _chk
    return _chk()

def _screenshot_handler(target_path: Optional[str] = None, **kwargs) -> str:
    """Capture Windows screenshot."""
    if not _available():
        return json.dumps({"error": "Computer Use requires WSL + Windows (PowerShell not found)"})
    try:
        cu = _get_cu()
        path = cu.screenshot(target_path)
        size = cu.screen_size()
        result = {
            "result": f"Screenshot saved to {path}",
            "image_path": path,
            "screen_width": size[0],
            "screen_height": size[1],
            "note": "Use this screenshot with vision analysis to understand what's on screen"
        }
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"Screenshot failed: {e}"})


def _mouse_move_handler(x: int, y: int, **kwargs) -> str:
    """Move mouse to specified screen coordinates."""
    if not _available():
        return json.dumps({"error": "Computer Use not available (not on WSL/Windows)"})
    try:
        cu = _get_cu()
        cu.mouse_move(x, y)
        return json.dumps({"result": f"Mouse moved to ({x}, {y})", "x": x, "y": y})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _mouse_click_handler(x: Optional[int] = None, y: Optional[int] = None,
                          button: str = "left", **kwargs) -> str:
    """Click mouse. Can specify position and button (left/right/middle)."""
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        cu.mouse_click(x, y, button)
        loc = f" at ({x}, {y})" if x is not None else ""
        return json.dumps({"result": f"{button} click{loc}", "x": x, "y": y, "button": button})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _mouse_doubleclick_handler(x: Optional[int] = None, y: Optional[int] = None, **kwargs) -> str:
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        cu.mouse_doubleclick(x, y)
        loc = f" at ({x}, {y})" if x is not None else ""
        return json.dumps({"result": f"Double click{loc}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _mouse_drag_handler(x1: int, y1: int, x2: int, y2: int, **kwargs) -> str:
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        cu.mouse_drag(x1, y1, x2, y2)
        return json.dumps({"result": f"Dragged from ({x1},{y1}) to ({x2},{y2})"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _key_type_handler(text: str, **kwargs) -> str:
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        cu.key_type(text)
        preview = text[:50] + ("..." if len(text) > 50 else "")
        return json.dumps({"result": f"Typed {len(text)} chars", "preview": preview})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _key_press_handler(keys: list, **kwargs) -> str:
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        cu.key_press(keys)
        return json.dumps({"result": f"Pressed: {'+'.join(keys)}", "keys": keys})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _scroll_handler(direction: str = "down", amount: int = 3, **kwargs) -> str:
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        cu.scroll(direction, amount)
        return json.dumps({"result": f"Scrolled {direction} {amount} clicks"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _screen_size_handler(**kwargs) -> str:
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        w, h = cu.screen_size()
        return json.dumps({"result": f"Screen: {w}x{h}", "width": w, "height": h})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _mouse_position_handler(**kwargs) -> str:
    if not _available():
        return json.dumps({"error": "Computer Use not available"})
    try:
        cu = _get_cu()
        x, y = cu.mouse_position()
        return json.dumps({"result": f"Mouse at ({x}, {y})", "x": x, "y": y})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _cu_analyze_handler(image_path: str = None, question: str = None, **kwargs) -> str:
    """Analyze screenshot content (requires vision model)."""
    from core.llm import quick_ask
    
    path = image_path or "/tmp/computer_use_screen.png"
    if not os.path.exists(path):
        return json.dumps({"error": f"Screenshot not found at {path}. Take one first."})
    
    q = question or "Describe what you see on this screen in detail. What elements, buttons, text fields, and controls are visible?"
    
    try:
        # read image and analyze via vision model
        with open(path, "rb") as f:
            import base64
            img_b64 = base64.b64encode(f.read()).decode()
        
        # use WW's vision capability
        from core.llm import LimitedContextSession
        session = LimitedContextSession()
        prompt = f"""You are a screen analysis AI. Look at this screenshot and {q}
        
Return your analysis as JSON with these fields:
- summary: one-line description of what's on screen
- elements: list of visible interactive elements with their approximate positions
- text: all visible text
- actions: suggested next actions"""
        
        result = session.ask_with_vision(prompt, img_b64)
        return json.dumps({"result": result, "image_path": path})
    except Exception as e:
        return json.dumps({"error": f"Analysis failed: {e}"})


# ── toolregister ──────────────────────────────────────

def register_tools(registry: ToolRegistry):
    """registerall  Computer Use tool. """

    registry.register(ToolDef(
        name="cu_screenshot",
        description="Take a screenshot of the Windows desktop. Returns the image path. Use this to see what's on screen before any action.",
        handler=_screenshot_handler,
        parameters={
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "description": "Optional path to save screenshot", "default": None},
            },
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_mouse_move",
        description="Move mouse cursor to absolute screen coordinates. Use after cu_screenshot + vision analysis to know where to click.",
        handler=_mouse_move_handler,
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate (0 = left edge)"},
                "y": {"type": "integer", "description": "Y coordinate (0 = top edge)"},
            },
            "required": ["x", "y"],
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_mouse_click",
        description="Click mouse at current position or at specified coordinates.",
        handler=_mouse_click_handler,
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Optional X coordinate"},
                "y": {"type": "integer", "description": "Optional Y coordinate"},
                "button": {"type": "string", "description": "Button: left, right, or middle", "default": "left"},
            },
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_mouse_doubleclick",
        description="Double-click at current position or specified coordinates.",
        handler=_mouse_doubleclick_handler,
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Optional X coordinate"},
                "y": {"type": "integer", "description": "Optional Y coordinate"},
            },
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_mouse_drag",
        description="Drag mouse from one coordinate to another (useful for selecting text or moving items).",
        handler=_mouse_drag_handler,
        parameters={
            "type": "object",
            "properties": {
                "x1": {"type": "integer", "description": "Start X"},
                "y1": {"type": "integer", "description": "Start Y"},
                "x2": {"type": "integer", "description": "End X"},
                "y2": {"type": "integer", "description": "End Y"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_type",
        description="Type text at the currently focused element/field.",
        handler=_key_type_handler,
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_key_press",
        description="Press keyboard shortcut combination. Examples: ['ctrl','c'] for copy, ['alt','tab'] for window switch.",
        handler=_key_press_handler,
        parameters={
            "type": "object",
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key names to press together. Common: ctrl, alt, shift, tab, enter, escape, f1-f12, backspace, delete, home, end, up, down, left, right",
                },
            },
            "required": ["keys"],
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_scroll",
        description="Scroll the mouse wheel.",
        handler=_scroll_handler,
        parameters={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "description": "up, down, left, or right", "default": "down"},
                "amount": {"type": "integer", "description": "Number of scroll clicks", "default": 3},
            },
        },
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_screen_size",
        description="Get the screen resolution (width x height).",
        handler=_screen_size_handler,
        parameters={"type": "object", "properties": {}},
        category="computer_use",
    ))

    registry.register(ToolDef(
        name="cu_mouse_position",
        description="Get current mouse cursor coordinates.",
        handler=_mouse_position_handler,
        parameters={"type": "object", "properties": {}},
        category="computer_use",
    ))

    # ── High-level visual closed loop ──────────────────────────

    def _cu_do_handler(task: str, max_steps: int = 20, **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            result = cu.do(task, max_steps)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_do",
        description="HIGH-LEVEL computer use: give a task description, the agent will look at the screen, decide actions, and repeat until done. Examples: 'Open Chrome and go to google.com', 'Test the login form error message', 'Check if the button has proper contrast'.",
        handler=_cu_do_handler,
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description. Be specific about what to do."},
                "max_steps": {"type": "integer", "description": "Maximum steps before timeout", "default": 20},
            },
            "required": ["task"],
        },
        category="computer_use",
        examples=["cu_do(task='Open Notepad and type Hello World')",
                  "cu_do(task='Open Chrome and search for Python tutorial')"],
    ))

    # ── Application start tool ──────────────────────────

    def _launch_app_handler(app_name: str, args: str = "", **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            return json.dumps({"success": True, "result": cu.launch_app(app_name, args)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_launch",
        description="Launch a Windows application by name. Supports: chrome, notepad, vscode, terminal, calculator, explorer, paint, settings, etc.",
        handler=_launch_app_handler,
        parameters={
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "Application name (e.g. 'chrome', 'notepad', 'vscode')"},
                "args": {"type": "string", "description": "Optional command line arguments", "default": ""},
            },
            "required": ["app_name"],
        },
        category="computer_use",
        examples=["cu_launch(app_name='notepad')", "cu_launch(app_name='chrome', args='--incognito')"],
    ))

    def _open_url_handler(url: str, browser: str = "chrome", **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            return json.dumps({"success": True, "result": cu.open_url(url, browser)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_open_url",
        description="Open a URL in the specified browser.",
        handler=_open_url_handler,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open (e.g. 'https://google.com')"},
                "browser": {"type": "string", "description": "Browser name", "default": "chrome"},
            },
            "required": ["url"],
        },
        category="computer_use",
    ))

    # ── browser/CDP tool ──────────────────────────

    def _browser_launch_handler(**kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            return json.dumps({"success": True, "result": cu.browser_launch()})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_browser_launch",
        description="Launch Chrome with remote debugging port (CDP) enabled for precision browser control.",
        handler=_browser_launch_handler,
        parameters={"type": "object", "properties": {}},
        category="computer_use",
    ))

    def _browser_navigate_handler(url: str, **kwargs) -> str:
        try:
            cu = _get_cu()
            result = cu.browser_navigate(url)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_browser_navigate",
        description="Navigate current Chrome tab to a URL via CDP.",
        handler=_browser_navigate_handler,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to (e.g. 'https://google.com')"},
            },
            "required": ["url"],
        },
        category="computer_use",
    ))

    def _browser_text_handler(**kwargs) -> str:
        try:
            cu = _get_cu()
            text = cu.browser_text()
            return json.dumps({"success": True, "text": text[:10000]})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_browser_text",
        description="Get the text content of the current browser page via CDP.",
        handler=_browser_text_handler,
        parameters={"type": "object", "properties": {}},
        category="computer_use",
    ))

    def _browser_click_handler(selector: str, **kwargs) -> str:
        try:
            cu = _get_cu()
            result = cu.browser_click(selector)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_browser_click_element",
        description="Click a page element by CSS selector via CDP.",
        handler=_browser_click_handler,
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector (e.g. '#login-btn', '.submit', 'button')"},
            },
            "required": ["selector"],
        },
        category="computer_use",
    ))

    def _browser_fill_handler(selector: str, text: str, **kwargs) -> str:
        try:
            cu = _get_cu()
            result = cu.browser_fill(selector, text)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_browser_fill",
        description="Fill an input field by CSS selector via CDP.",
        handler=_browser_fill_handler,
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the input field"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["selector", "text"],
        },
        category="computer_use",
    ))

    # ── Element-level interaction tool ──────────────────────────

    def _find_element_handler(description: str, **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            result = cu.find_element(description)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_find_element",
        description="Find an element on screen by visual description (e.g. 'Login button', 'Search box'). Returns coordinates.",
        handler=_find_element_handler,
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Visual description of the element to find (e.g. 'Submit button', 'Username input field')"},
            },
            "required": ["description"],
        },
        category="computer_use",
    ))

    def _click_text_handler(label: str, **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            result = cu.click_text(label)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_click_text",
        description="Find text/button on screen and click it. Uses vision to locate the element.",
        handler=_click_text_handler,
        parameters={
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Text label or description of the element to click (e.g. 'Login', 'Submit', 'OK')"},
            },
            "required": ["label"],
        },
        category="computer_use",
    ))

    def _fill_field_handler(label: str, text: str, **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            result = cu.fill_field(label, text)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_fill_field",
        description="Find an input field by visual description and type text into it.",
        handler=_fill_field_handler,
        parameters={
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Visual description of the input field (e.g. 'Username', 'Search box', 'Email')"},
                "text": {"type": "string", "description": "Text to type into the field"},
            },
            "required": ["label", "text"],
        },
        category="computer_use",
    ))

    # ── Smart Degradation tool ────────────────────

    def _smart_handler(task: str, max_steps: int = 20, **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            result = cu.smart(task, max_steps)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_smart",
        description="SMART EXECUTION: automatically chooses the best path for a task. Tries CDP/API first, degrades to vision+GUI if needed. Examples: 'Open Chrome and go to google.com' (uses CDP), 'Open Notepad and type hello' (uses app launcher + vision), 'Click the login button' (uses element finder + click).",
        handler=_smart_handler,
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description. The system will choose the best execution path."},
                "max_steps": {"type": "integer", "description": "Maximum vision loop steps if falling back to GUI", "default": 20},
            },
            "required": ["task"],
        },
        category="computer_use",
        examples=["cu_smart(task='Open Chrome and go to google.com')",
                  "cu_smart(task='Search for Python tutorials')",
                  "cu_smart(task='Click the login button and type my password')"],
    ))

    # ── Appshot tool ────────────────────────────

    def _screenshot_active_handler(**kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            path = cu.screenshot_active()
            return json.dumps({"success": True, "path": path})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_appshot",
        description="Appshot: capture only the active/foreground window instead of full screen.",
        handler=_screenshot_active_handler,
        parameters={"type": "object", "properties": {}},
        category="computer_use",
    ))

    # ── History tool ────────────────────────────────

    def _history_handler(**kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            hist = cu.history()
            return json.dumps({"success": True, "history": hist})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_history",
        description="Show recent Computer Use action history with timestamps.",
        handler=_history_handler,
        parameters={"type": "object", "properties": {}},
        category="computer_use",
    ))

    def _history_clear_handler(**kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            cu.history_clear()
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_history_clear",
        description="Clear Computer Use action history.",
        handler=_history_clear_handler,
        parameters={"type": "object", "properties": {}},
        category="computer_use",
    ))

    def _rollback_handler(steps: int = 1, **kwargs) -> str:
        if not _available():
            return json.dumps({"error": "Computer Use not available"})
        try:
            cu = _get_cu()
            result = cu.rollback(steps)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(ToolDef(
        name="cu_rollback",
        description="Rollback undoable actions: mouse_move (restore pos), key_type (Ctrl+Z). Check cu_history first.",
        handler=_rollback_handler,
        parameters={
            "type": "object",
            "properties": {
                "steps": {"type": "integer", "description": "Number of actions to rollback", "default": 1},
            },
        },
        category="computer_use",
    ))
