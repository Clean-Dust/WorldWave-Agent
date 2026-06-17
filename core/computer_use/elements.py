"""core/computer_use/elements.py — Element-level interaction

Find specific text/button/icon position on screen, then click.
Use vision model for semantic localization (given text description → return coordinates).
"""

from __future__ import annotations
import json
import urllib.request

from core.computer_use.config import get_config
from core.computer_use.vision import VISION_API_KEY, VISION_BASE_URL, _img_to_data_url


ELEMENT_PROMPT = """You are a screen element locator. Given a screenshot and a description of an element to find, return its coordinates.

RULES:
1. Look at the screenshot carefully
2. Find the element matching the description (text/button/icon/field)
3. Return the CENTER coordinates of that element
4. If the element has multiple instances, return the most relevant one
5. If not found, return {"found": false, "reason": "..."}

RESPONSE FORMAT (JSON only):
{
  "found": true,
  "x": 500,
  "y": 350,
  "width": 120,
  "height": 40,
  "label": "Login button",
  "confidence": "high|medium|low"
}"""


def find_element(task: str, screenshot_path: str, cu=None) -> dict:
    """Find specified element position on screen.

    Args:
        task: Element description, e.g., "Login button", "Search input field", "Submit"
        screenshot_path: screenscreenshotpath
        cu: ComputerUse instance (for screenshot dimensions)

    Returns:
        {"found": bool, "x": int, "y": int, "width": int, "height": int,
         "label": str, "confidence": str}
    """
    img_url = _img_to_data_url(screenshot_path)

    payload = json.dumps({
        "model": get_config().vision_model,
        "messages": [
            {"role": "system", "content": ELEMENT_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"Find this element on screen: {task}"},
                {"type": "image_url", "image_url": {"url": img_url}},
            ]},
        ],
        "temperature": 0.05,
        "max_tokens": 512,
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
            # Process markdown JSON packaging
            content = content.strip()
            if content.startswith("```"):
                parts = content.split("\n", 1)
                if len(parts) > 1:
                    content = parts[1]
                if content.endswith("```"):
                    content = content[:-3].strip()
            data = json.loads(content)

            if not data.get("found", False):
                return {
                    "found": False,
                    "reason": data.get("reason", "Element not found"),
                    "label": task,
                }

            return {
                "found": True,
                "x": int(data.get("x", 0)),
                "y": int(data.get("y", 0)),
                "width": int(data.get("width", 20)),
                "height": int(data.get("height", 20)),
                "label": data.get("label", task),
                "confidence": data.get("confidence", "medium"),
            }
    except json.JSONDecodeError as e:
        return {"found": False, "reason": f"Parse error: {e}", "label": task}
    except Exception as e:
        return {"found": False, "reason": str(e), "label": task}


def find_and_click(task: str, cu) -> dict:
    """Find element and click.

    Process: screenshot → vision finds element coordinates → move and click.
    """
    path = cu.screenshot()
    result = find_element(task, path, cu)

    if not result.get("found"):
        return {"success": False, "reason": result.get("reason", "not found"),
                "found": False}

    x, y = result["x"], result["y"]
    cu.mouse_move(x, y)
    cu.mouse_click(x, y)
    return {
        "success": True,
        "x": x, "y": y,
        "label": result.get("label", task),
        "confidence": result.get("confidence", "medium"),
    }


def find_and_type(task: str, text: str, cu) -> dict:
    """Find element (input box), click and type text."""
    click_result = find_and_click(task, cu)
    if not click_result.get("success"):
        return click_result

    cu.key_type(text)
    return {
        "success": True,
        "action": f"Typed '{text}' into '{task}'",
        "x": click_result["x"],
        "y": click_result["y"],
    }


def click_by_id(target_id, cu, button="left") -> dict:
    """Click a numbered element by its SoM target ID.

    Used by vision.py Tier 3+ (Set-of-Mark). Looks up the element
    in the UIAutomation element cache and clicks its center.

    Args:
        target_id: Element ID (int or str)
        cu: ComputerUse instance
        button: "left" | "right"

    Returns:
        {"success": bool, "msg": str, "x": int, "y": int}
    """
    # Try to find element in the stored list
    eid = int(target_id) if not isinstance(target_id, int) else target_id

    # Look up from recent UIA element cache (stored on cu)
    elements = getattr(cu, "_last_uia_elements", [])
    target = None
    for el in elements:
        if el.get("id") == eid:
            target = el
            break

    if target:
        cx = target["x"] + target["width"] // 2
        cy = target["y"] + target["height"] // 2
    else:
        # No cache — try fresh UIA scan
        from core.computer_use.uia import get_interactive_elements
        fresh = get_interactive_elements()
        for el in fresh:
            if el.get("id") == eid:
                cx = el["x"] + el["width"] // 2
                cy = el["y"] + el["height"] // 2
                target = el
                break
        else:
            return {"success": False, "msg": f"Element [{eid}] not found in UIA tree", "x": 0, "y": 0}

    cu.mouse_move(cx, cy)
    cu.mouse_click(cx, cy, button)
    return {"success": True, "msg": f"Clicked [{eid}]", "x": cx, "y": cy}
