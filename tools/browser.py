"""ww/tools/browser.py — WW browser control tool

Allow WW to control browser for web page interaction.
Use Chrome headless mode (no GUI required).

supports: 
- loadpage + get DOM
- screenshot
- execute JavaScript
- click element
- fill form
- wait for element
"""

from __future__ import annotations
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional


CHROME_PATH = "google-chrome"
CHROME_HEADLESS_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--single-process",
    "--disable-web-security",
    "--window-size=1920,1080",
]


def _chrome_cmd(args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """execute Chrome command. """
    cmd = [CHROME_PATH] + CHROME_HEADLESS_ARGS + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _browser_navigate_handler(url: str, wait_seconds: int = 3,
                              screenshot: bool = False) -> Dict:
    """Load the webpage and get the DOM content."""
    try:
        result = _chrome_cmd([
            "--dump-dom",
            "--virtual-time-budget=" + str(wait_seconds * 1000),
            url,
        ], timeout=wait_seconds + 10)
        
        if result.returncode != 0:
            return {"success": False, "error": result.stderr[:500]}
        
        content = result.stdout[:10000]
        
        output = {
            "url": url,
            "content": content,
            "content_length": len(result.stdout),
            "title": _extract_title(content),
        }
        
        if screenshot:
            ss = _browser_screenshot_handler(url, wait_seconds)
            if ss.get("success"):
                output["screenshot"] = ss.get("path")
        
        return {"success": True, "output": json.dumps(output, indent=2), "data": output}
    
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "browser timeout"}
    except FileNotFoundError:
        return {"success": False, "error": "google-chrome not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _browser_screenshot_handler(url: str, wait_seconds: int = 3,
                                path: str = "") -> Dict:
    """Take a screenshot of the webpage."""
    try:
        output_path = path or os.path.join(tempfile.gettempdir(),
                                           "ww_screenshot_" + str(int(time.time())) + ".png")
        result = _chrome_cmd([
            "--screenshot=" + output_path,
            "--virtual-time-budget=" + str(wait_seconds * 1000),
            url,
        ], timeout=wait_seconds + 10)
        
        if result.returncode != 0:
            return {"success": False, "error": result.stderr[:500]}
        
        if os.path.isfile(output_path):
            size = os.path.getsize(output_path)
            return {"success": True, "output": output_path, "data": {"path": output_path, "size": size}}
        
        return {"success": False, "error": "screenshot not created"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _browser_js_handler(url: str, script: str, wait_seconds: int = 3) -> Dict:
    """Execute JavaScript on the page and get the result."""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
            f.write(script)
            js_path = f.name
        
        result = _chrome_cmd([
            "--dump-dom",
            "--virtual-time-budget=" + str(wait_seconds * 1000),
            "--eval=" + script,
            url,
        ], timeout=wait_seconds + 10)
        
        os.unlink(js_path)
        
        if result.returncode != 0:
            return {"success": False, "error": result.stderr[:500]}
        
        return {"success": True, "output": result.stdout[:5000], "data": {"result": result.stdout[:5000]}}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _browser_pdf_handler(url: str, path: str = "", wait_seconds: int = 3) -> Dict:
    """Will output the webpage as PDF."""
    try:
        output_path = path or os.path.join(tempfile.gettempdir(),
                                           "ww_page_" + str(int(time.time())) + ".pdf")
        result = _chrome_cmd([
            "--print-to-pdf=" + output_path,
            "--virtual-time-budget=" + str(wait_seconds * 1000),
            url,
        ], timeout=wait_seconds + 10)
        
        if result.returncode != 0:
            return {"success": False, "error": result.stderr[:500]}
        
        if os.path.isfile(output_path):
            size = os.path.getsize(output_path)
            return {"success": True, "output": output_path, "data": {"path": output_path, "size": size}}
        
        return {"success": False, "error": "PDF not created"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _extract_title(html: str) -> str:
    """Retrieve the title from HTML."""
    match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip()[:100] if match else ""


def register_browser_tools(registry):
    """Register the browser tool to the registry."""
    registry.register_from_def(
        "browser_navigate",
        "loadWebpage andget DOM content. reads  JavaScript Render page. ",
        _browser_navigate_handler,
        parameters={
            "url": {"type": "string", "description": "Webpage URL"},
            "wait_seconds": {"type": "integer", "description": "Wait for rendering seconds", "default": 3},
            "screenshot": {"type": "boolean", "description": " is Different or same screenshot", "default": False},
        },
        examples=['browser_navigate(url="https://example.com")',
                  'browser_navigate(url="https://example.com", screenshot=True)'],
        category="browser",
    )

    registry.register_from_def(
        "browser_screenshot",
        "Capture webpagescreenshot (fullpage) . ",
        _browser_screenshot_handler,
        parameters={
            "url": {"type": "string", "description": "Webpage URL"},
            "wait_seconds": {"type": "integer", "description": "Wait for rendering seconds", "default": 3},
            "path": {"type": "string", "description": "outputpath (optional) ", "default": ""},
        },
        category="browser",
    )

    registry.register_from_def(
        "browser_js",
        "at Webpage execute JavaScript andgetResult. reads dynamiccontent、clickbuttonetc.. ",
        _browser_js_handler,
        parameters={
            "url": {"type": "string", "description": "Webpage URL"},
            "script": {"type": "string", "description": "JavaScript Code"},
            "wait_seconds": {"type": "integer", "description": "WaitexecuteSeconds", "default": 3},
        },
        examples=['browser_js(url="https://example.com", script="document.title")',
                  'browser_js(url="https://example.com", script="JSON.parse(document.body.innerText)")'],
        category="browser",
    )

    registry.register_from_def(
        "browser_pdf",
        "will Webpageoutputas PDF file. ",
        _browser_pdf_handler,
        parameters={
            "url": {"type": "string", "description": "Webpage URL"},
            "path": {"type": "string", "description": "outputpath (optional) ", "default": ""},
            "wait_seconds": {"type": "integer", "description": "Wait for rendering seconds", "default": 3},
        },
        category="browser",
    )
