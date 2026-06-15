# core/mascot/launcher.ps1 — Fat Shark Mascot launcher
param(
    [string]$Url = "http://localhost:9300/ww/mascot",
    [switch]$AppMode,       # Floating window without toolbar (needs Edge or Chrome)
    [switch]$Tray,          # System tray mode (stays in background)
    [int]$Width = 360,
    [int]$Height = 440
)

try {
    if ($Tray) {
        # System tray mode — stays in system tray, no browser needed
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
        $trayScript = Join-Path $scriptDir "mascot_tray.ps1"
        if (Test-Path $trayScript) {
            # Use -WindowStyle Hidden to avoid popping up a PowerShell window
            Start-Process powershell.exe -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden",
                "-File", "`"$trayScript`""
            ) -WindowStyle Hidden
            Write-Host "🐋 Mascot tray started (check system tray)"
            exit 0
        }
        Write-Error "mascot_tray.ps1 not found"
        exit 1
    }

    if ($AppMode) {
        # Try Edge app mode (floating window without toolbar)
        $edgePath = Get-Command "msedge.exe" -ErrorAction SilentlyContinue
        if ($edgePath) {
            Start-Process "msedge.exe" -ArgumentList "--app=$Url --window-size=${Width},${Height}"
            Write-Host "🐋 Edge app mode"
            exit 0
        }
        # Try Chrome app mode
        $chromePath = Get-Command "chrome.exe" -ErrorAction SilentlyContinue
        if ($chromePath) {
            Start-Process "chrome.exe" -ArgumentList "--app=$Url --window-size=${Width},${Height}"
            Write-Host "🐋 Chrome app mode"
            exit 0
        }
    }

    # Default: open in your browser
    Start-Process $Url
    Write-Host "🐋 Mascot opened (default browser)"
}
catch {
    Write-Error "Failed: $_"
    exit 1
}
