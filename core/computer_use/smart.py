"""core/computer_use/smart.py — Smart Degradation layer

Claude Code style "API first, GUI later" strategy.
Given a task, auto-select the best path to execute.

Degradation chain:
  1. Direct API / built-in command
  2. CDP browser control
  3. Vision-based GUI automation
  4. Fallback: co_do visual loop
"""

from __future__ import annotations
import os
import re
from typing import Optional


# ── Task type detection ──────────────────────────────────

def _classify_task(task: str) -> str:
    """classificationtasktype. """
    t = task.lower()

    # Browser related
    browser_keywords = [
        "browser", "chrome", "firefox", "edge", "web", "internet",
        "google", "search", "youtube", "facebook", "twitter", "github",
        "open http", "visit ", "navigate to", "go to ",
        ".com", ".org", ".net", ".io",
        "http://", "https://",
    ]
    for kw in browser_keywords:
        if kw in t:
            return "browser"

    # Application start
    app_keywords = [
        "open ", "launch ", "start ", "run ", "open",
        "notepad", "notepad", "calculator", "calculator ",
        "vscode", "terminal", "cmd",
        "explorer", "file manager", "resource management ",
    ]
    for kw in app_keywords:
        if kw in t:
            return "app_launch"

    # File operations
    file_keywords = [
        "file ", "open file", "save", "read file",
        "create ", "write ", "edit ", "delete ",
        ".txt", ".py", ".js", ".html", ".json",
    ]
    for kw in file_keywords:
        if kw in t:
            return "file_op"

    # System operations
    system_keywords = [
        "setting", "control panel", "task manager",
        "screenshot", "screen capture", "screenshot", "screenshot",
        "volume", "brightness", "wifi", "bluetooth",
    ]
    for kw in system_keywords:
        if kw in t:
            return "system"

    # contains  URL
    url_pattern = re.compile(r'https?://\S+')
    if url_pattern.search(task):
        return "browser"

    # Test/GUI operations
    test_keywords = [
        "click ", "type ", "press ", "double click", "right click",
        "drag ", "scroll ", "select ", "check ", "verify",
        "test ", "try ", "fill ", "input ", "enter ",
        "find ", "search for ", "locate ",
        "should ", "expect ", "assert ", "compare ",
    ]
    for kw in test_keywords:
        if kw in t:
            return "gui"

    # default
    return "gui"


# ── Smart Execution ─────────────────────────────

def smart_execute(task: str, cu=None, max_vision_steps: int = 20) -> dict:
    """Smart Degradation execute . 

    1. classificationtask
    2. Select the best path
    3. needs   degradation

    Args:
        task: Task description
        cu: ComputerUse instance
        max_vision_steps: Maximum steps for vision closed loop
    """
    if cu is None:
        from core.computer_use import get as _get_cu
        cu = _get_cu()

    task_type = _classify_task(task)
    result = {"task": task, "type": task_type, "path": "", "success": False, "summary": ""}

    if task_type == "app_launch":
        return _handle_app_launch(task, cu, result)

    elif task_type == "browser":
        return _handle_browser(task, cu, result)

    elif task_type == "file_op":
        return _handle_file_op(task, cu, result)

    elif task_type == "system":
        return _handle_system(task, cu, result)

    else:
        # GUI / default: Try vision closed loop first
        return _handle_gui(task, cu, result, max_vision_steps)


def _handle_app_launch(task: str, cu, result: dict) -> dict:
    """Application start path."""
    from core.computer_use.apps import launch, launch_url

    # Try to retrieve application name
    task_lower = task.lower()
    # from "open X" / "launch X" / "open X" retrieve name
    for prefix in ["open ", "launch ", "start ", "run ", "open"]:
        if prefix in task_lower:
            name = task_lower.split(prefix)[-1].strip().split()[0]
            # retrieve URL parameters
            url_pattern = re.compile(r'https?://\S+')
            urls = url_pattern.findall(task)
            if urls and name in ("chrome", "browser", "web"):
                try:
                    out = launch_url(urls[0])
                    result.update({"success": True, "summary": out, "path": "api:launch_url"})
                    return result
                except Exception as e:
                    result["summary"] = f"URL launch failed: {e}, trying fallback..."

            try:
                out = launch(name)
                result.update({"success": True, "summary": out, "path": "api:launch"})
                return result
            except Exception:
                pass

    # Fallback: Directly start application
    try:
        from core.computer_use.apps import launch
        name = task_lower.replace("open ", "").replace("launch ", "").replace("start ", "").strip().split()[0]
        out = launch(name)
        result.update({"success": True, "summary": out, "path": "api:launch"})
        return result
    except Exception as e:
        result["summary"] = f"API launch failed, falling back to vision: {e}"

    # Fallback: Vision closed loop
    return _handle_gui(task, cu, result)


