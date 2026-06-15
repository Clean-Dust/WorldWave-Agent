"""core/computer_use/browser.py — Browser Stealth control

Use puppeteer-extra + stealth to connect CDP,
Avoid anti-crawling detection, provide precise browser control.

Architecture:
  Python (WSL) → node.exe (Windows) → puppeteer-extra + stealth → CDP (Chrome/Edge)

Dependencies:
  - Chrome/Edge running in CDP mode (port 9222)
  - Node.js at  Windows  
  - puppeteer-extra + stealth plugin (installed in Windows playwright directory)
"""

from __future__ import annotations
import json
import os
import subprocess
import time

from core.computer_use import _ps, ComputerUseError

CDP_PORT = 9222
STEALTH_HELPER = "C:\\Users\\Public\\playwright\\stealth_browser.js"

# ── browserlifecycle ──────────────────────────────

def _find_browser() -> str:
    """Auto detect available browser path."""
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in paths:
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", f"Test-Path '{p}'"],
                capture_output=True, text=True, timeout=5
            )
            if "True" in r.stdout:
                return p
        except Exception:
            continue
    raise ComputerUseError("No browser found (Chrome or Edge required)")


def is_running() -> bool:
    """Check if browser CDP is currently listening."""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"try {{ Invoke-RestMethod -Uri 'http://127.0.0.1:{CDP_PORT}/json/version' -ErrorAction Stop | ConvertTo-Json -Compress }} catch {{}}"],
            capture_output=True, text=True, timeout=5
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def launch() -> str:
    """Start browser and enable CDP (port 9222)."""
    if is_running():
        return "Browser CDP already running on port 9222"

    browser_path = _find_browser()

    try:
        _ps(f"Start-Process -WindowStyle Hidden '{browser_path}' "
            f"-ArgumentList '--remote-debugging-port={CDP_PORT} --no-first-run about:blank'")
        time.sleep(3)

        if is_running():
            name = os.path.splitext(os.path.basename(browser_path))[0]
            return f"{name} launched with CDP on port 9222"
        # Try one more time with longer wait
        time.sleep(3)
        if is_running():
            return "Browser launched with CDP"
        return "Browser launched (CDP check timed out)"
    except Exception as e:
        raise ComputerUseError(f"Failed to launch browser: {e}")


def close_tabs():
    """Close all CDP tabs."""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"$r = Invoke-RestMethod 'http://127.0.0.1:{CDP_PORT}/json' -ErrorAction SilentlyContinue; "
             f"if($r){{$r|ForEach-Object{{try{{Invoke-WebRequest 'http://127.0.0.1:{CDP_PORT}/json/close/$($_.id)' -Method Get -ErrorAction SilentlyContinue}}catch{{}}}};'closed'}}"],
            capture_output=True, text=True, timeout=10
        )
        return
    except Exception:
        pass


# ── Stealth browser operations ──────────────────────────

def _call(action: str, params: dict = None, timeout: int = 60) -> dict:
    """via  stealth Node.js helper execute browser operations."""
    if params is None:
        params = {}

    # Ensure CDP runs 
    if not is_running():
        launch()

    args = json.dumps({
        "cdp_url": f"http://127.0.0.1:{CDP_PORT}",
        "action": action,
        "params": params,
    })

    try:
        r = subprocess.run(
            ["node.exe", STEALTH_HELPER, args],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0 and not r.stdout.strip():
            raise ComputerUseError(f"Stealth helper error: {r.stderr.strip()[:200]}")
        result = json.loads(r.stdout.strip())
        return result
    except json.JSONDecodeError:
        return {"success": False, "error": f"Invalid response: {r.stdout[:200]}"}
    except FileNotFoundError:
        return {"success": False, "error": "Node.js not found on Windows"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def navigate(url: str) -> dict:
    return _call("navigate", {"url": url}, timeout=60)


def screenshot() -> str:
    result = _call("screenshot", {}, timeout=30)
    return result.get("path") if result.get("success") else None


def get_page_text() -> str:
    result = _call("text", {})
    return result.get("text", "")


def get_title() -> str:
    result = _call("title", {})
    return result.get("title", "")


def click_element(selector: str) -> dict:
    return _call("click", {"selector": selector}, timeout=15)


def fill_input(selector: str, text: str) -> dict:
    return _call("fill", {"selector": selector, "text": text}, timeout=15)


def evaluate_js(code: str) -> dict:
    return _call("evaluate", {"code": code}, timeout=15)


def element_exists(selector: str) -> bool:
    result = _call("get_element", {"selector": selector})
    return result.get("found", False)
