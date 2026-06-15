# core/mascot/mascot_tray.ps1 — Worldwave Windows Tray v2
# Native Windows system tray with server control, dashboard, and status
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File mascot_tray.ps1
#
# Features:
#   - Live WW server status (color-coded icon)
#   - Server start/stop/restart from tray menu
#   - Open dashboard in browser
#   - Open config directory
#   - View recent logs
#   - Balloon notifications for state changes

param(
    [int]$Port = 9300,
    [int]$PollInterval = 3
)

$WW_URL = "http://localhost:$Port/ww/mascot/state"
$WW_DASHBOARD = "http://localhost:$Port/ww/mascot"
$ErrorActionPreference = "SilentlyContinue"

# ── Color State Mapping ──
$STATES = @{
    "idle"     = @{Color = [System.Drawing.Color]::FromArgb(123, 167, 188); Text = "Idle";           Emoji = [char]0x1F30A}
    "thinking" = @{Color = [System.Drawing.Color]::FromArgb(100, 180, 255); Text = "Thinking";       Emoji = [char]0x1F4AD}
    "happy"    = @{Color = [System.Drawing.Color]::FromArgb(123, 207, 140); Text = "Completed!";     Emoji = [char]0x2728}
    "sad"      = @{Color = [System.Drawing.Color]::FromArgb(232, 128, 112); Text = "Failed";         Emoji = [char]0x1F4A7}
    "excited"  = @{Color = [System.Drawing.Color]::FromArgb(255, 215, 0);   Text = "Amazing!";       Emoji = [char]0x1F389}
    "sleep"    = @{Color = [System.Drawing.Color]::FromArgb(150, 150, 150); Text = "Sleeping";       Emoji = [char]0x1F4A4}
    "error"    = @{Color = [System.Drawing.Color]::FromArgb(255, 107, 107); Text = "Error!";         Emoji = [char]0x1F4A5}
    "offline"  = @{Color = [System.Drawing.Color]::Gray;                    Text = "Offline";        Emoji = [char]0x274C}
}

# ── Load WinForms ──
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ── Generate State Icon (20x20 colored dot with highlight) ──
function New-StateIcon($color) {
    $bmp = New-Object System.Drawing.Bitmap 20, 20
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "HighQuality"
    $brush = New-Object System.Drawing.SolidBrush $color
    $g.FillEllipse($brush, 2, 2, 16, 16)
    $hl = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(80, 255, 255, 255))
    $g.FillEllipse($hl, 5, 4, 6, 6)
    $g.Dispose(); $brush.Dispose(); $hl.Dispose()
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

# ── Server control helpers ──
$WW_CLI = "$env:USERPROFILE\worldwave\ww_cli.py"
$PYTHON = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PYTHON) { $PYTHON = (Get-Command python3 -ErrorAction SilentlyContinue).Source }

function Invoke-WWCommand($action) {
    if ($PYTHON -and (Test-Path $WW_CLI)) {
        Start-Process $PYTHON -ArgumentList "`"$WW_CLI`" server $action" -WindowStyle Hidden -Wait
    }
}

function Test-WWServer {
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:$Port/ww/mascot/state" -UseBasicParsing -TimeoutSec 2
        return $true
    } catch { return $false }
}

# ── Build Menu ──
$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Text = "Worldwave — Connecting..."
$tray.Icon = New-StateIcon ([System.Drawing.Color]::Gray)
$tray.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

# Status header (non-clickable)
$statusLabel = New-Object System.Windows.Forms.ToolStripMenuItem("Worldwave v0.5")
$statusLabel.Enabled = $false
$statusLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$menu.Items.Add($statusLabel) | Out-Null

$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

# Server controls
$startItem = New-Object System.Windows.Forms.ToolStripMenuItem("▶ Start Server")
$startItem.add_Click({ Invoke-WWCommand "start" })
$menu.Items.Add($startItem) | Out-Null

$stopItem = New-Object System.Windows.Forms.ToolStripMenuItem("■ Stop Server")
$stopItem.add_Click({ Invoke-WWCommand "stop" })
$menu.Items.Add($stopItem) | Out-Null

$restartItem = New-Object System.Windows.Forms.ToolStripMenuItem("↻ Restart Server")
$restartItem.add_Click({ Invoke-WWCommand "restart" })
$menu.Items.Add($restartItem) | Out-Null

$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

# Tools
$dashboardItem = New-Object System.Windows.Forms.ToolStripMenuItem("🌐 Open Dashboard")
$dashboardItem.add_Click({ Start-Process $WW_DASHBOARD })
$menu.Items.Add($dashboardItem) | Out-Null

$configItem = New-Object System.Windows.Forms.ToolStripMenuItem("📁 Open Config")
$configItem.add_Click({ Start-Process "$env:USERPROFILE\.ww" })
$menu.Items.Add($configItem) | Out-Null

$logsItem = New-Object System.Windows.Forms.ToolStripMenuItem("📋 View Server Logs")
$logsItem.add_Click({ 
    if ($PYTHON -and (Test-Path $WW_CLI)) {
        Start-Process $PYTHON -ArgumentList "`"$WW_CLI`" logs 20" -WindowStyle Normal
    }
})
$menu.Items.Add($logsItem) | Out-Null

$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

# Exit
$exitItem = New-Object System.Windows.Forms.ToolStripMenuItem("Exit")
$exitItem.add_Click({
    $tray.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})
$menu.Items.Add($exitItem) | Out-Null

$tray.ContextMenuStrip = $menu

# Click to open dashboard
$tray.add_Click({
    if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        Start-Process $WW_DASHBOARD
    }
})

# ── Main Polling Loop ──
$lastState = ""
while ($true) {
    try {
        $resp = Invoke-WebRequest -Uri $WW_URL -UseBasicParsing -TimeoutSec 3
        $data = $resp.Content | ConvertFrom-Json
        $state = $data.state
        $msg = $data.message

        if ($state -ne $lastState) {
            $lastState = $state
            $info = $STATES[$state]
            if ($info) {
                $tray.Icon = New-StateIcon $info.Color
                $tray.Text = "Worldwave: $($info.Emoji) $($info.Text)"
                $statusLabel.Text = "WW — $($info.Text)"

                # Update menu enable/disable based on server state
                $isRunning = ($state -ne "offline")
                $startItem.Enabled = -not $isRunning
                $stopItem.Enabled = $isRunning
                $restartItem.Enabled = $isRunning
                $dashboardItem.Enabled = $isRunning
                $logsItem.Enabled = $isRunning

                # Notification for transient states
                if ($state -in @("happy", "sad", "error", "excited")) {
                    $tray.ShowBalloonTip(3000, "Worldwave", "$($info.Emoji) $($info.Text)", "Info")
                }
            }
        }
    } catch {
        if ($lastState -ne "offline") {
            $lastState = "offline"
            $info = $STATES["offline"]
            $tray.Icon = New-StateIcon $info.Color
            $tray.Text = "Worldwave: Offline"
            $statusLabel.Text = "WW — Offline"
            $startItem.Enabled = $true
            $stopItem.Enabled = $false
            $restartItem.Enabled = $false
            $dashboardItem.Enabled = $false
            $logsItem.Enabled = $false
        }
    }

    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Seconds $PollInterval
}
