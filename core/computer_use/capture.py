"""core/computer_use/capture.py — Screen capture engine

Two backends:
- GDI: Windows GDI+ via PowerShell (fallback, works everywhere)
- DXGI: MSS library (fast ~30ms, requires mss package)

Tier 0 (Basic) uses GDI.
Tier 1+ (Fast Capture) uses MSS/DXGI.
Tier 6 (Cerebellum) switches to video-level capture.

All backends return a PIL Image or numpy array for downstream processing.
"""

from __future__ import annotations
import os
import subprocess
import time
from typing import Optional, Tuple

from core.computer_use.config import get_config


# ── Backend detection ──────────────────────────────────────────

def _has_mss() -> bool:
    try:
        import mss
        return True
    except ImportError:
        return False


def _is_wsl() -> bool:
    """Detect if running under WSL."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower() or "wsl" in f.read().lower()
    except Exception:
        return False


def _powershell_available() -> bool:
    return os.path.exists("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def _ps(cmd: str, timeout: int = 30) -> str:
    """Run PowerShell command, return stdout."""
    ps_path = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    r = subprocess.run(
        [ps_path, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        raise RuntimeError(f"PowerShell error: {r.stderr or r.stdout}")
    return r.stdout.strip()


# ── GDI backend (Tier 0) ──────────────────────────────────────────

def _capture_gdi(save_path: str) -> str:
    """GDI+ screenshot via PowerShell. Slow (~200ms), no deps."""
    win_path = save_path.replace("/mnt/c/", "C:/").replace("/", "\\")
    _ps(
        'Add-Type -AssemblyName System.Windows.Forms; '
        'Add-Type -AssemblyName System.Drawing; '
        '$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; '
        '$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; '
        '$g = [System.Drawing.Graphics]::FromImage($bmp); '
        '$g.CopyFromScreen($b.X, $b.Y, 0, 0, $b.Size); $g.Dispose(); '
        f'$bmp.Save("{win_path}", [System.Drawing.Imaging.ImageFormat]::Png); '
        '$bmp.Dispose()',
        timeout=60
    )
    return save_path


# ── DXGI backend (Tier 1+) ─────────────────────────────────────────

def _capture_dxgi(save_path: str) -> str:
    """MSS screenshot using DXGI backend. Fast (~30ms)."""
    import mss
    with mss.mss() as sct:
        monitor = sct.monitors[0]  # Full virtual screen
        sct_img = sct.grab(monitor)
        mss.tools.to_png(sct_img.rgb, sct_img.size, output=save_path)
    return save_path


# ── Public API ─────────────────────────────────────────────────────

def screenshot(save_path: Optional[str] = None) -> str:
    """Capture full screen using the configured backend.

    On WSL: Always uses PowerShell GDI (mss can't capture Windows
    screen from inside WSL Linux). The tier setting controls other
    features (UIA, SoM, verification), not the capture method.

    On native Windows: Uses MSS/DXGI for fast capture when Tier 1+.

    Args:
        save_path: Output PNG path. Auto-generated if None.

    Returns:
        Absolute path to the saved screenshot.
    """
    if save_path is None:
        save_path = f"/tmp/cu_screen_{int(time.time() * 1000)}.png"

    cfg = get_config()

    # WSL cannot use mss for Windows screen capture
    if _is_wsl():
        if not _powershell_available():
            raise RuntimeError("PowerShell not found. Are you on Windows/WSL?")
        return _capture_gdi(save_path)

    # Native Windows or Linux with X11
    if cfg.capture_method == "dxgi" and _has_mss():
        return _capture_dxgi(save_path)
    else:
        if not _powershell_available():
            if _has_mss():
                return _capture_dxgi(save_path)
            raise RuntimeError(
                "No capture backend available. "
                "Install mss (pip install mss) or run on Windows/WSL."
            )
        return _capture_gdi(save_path)


def screenshot_pil() -> "PIL.Image":
    """Capture and return as PIL Image (in-memory, no file)."""
    import mss
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        sct_img = sct.grab(monitor)
        from PIL import Image
        return Image.frombytes("RGB", sct_img.size, sct_img.rgb)


def screen_size() -> Tuple[int, int]:
    """Get primary screen dimensions."""
    # WSL: must use PowerShell, mss can't see Windows screen
    if _is_wsl() or not _has_mss():
        out = _ps(
            'Add-Type -AssemblyName System.Windows.Forms; '
            '$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; '
            'Write-Host "$($b.Width) $($b.Height)"'
        )
        parts = out.split()
        return (int(parts[0]), int(parts[1]))
    else:
        import mss
        with mss.mss() as sct:
            m = sct.monitors[0]
            return (m["width"], m["height"])


def delta_pixels(before_path: str, after_path: str) -> float:
    """Compute fraction of pixels that changed between two screenshots.

    Used by Tier 4 (Visual Feedback) to verify actions.

    Args:
        before_path: Path to before-action screenshot
        after_path: Path to after-action screenshot

    Returns:
        Fraction of changed pixels (0.0 = identical, 1.0 = completely different)
    """
    from PIL import Image
    try:
        img1 = Image.open(before_path)
        img2 = Image.open(after_path)
    except Exception:
        return 1.0  # Can't compare, assume changed

    if img1.size != img2.size:
        return 1.0

    # Compare pixel-by-pixel using numpy if available
    try:
        import numpy as np
        arr1 = np.array(img1, dtype=np.int16)
        arr2 = np.array(img2, dtype=np.int16)
        diff = np.abs(arr1 - arr2).sum(axis=2)  # Sum across RGB
        changed = (diff > 30).sum()  # Tolerance for compression noise
        total = diff.size
        return changed / total
    except ImportError:
        # Pure PIL fallback
        changed = 0
        total = img1.size[0] * img1.size[1]
        pixels1 = img1.getdata()
        pixels2 = img2.getdata()
        for p1, p2 in zip(pixels1, pixels2):
            if abs(p1[0] - p2[0]) > 30 or abs(p1[1] - p2[1]) > 30 or abs(p1[2] - p2[2]) > 30:
                changed += 1
        return changed / total