def _handle_browser(task: str, cu, result: dict) -> dict:
    """Browser operation path — CDP first, then vision."""
    from core.computer_use.browser import is_running, launch as cdp_launch, navigate, tab_screenshot, get_page_text

    # Phase 1: Ensure Chrome + CDP runs
    try:
        if not is_running():
            cdp_launch()
        result["summary"] = "CDP ready"
    except Exception as e:
        result["summary"] = f"CDP not available, falling back to vision: {e}"
        return _handle_gui(task, cu, result)

    # Phase 2: retrieve URL
    url_pattern = re.compile(r'https?://\S+')
    urls = url_pattern.findall(task)

    # if  is search
    search_match = re.search(r'(?:search|search|search|find)\s*(?:for|one )?\s*["「]?(.+?)["」]?(?:\s+on|\s+in|\s*$)', task, re.IGNORECASE)
    if search_match and not urls:
        query = search_match.group(1)
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        try:
            navigate(search_url)
            result.update({"success": True, "summary": f"Searched '{query}' on Google", "path": "cdp:navigate"})
            return result
        except Exception:
            pass

    # Direct navigation
    if urls:
        try:
            navigate(urls[0])
            result.update({"success": True, "summary": f"Navigated to {urls[0]}", "path": "cdp:navigate"})
            return result
        except Exception as e:
            result["summary"] = f"CDP navigate failed: {e}, falling back to vision"

    # Open new tab → Navigate
    try:
        # Retrieve website name, construct Google search
        words = task.lower().replace("open ", "").replace("browser", "").replace("chrome", "").strip()
        if words and words not in ("open", "browser", "chrome", "web"):
            search_url = f"https://www.google.com/search?q={words.replace(' ', '+')}"
            navigate(search_url)
            result.update({"success": True, "summary": f"Searched '{words}'", "path": "cdp:navigate"})
            return result
    except Exception:
        pass

    # Fallback: Vision closed loop
    result["summary"] = "CDP exhausted, using vision"
    return _handle_gui(task, cu, result)


def _handle_file_op(task: str, cu, result: dict) -> dict:
    """File operation path."""
    # Direct filesystem operations do not need Computer Use
    # If it is opening a file, use PowerShell Start-Process
    from core.computer_use.apps import open_file
    import re

    # Try to retrieve file path
    path_patterns = [
        r'["\'](.+?\.\w+)["\']',
        r'(?:open|open|open file)\s+(\S+\.\w+)',
        r'(?:file|file)\s*:?\s*["\']?(\S+\.\w+)["\']?',
        r'(\S+\.\w+)',
    ]

    for pattern in path_patterns:
        m = re.search(pattern, task)
        if m:
            path = m.group(1)
            # Check if it is a WSL path, convert to Windows path
            if path.startswith("/mnt/"):
                try:
                    open_file(path)
                    result.update({"success": True, "summary": f"Opened {path}", "path": "api:open_file"})
                    return result
                except Exception:
                    pass

    # Fallback
    result["summary"] = "File operation via vision"
    return _handle_gui(task, cu, result)


def _handle_system(task: str, cu, result: dict) -> dict:
    """System operation path."""
    task_lower = task.lower()

    if "screenshot" in task_lower or "screenshot" in task_lower or "screenshot" in task_lower:
        path = cu.screenshot(f"/tmp/cu_smart_{int(__import__('time').time())}.png")
        result.update({"success": True, "summary": f"Saved screenshot to {path}", "path": "api:screenshot"})
        return result

    if "task manager" in task_lower or "taskmgr" in task_lower:
        from core.computer_use.apps import launch
        launch("task_manager")
        result.update({"success": True, "summary": "Opened Task Manager", "path": "api:launch"})
        return result

    # Fallback
    result["summary"] = "System operation via vision"
    return _handle_gui(task, cu, result)


def _handle_gui(task: str, cu, result: dict, max_steps: int = 20) -> dict:
    """GUI operation path — vision closed loop."""
    result["path"] = "vision:do_task"
    try:
        from core.computer_use.vision import do_task
        vision_result = do_task(task, cu=cu, max_steps=max_steps)
        result.update({
            "success": vision_result.get("success", False),
            "summary": vision_result.get("summary", ""),
            "steps": vision_result.get("steps", 0),
            "vision_details": vision_result,
        })
        return result
    except Exception as e:
        result.update({"success": False, "summary": f"Vision loop failed: {e}"})
        return result
