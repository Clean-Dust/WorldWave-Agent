"""core/computer_use/ — WW Computer Use module

Benchmarked against Claude Code / Codex Computer Use feature.
Controls Windows desktop mouse, keyboard, screen screenshot.

Architecture:
  WW (WSL) → PowerShell → Win32 API (Windows)

Does not need Windows background service, each operation directly calls PowerShell.
"""

from __future__ import annotations
import os
import subprocess
import time
from typing import Optional

# Preload C# Win32 helper (auto-compile on first use)
CS_HELPER = r"C:\Users\Public\playwright\ww_cu.cs"
CS_REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "core/computer_use/ww_cu.cs")
PS_LOAD = f'Add-Type -Path "{CS_HELPER}"; '

PS_AVAILABLE = None


def _check_powershell() -> bool:
    global PS_AVAILABLE
    if PS_AVAILABLE is None:
        PS_AVAILABLE = os.path.exists("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return PS_AVAILABLE


def _ps(cmd: str, timeout: int = 30) -> str:
    """Execute PowerShell command, return stdout. Use semicolons to concatenate single-line commands."""
    ps_path = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    full_cmd = [ps_path, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=False, timeout=timeout)
        stdout = r.stdout.decode('utf-8', errors='replace').strip()
        stderr = r.stderr.decode('utf-8', errors='replace').strip()
        if r.returncode != 0:
            raise ComputerUseError(f"PowerShell error: {stderr or stdout}")
        if stdout.startswith("#< CLIXML"):
            raise ComputerUseError(f"PowerShell CLIXML error:\n{stdout}")
        return stdout
    except subprocess.TimeoutExpired:
        raise ComputerUseError("PowerShell command timed out")


class ComputerUseError(Exception):
    pass


class ComputerUse:
    """Computer Use control — directly call PowerShell/Win32 API.

    Supports progressive tiers (0-6) via env WW_COMPUTER_USE_TIER or
    constructor parameter. Higher tiers enable more capabilities:
      Tier 0 — Basic GDI screenshot, manual coords
      Tier 1 — MSS/DXGI fast capture
      Tier 2 — + UIAutomation element detection
      Tier 3 — + Set-of-Mark numbered labels
      Tier 4 — + Post-action visual verification
      Tier 5 — + Spatio-temporal memory
      Tier 6 — + Local cerebellum VLM (GPU)
    """

    def __init__(self, enable_history: bool = True, tier: int = None):
        if not _check_powershell():
            raise ComputerUseError("PowerShell not found at /mnt/c/Windows/System32/")
        self._screen_w = None
        self._screen_h = None
        self._enable_history = enable_history
        self._history: list[dict] = []
        self._mouse_pos_history: list[tuple] = []

        # Tier configuration
        from core.computer_use.config import reload_config, get_config
        if tier is not None:
            import os
            os.environ["WW_COMPUTER_USE_TIER"] = str(tier)
            reload_config()
        self._cfg = get_config()

        # UIA element cache (populated by vision.py SoM loop)
        self._last_uia_elements: list[dict] = []

        # Auto-deploy C# helper
        self._ensure_helper()

    def _log(self, tool: str, params: dict, result=None, before_shot: str = None):
        """Record operation history (for rollback)."""
        if not self._enable_history:
            return
        entry = {
            "ts": __import__("time").time(),
            "tool": tool,
            "params": dict(params),
            "result": result,
            "before_shot": before_shot,
            "after_shot": None,
        }
        self._history.append(entry)
        # Only keep the latest 50 entries
        if len(self._history) > 50:
            self._history = self._history[-50:]

    def _log_after(self, after_shot: str = None):
        """Supplement an operation after_shot."""
        if self._history and after_shot:
            self._history[-1]["after_shot"] = after_shot

    def _ensure_helper(self):
        """Ensure Windows side has C# Win32 helper."""
        win_path = "/mnt/c/Users/Public/playwright/ww_cu.cs"
        if not os.path.exists(win_path) and os.path.exists(CS_REPO):
            import shutil
            os.makedirs(os.path.dirname(win_path), exist_ok=True)
            shutil.copy2(CS_REPO, win_path)

    # ── Screen operations ──────────────────────────────────

    def screen_size(self) -> tuple[int, int]:
        out = _ps('Add-Type -AssemblyName System.Windows.Forms; '
                  '$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; '
                  'Write-Host "$($b.Width) $($b.Height)"')
        parts = out.split()
        self._screen_w, self._screen_h = int(parts[0]), int(parts[1])
        return (self._screen_w, self._screen_h)

    def screenshot(self, save_path: Optional[str] = None) -> str:
        """Capture full screen.

        Tier 0: GDI via PowerShell (slow, works everywhere)
        Tier 1+: MSS/DXGI (fast ~30ms, requires mss package)
        """
        from core.computer_use.capture import screenshot as cap_screenshot
        return cap_screenshot(save_path)

    def screenshot_b64(self) -> str:
        out = _ps('Add-Type -AssemblyName System.Windows.Forms; '
                  'Add-Type -AssemblyName System.Drawing; '
                  '$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; '
                  '$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; '
                  '$g = [System.Drawing.Graphics]::FromImage($bmp); '
                  '$g.CopyFromScreen($b.X, $b.Y, 0, 0, $b.Size); $g.Dispose(); '
                  '$ms = New-Object System.IO.MemoryStream; '
                  '$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); '
                  '$bmp.Dispose(); '
                  '[System.Convert]::ToBase64String($ms.ToArray())',
                  timeout=60)
        return out.strip()

    def screenshot_active(self, save_path=None):
        """Appshot: Only capture when active window (not full screen)."""
        if save_path is None:
            import time
            save_path = f"/mnt/c/Users/Public/playwright/appshot_{int(time.time())}.png"
        # Ensure save to Windows path
        win_path = save_path.replace("/mnt/c/", "C:/").replace("/", "\\")
        ps_cmd = f'{PS_LOAD}$info = [WWCU.Window]::GetActiveWindowInfo(); '
        ps_cmd += '$parts = $info.Split("|"); '
        ps_cmd += '$left = [int]$parts[0]; $top = [int]$parts[1]; $right = [int]$parts[2]; $bottom = [int]$parts[3]; '
        ps_cmd += '$w = $right - $left; $h = $bottom - $top; '
        ps_cmd += 'Add-Type -AssemblyName System.Windows.Forms; '
        ps_cmd += 'Add-Type -AssemblyName System.Drawing; '
        ps_cmd += 'if ($w -le 0 -or $h -le 0) { "FULLSCREEN"; exit }; '
        ps_cmd += '$bmp = New-Object System.Drawing.Bitmap $w, $h; '
        ps_cmd += '$g = [System.Drawing.Graphics]::FromImage($bmp); '
        ps_cmd += '$g.CopyFromScreen($left, $top, 0, 0, [System.Drawing.Size]::new($w, $h)); '
        ps_cmd += f'$bmp.Save("{win_path}", [System.Drawing.Imaging.ImageFormat]::Png); '
        ps_cmd += '$g.Dispose(); $bmp.Dispose(); Write-Host "OK"'
        try:
            out = _ps(ps_cmd, timeout=30)
            if "FULLSCREEN" in out or "OK" not in out:
                return self.screenshot(save_path)
            return save_path
        except Exception:
            return self.screenshot(save_path)

    # ── Mouse operations ──────────────────────────────────
        out = _ps(f'{PS_LOAD}'
                  '$p = New-Object WWCU.Mouse+POINT; '
                  '$null = [WWCU.Mouse]::GetCursorPos([ref]$p); '
                  'Write-Host "$($p.X) $($p.Y)"')
        parts = out.split()
        return (int(parts[0]), int(parts[1]))

    def mouse_move(self, x: int, y: int):
        self._log("mouse_move", {"x": x, "y": y})
        _ps(f'{PS_LOAD}$null = [WWCU.Mouse]::SetCursorPos({x},{y})')

    def mouse_click(self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left"):
        if x is not None and y is not None:
            self.mouse_move(x, y)
            time.sleep(0.05)
        btn_map = {"left": "0x0002,0x0004", "right": "0x0008,0x0010", "middle": "0x0020,0x0040"}
        down, up = btn_map.get(button, "0x0002,0x0004").split(",")
        _ps(f'{PS_LOAD}'
            f'$null = [WWCU.Mouse]::mouse_event({down},0,0,0,[UIntPtr]::Zero); '
            f'$null = [WWCU.Mouse]::mouse_event({up},0,0,0,[UIntPtr]::Zero)')
        self._log("mouse_click", {"x": x, "y": y, "button": button})

    def mouse_doubleclick(self, x: Optional[int] = None, y: Optional[int] = None):
        self.mouse_click(x, y)
        time.sleep(0.05)
        self.mouse_click(x, y)
        self._log("mouse_doubleclick", {"x": x, "y": y})

    def mouse_drag(self, start_x: int, start_y: int, end_x: int, end_y: int):
        self.mouse_move(start_x, start_y)
        time.sleep(0.1)
        _ps(f'{PS_LOAD}$null = [WWCU.Mouse]::mouse_event(0x0002,0,0,0,[UIntPtr]::Zero)')
        time.sleep(0.05)
        self.mouse_move(end_x, end_y)
        time.sleep(0.05)
        _ps(f'{PS_LOAD}$null = [WWCU.Mouse]::mouse_event(0x0004,0,0,0,[UIntPtr]::Zero)')
        self._log("mouse_drag", {"start_x": start_x, "start_y": start_y, "end_x": end_x, "end_y": end_y})

    def scroll(self, direction: str = "down", amount: int = 3):
        delta_map = {"up": "360", "down": "-360", "left": "-92160", "right": "92160"}
        wparam = delta_map.get(direction, "-360")
        _ps(f'{PS_LOAD}$null = [WWCU.Mouse]::mouse_event(0x0800,0,0,{wparam},[UIntPtr]::Zero)')

    # ── Keyboard operations ──────────────────────────────────

    def key_type(self, text: str):
        special = "+^%~(){}[]"
        escaped = ""
        for ch in text:
            if ch in special:
                escaped += "{" + ch + "}"
            elif ch == '\n':
                escaped += "{ENTER}"
            elif ch == '\t':
                escaped += "{TAB}"
            else:
                escaped += ch
        safe_text = escaped.replace("'", "''")
        _ps(f'Add-Type -AssemblyName System.Windows.Forms; '
            f"[System.Windows.Forms.SendKeys]::SendWait('{safe_text}')")
        self._log("key_type", {"text_len": len(text)})

    def key_press(self, keys: list):
        combo = "+".join(keys)
        _ps(f'Add-Type -AssemblyName System.Windows.Forms; '
            f'[System.Windows.Forms.SendKeys]::SendWait("({combo})")')
        self._log("key_press", {"keys": keys})

    def key_enter(self):
        self.key_press(["enter"])

    def key_tab(self):
        self.key_press(["tab"])

    def key_escape(self):
        self.key_press(["escape"])

    def key_backspace(self):
        self.key_press(["backspace"])

    # ── High-level vision closed loop ──────────────────────────────

    def do(self, task: str, max_steps: int = 20) -> dict:
        """High-level Computer Use: See → Think → Do → See vision closed loop.

        Tier-aware: adapts behavior based on WW_COMPUTER_USE_TIER:
          Tier 1-2: Direct pixel coordinate guessing
          Tier 3+:  Set-of-Mark numbered elements (95%+ accuracy)
          Tier 4+:  Post-action pixel diff verification

        Args:
            task: Task description (e.g. "Open Chrome and go to google.com")
            max_steps: Maximum steps

        Returns:
            {"success": bool, "summary": "...", "steps": int, "actions_taken": [...]}
        """
        from core.computer_use.vision import do_task
        return do_task(task, cu=self, max_steps=max_steps)

    # ── Application start ──────────────────────────────

    def launch_app(self, app_name: str, args: str = "") -> str:
        """Start Windows application."""
        from core.computer_use.apps import launch
        return launch(app_name, args)

    def open_url(self, url: str, browser: str = "chrome") -> str:
        """Open URL in browser."""
        from core.computer_use.apps import launch_url
        return launch_url(url, browser)

    def open_file(self, path: str) -> str:
        """Open file with default application."""
        from core.computer_use.apps import open_file
        return open_file(path)

    # ── Browser/CDP control ──────────────────────────

    def browser_launch(self, headless: bool = False) -> str:
        from core.computer_use.browser import launch as b_launch
        return b_launch(headless)

    def browser_navigate(self, url: str) -> dict:
        from core.computer_use.browser import navigate
        return navigate(url)

    def browser_screenshot(self) -> str:
        from core.computer_use.browser import tab_screenshot
        return tab_screenshot()

    def browser_text(self) -> str:
        from core.computer_use.browser import get_page_text
        return get_page_text()

    def browser_click(self, selector: str) -> dict:
        from core.computer_use.browser import click_element
        return click_element(selector)

    def browser_fill(self, selector: str, text: str) -> dict:
        from core.computer_use.browser import fill_input
        return fill_input(selector, text)

    def browser_js(self, code: str) -> dict:
        from core.computer_use.browser import evaluate_js
        return evaluate_js(code)

    # ── Element-level interaction ──────────────────────────────

    def find_element(self, description: str) -> dict:
        """Find specified element position on screen."""
        from core.computer_use.elements import find_element as _fe
        path = self.screenshot()
        return _fe(description, path, self)

    def click_text(self, label: str) -> dict:
        """Find text element on screen and click."""
        from core.computer_use.elements import find_and_click
        return find_and_click(label, self)

    def fill_field(self, label: str, text: str) -> dict:
        """Find input box and fill in text."""
        from core.computer_use.elements import find_and_type
        return find_and_type(label, text, self)

    # ── Smart Degradation ────────────────────────

    def smart(self, task: str, max_steps: int = 20) -> dict:
        """Smart Execution: Auto-select best path to execute task."""
        from core.computer_use.smart import smart_execute
        return smart_execute(task, cu=self, max_vision_steps=max_steps)

    # ── Operation history and Rollback ────────────────────────

    def history(self, limit: int = 10) -> list[dict]:
        return list(self._history[-limit:])

    def history_clear(self):
        self._history.clear()
        self._mouse_pos_history.clear()

    def rollback(self, steps: int = 1) -> dict:
        """Rollback the latest N operations. Can restore: mouse_move (recovery position)."""
        if steps < 1:
            return {"success": True, "message": "Nothing to rollback"}
        if not self._history:
            return {"success": False, "message": "No history to rollback"}

        undone = 0
        for i in range(min(steps, len(self._history))):
            entry = self._history[-(i + 1)]
            tool = entry["tool"]

            if tool == "mouse_move":
                # Recovery to operation position
                # First try to find a mouse_move position
                if i + 1 < len(self._history):
                    prev_entry = self._history[-(i + 2)]
                    if prev_entry["tool"] == "mouse_move":
                        self.mouse_move(prev_entry["params"]["x"], prev_entry["params"]["y"])
                    else:
                        # No mouse_move, find the nearest position in history
                        found = False
                        for older in reversed(self._history[:-(i + 1)]):
                            if older["tool"] == "mouse_move":
                                self.mouse_move(older["params"]["x"], older["params"]["y"])
                                found = True
                                break
                        if not found:
                            # No reference position, move to center
                            w, h = self.screen_size()
                            self.mouse_move(w // 2, h // 2)
                undone += 1
            elif tool == "key_type":
                self.key_press(["ctrl", "z"])
                undone += 1
            elif tool == "key_press":
                if entry["params"]["keys"] == ["ctrl", "z"]:
                    pass  # Itself is Ctrl+Z, cannot undo
                else:
                    self.key_press(["ctrl", "z"])
                    undone += 1
            elif tool in ("mouse_click", "mouse_doubleclick", "scroll"):
                pass  # These operations cannot be undone, skip
            else:
                pass

        # Remove rollback entries from history
        self._history = self._history[:-steps]
        return {
            "success": undone > 0,
            "message": f"Rolled back {undone} action(s)" if undone else "No undoable actions found",
            "undone": undone,
            "skipped": steps - undone,
        }


# ── Module-level shortcut functions ──────────────────────────────

_default_cu = None


def get() -> ComputerUse:
    global _default_cu
    if _default_cu is None:
        _default_cu = ComputerUse()
    return _default_cu


def check_available() -> bool:
    try:
        return _check_powershell()
    except Exception:
        return False


# Alias for external consumers
get_cu_tools = get
