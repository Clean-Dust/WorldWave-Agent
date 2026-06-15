"""core/computer_use/uia.py — Windows UIAutomation UI tree extraction

Extracts interactive elements and their coordinates from the active
window using Windows Automation API. Called via .ps1 temp file from WSL.

Provides the "Semantic Stream" (Tier 2+) — structured element data
that complements the pixel-based screenshot.

Output format:
[
    {
        "id": 1,
        "type": "button",
        "label": "Submit",
        "x": 100, "y": 200,
        "width": 80, "height": 30,
        "enabled": true,
        "automation_id": "submitBtn"
    },
    ...
]
"""

from __future__ import annotations
import json
import os
import subprocess
import tempfile


_PS_PATH = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

_UIA_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$global:props = @()
try {
    $focus = [System.Windows.Automation.AutomationElement]::FocusedElement
    if (-not $focus) { Write-Host '[]'; exit }

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $root = $walker.GetParent($focus)
    if (-not $root) { $root = $focus }
    while ($walker.GetParent($root)) { $root = $walker.GetParent($root) }

    function Walk-Tree($element, $depth) {
        if ($depth -gt 8) { return }
        $name = ""
        try { $name = $element.Current.Name } catch {}
        $ctrlType = ""
        try { $ctrlType = $element.Current.ControlType.ProgrammaticName } catch {}
        $aid = ""
        try { $aid = $element.Current.AutomationId } catch {}
        $enabled = $true
        try { $enabled = $element.Current.IsEnabled } catch {}
        $bb = $null
        try { $bb = $element.Current.BoundingRectangle } catch {}

        if ($bb -and $bb.Width -gt 0 -and $bb.Height -gt 0) {
            $typeName = $ctrlType -replace '^ControlType\.', ''
            $global:props += @{
                type = $typeName
                label = $name
                x = [int]$bb.Left
                y = [int]$bb.Top
                width = [int]$bb.Width
                height = [int]$bb.Height
                enabled = $enabled
                automation_id = $aid
            }
        }
        try {
            $child = $walker.GetFirstChild($element)
            while ($child) {
                Walk-Tree $child ($depth + 1)
                $child = $walker.GetNextSibling($child)
            }
        } catch {}
    }
    Walk-Tree $root 0
} catch {}

# De-duplicate by approximate position
$seen = @{}
$deduped = $global:props | Where-Object {
    $key = "$([math]::Floor($_.x / 10))-$([math]::Floor($_.y / 10))"
    if (-not $seen.ContainsKey($key)) { $seen[$key] = $true; $true }
    else { $false }
}

# Filter to reasonable element sizes
$filtered = $deduped | Where-Object {
    $_.width -le 2000 -and $_.height -le 2000 -and
    $_.width -gt 5 -and $_.height -gt 5
}

# Assign sequential IDs
$idx = 1
$result = $filtered | ForEach-Object {
    $_ | Add-Member -NotePropertyName id -NotePropertyValue $idx -PassThru
    $idx++
}

Write-Host ($result | ConvertTo-Json -Compress)
"""


# ── Public API ─────────────────────────────────────────────────────

def _run_ps_script(script_content: str, timeout: int = 30) -> str:
    """Write PowerShell script to temp file and execute it.

    Using temp file avoids bash escaping issues with complex inline scripts.
    """
    if not os.path.exists(_PS_PATH):
        raise RuntimeError("PowerShell not found")

    # Write to a temp .ps1 file on the Windows filesystem
    win_tmp = "C:\\Users\\Public\\ww_uia_temp.ps1"
    wsl_tmp = "/mnt/c/Users/Public/ww_uia_temp.ps1"
    with open(wsl_tmp, "w", encoding="utf-16-le") as f:
        # UTF-16 LE BOM for PowerShell
        f.write("\ufeff")
        f.write(script_content)

    try:
        r = subprocess.run(
            [_PS_PATH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win_tmp],
            capture_output=True, text=False, timeout=timeout,
        )
        stdout = r.stdout.decode("utf-8", errors="replace").strip()
        stderr = r.stderr.decode("utf-8", errors="replace").strip()

        if r.returncode != 0 and not stdout:
            # Script may have failed silently, try capturing stderr content
            if stderr:
                return ""
            return ""

        # Extract JSON from PowerShell output
        # PowerShell may prepend banner text or CLIXML errors
        lines = [l for l in stdout.split("\n") if l.strip() and not l.strip().startswith("{#")]
        # Find the JSON blob (starts with [)
        json_lines = [l for l in stdout.split("\n") if l.strip().startswith("[")]
        if json_lines:
            return json_lines[0].strip()
        return ""
    finally:
        try:
            os.unlink(wsl_tmp)
        except Exception:
            pass


def extract_elements() -> list[dict]:
    """Extract interactive UI elements from the active window.

    Returns:
        List of elements with id, type, label, x, y, width, height, enabled.
        Empty list if UIA fails or not on Windows.
    """
    if not os.path.exists(_PS_PATH):
        return []

    try:
        stdout = _run_ps_script(_UIA_SCRIPT)
        if not stdout:
            return []
        data = json.loads(stdout)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError, RuntimeError):
        return []


def get_interactive_elements(min_size: int = 10) -> list[dict]:
    """Get interactive elements filtered for likely clickability.

    Args:
        min_size: Minimum width/height in pixels

    Returns:
        Filtered element list
    """
    elements = extract_elements()
    return [
        e for e in elements
        if e.get("width", 0) >= min_size and e.get("height", 0) >= min_size
    ]


def get_active_window_title() -> str:
    """Get the active window's title."""
    script = r"""
Add-Type -AssemblyName UIAutomationClient
try {
    $f = [System.Windows.Automation.AutomationElement]::FocusedElement
    Write-Host $f.Current.Name
} catch {
    Write-Host ""
}
"""
    try:
        stdout = _run_ps_script(script)
        return stdout or ""
    except Exception:
        return ""

