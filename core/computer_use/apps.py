"""core/computer_use/apps.py — Application launcher

Manage Windows desktop application start, detection, focus switching.
"""

from __future__ import annotations
from typing import Optional

from core.computer_use import _ps


# Common application start command mapping table
APP_TABLE = {
    # browser
    "chrome": "Start-Process 'chrome.exe'",
    "chrome_incognito": "Start-Process 'chrome.exe' -ArgumentList '-incognito'",
    "edge": "Start-Process 'msedge.exe'",
    "firefox": "Start-Process 'firefox.exe'",
    "brave": "Start-Process 'brave.exe'",
    # developmenttool
    "vscode": "Start-Process 'code.exe'",
    "notepad": "Start-Process 'notepad.exe'",
    "notepad++": "Start-Process 'notepad++.exe'",
    "terminal": "Start-Process 'wt.exe'",  # Windows Terminal
    "cmd": "Start-Process 'cmd.exe'",
    "powershell_ise": "Start-Process 'powershell_ise.exe'",
    "sublime": "Start-Process 'sublime_text.exe'",
    # systemtool
    "explorer": "Start-Process 'explorer.exe'",
    "calculator": "Start-Process 'calc.exe'",
    "paint": "Start-Process 'mspaint.exe'",
    "snipping_tool": "Start-Process 'SnippingTool.exe'",
    "task_manager": "Start-Process 'taskmgr.exe'",
    "control_panel": "Start-Process 'control.exe'",
    "settings": "Start-Process 'ms-settings:'",
    "regedit": "Start-Process 'regedit.exe'",
    # Communication
    "slack": "Start-Process 'slack.exe'",
    "discord": "Start-Process 'discord.exe'",
    "teams": "Start-Process 'teams.exe'",
    "zoom": "Start-Process 'Zoom.exe'",
    "telegram": "Start-Process 'Telegram.exe'",
    "outlook": "Start-Process 'outlook.exe'",
}

# Alias mapping
ALIASES = {
    "google": "chrome",
    "browser": "chrome",
    "web": "chrome",
    "internet": "chrome",
    "vs": "vscode",
    "notepad++": "notepad_plusplus",
    "explorer": "explorer",
    "file": "explorer",
    "calc": "calculator",
    "ps": "powershell_ise",
    "wt": "terminal",
    "reg": "regedit",
    "Control Panel": "control_panel",
    "setting": "settings",
}


def _resolve_app(name: str) -> Optional[str]:
    """Resolve application name to PowerShell command."""
    name = name.lower().strip()
    # Direct match
    if name in APP_TABLE:
        return APP_TABLE[name]
    # Alias match
    if name in ALIASES:
        return APP_TABLE.get(ALIASES[name])
    # Partial match
    for key, cmd in APP_TABLE.items():
        if name in key or key in name:
            return cmd
    return None


def launch(app_name: str, args: str = "") -> str:
    """Start application.

    Args:
        app_name: Application name (supports aliases, fuzzy matching)
        args: Additional command line parameters (optional)

    Returns:
        State description

    Raises:
        ComputerUseError: Cannot find application
    """
    cmd = _resolve_app(app_name)
    if cmd is None:
        # Try to start directly (may be full path or custom name)
        _ps(f"Start-Process '{app_name}' {args}")
        return f"Launched '{app_name}'"

    full_cmd = cmd
    if args:
        full_cmd = cmd.rstrip("'") + f" {args}'"
    _ps(full_cmd)
    return f"Launched '{app_name}'"


def launch_url(url: str, browser: str = "chrome") -> str:
    """Open URL in browser."""
    _ps(f"Start-Process '{browser}.exe' -ArgumentList '--new-window {url}'")
    return f"Opened {url} in {browser}"


def focus_window(title: str) -> bool:
    """Will bring window with specified title to front."""
    try:
        _ps(f"""Add-Type @'
using System; using System.Runtime.InteropServices;
public class W {{ [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd); [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string w); }}
'@
$h = [W]::FindWindow($null, '{title}')
if ($h -ne [IntPtr]::Zero) {{ [W]::SetForegroundWindow($h); Write-Host 'OK' }} else {{ Write-Host 'NO' }}""")
        return True
    except Exception:
        return False


def list_running() -> list[dict]:
    """List running windows."""
    out = _ps('Get-Process | Where-Object { $_.MainWindowTitle } | Select-Object ProcessName, MainWindowTitle, Id | ConvertTo-Json -Compress')
    if not out:
        return []
    import json
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    return data


def open_file(path: str) -> str:
    """Open file with default application."""
    win_path = path.replace("/mnt/c/", "C:/").replace("/", "\\")
    _ps(f"Start-Process '{win_path}'")
    return f"Opened {path}"
