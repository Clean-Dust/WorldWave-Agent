#Requires -Version 5.1
<#
.SYNOPSIS
    Worldwave — One-click install script for Windows (PowerShell)
.DESCRIPTION
    Installs Worldwave AI Agent Framework on native Windows 10/11.
    Handles Python detection, git clone, venv creation, dependency
    installation, and CLI setup — all in one command.
.PARAMETER InstallDir
    Installation directory (default: $env:USERPROFILE\worldwave)
.PARAMETER Branch
    Git branch to clone (default: main)
.PARAMETER NoPythonCheck
    Skip Python version check
.PARAMETER NoGit
    Skip git, download ZIP instead
.PARAMETER NoOnboard
    Skip post-install onboarding wizard
.PARAMETER DevMode
    Install from local directory instead of cloning from GitHub
.EXAMPLE
    iwr -useb https://raw.githubusercontent.com/Clean-Dust/worldwave/main/install.ps1 | iex
.EXAMPLE
    .\install.ps1 -InstallDir "D:\worldwave" -NoOnboard
#>

param(
    [string]$InstallDir = "$env:USERPROFILE\worldwave",
    [string]$Branch = "main",
    [switch]$NoPythonCheck,
    [switch]$NoGit,
    [switch]$NoOnboard,
    [switch]$DevMode
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# ── Colors ──
function Write-Info  { Write-Host "⟳ $args" -ForegroundColor Blue }
function Write-OK    { Write-Host "✓ $args" -ForegroundColor Green }
function Write-Warn  { Write-Host "⚠ $args" -ForegroundColor Yellow }
function Write-Err   { Write-Host "✗ $args" -ForegroundColor Red }
function Write-Header { Write-Host ""; Write-Host "══ $args ══" -ForegroundColor Cyan }

$RepoUrl = "https://github.com/Clean-Dust/worldwave.git"
$MinPython = "3.10"
$WWConfig = "$env:USERPROFILE\.ww"

# ── Banner ──
Write-Host @"

  ╭─────────────────────────────────────────────╮
  │                                             │
  │   Worldwave — Next-Gen Autonomous AI Agent │
  │   Native Windows Installer                 │
  │                                             │
  ╰─────────────────────────────────────────────╯

"@ -ForegroundColor Cyan

# ═══════════════════════════════════════════════════
# Pre-flight Checks
# ═══════════════════════════════════════════════════
Write-Header "Environment Check"

# Python detection — try all common names
$PythonExe = $null
foreach ($name in @("python3", "python", "py")) {
    try {
        $v = & $name --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $name
            break
        }
    } catch { }
}

if (-not $PythonExe) {
    Write-Err "Python not found. Install Python 3.10+ from https://python.org"
    Write-Info "Or install from Microsoft Store: python"
    exit 1
}

if (-not $NoPythonCheck) {
    $pyVer = (& $PythonExe --version 2>&1) -replace "Python ", ""
    $pyMajor = [int]($pyVer -split "\.")[0]
    $pyMinor = [int]($pyVer -split "\.")[1]
    if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 10)) {
        Write-Err "Need Python $MinPython+ (current: $pyVer)"
        exit 1
    }
    Write-OK "Python $pyVer"
}

# Git
$GitAvailable = $false
if (-not $NoGit) {
    try {
        $gitVer = & git --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            $GitAvailable = $true
            Write-OK "Git $gitVer"
        }
    } catch {
        Write-Warn "Git not found — will download ZIP instead"
    }
} else {
    Write-Warn "Git skipped (--NoGit)"
}

# pip
$PipAvailable = $false
try {
    $null = & $PythonExe -m pip --version 2>&1
    if ($LASTEXITCODE -eq 0) { $PipAvailable = $true }
    Write-OK "pip available"
} catch {
    Write-Warn "pip not available"
}

# System info
$osInfo = [System.Environment]::OSVersion
$arch = [System.Environment]::GetEnvironmentVariable("PROCESSOR_ARCHITECTURE")
Write-OK "System: Windows $($osInfo.Version.Major).$($osInfo.Version.Minor) / $arch"

# ═══════════════════════════════════════════════════
# Download Worldwave
# ═══════════════════════════════════════════════════
Write-Header "Downloading Worldwave"

if ($DevMode) {
    Write-Info "Dev mode — using current directory"
    $InstallDir = (Get-Location).Path
} elseif (Test-Path $InstallDir) {
    Write-Info "Directory exists: $InstallDir"
    if ($GitAvailable -and (Test-Path "$InstallDir\.git")) {
        Write-Info "Updating code..."
        Push-Location $InstallDir
        try { git pull origin $Branch 2>&1 | Out-Null } catch { }
        Pop-Location
    }
} else {
    if ($GitAvailable) {
        Write-Info "Cloning from GitHub..."
        git clone --depth 1 --branch $Branch $RepoUrl $InstallDir 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Clone failed, creating empty directory"
            New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        }
    } else {
        # Download ZIP
        $zipUrl = "https://github.com/Clean-Dust/worldwave/archive/refs/heads/$Branch.zip"
        $zipPath = "$env:TEMP\worldwave-$Branch.zip"
        Write-Info "Downloading ZIP from GitHub..."
        try {
            Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
            Expand-Archive -Path $zipPath -DestinationPath $env:TEMP -Force
            $extracted = "$env:TEMP\worldwave-$Branch"
            if (Test-Path $extracted) {
                Move-Item $extracted $InstallDir -Force
            }
            Remove-Item $zipPath -Force
        } catch {
            Write-Warn "ZIP download failed, creating empty directory"
            New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        }
    }
}
Write-OK "Install directory: $InstallDir"

# ═══════════════════════════════════════════════════
# Virtual Environment
# ═══════════════════════════════════════════════════
Write-Header "Python Virtual Environment"

$VenvDir = "$InstallDir\.venv"
$VenvPython = "$VenvDir\Scripts\python.exe"

if (Test-Path $VenvDir) {
    Write-Info "Virtual environment exists, upgrading pip..."
    try { & $VenvPython -m pip install --quiet --upgrade pip 2>&1 | Out-Null } catch { }
} else {
    Write-Info "Creating virtual environment..."
    & $PythonExe -m venv $VenvDir
    Write-OK "Virtual environment created"
}
Write-OK "Python: $(& $VenvPython --version 2>&1)"

# ═══════════════════════════════════════════════════
# Install WW Package
# ═══════════════════════════════════════════════════
Write-Header "Installing Worldwave"

Push-Location $InstallDir
try {
    if (Test-Path "pyproject.toml") {
        Write-Info "Installing WW package (editable mode)..."
        & $VenvPython -m pip install --quiet -e . 2>&1
    } else {
        Write-Info "Installing core dependencies..."
        & $VenvPython -m pip install --quiet fastapi uvicorn pydantic httpx requests 2>&1
    }
    Write-OK "Dependencies installed"
} catch {
    Write-Warn "Some dependencies may have failed — WW should still work"
} finally {
    Pop-Location
}

# Optional dependencies
Push-Location $InstallDir
try {
    if (Test-Path "core\subconscious\nostr.py") {
        & $VenvPython -m pip install --quiet websockets 2>&1 | Out-Null
    }
    if (Test-Path "tools\browser.py") {
        & $VenvPython -m pip install --quiet playwright 2>&1 | Out-Null
    }
} catch { }
Pop-Location

# ═══════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════
Write-Header "Initial Setup"

New-Item -ItemType Directory -Force -Path $WWConfig | Out-Null

# .env template
$EnvFile = "$InstallDir\.env"
if (-not (Test-Path $EnvFile)) {
    @"
# Worldwave Environment Configuration
# Fill in at least one LLM API key to get started

# LLM Provider (at least one required)
DEEPSEEK_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=

# Computer Use Vision (optional)
WW_VISION_API_KEY=
WW_VISION_PROVIDER=openrouter
WW_VISION_MODEL=qwen/qwen2.5-vl-72b-instruct:free

# Telegram Gateway (optional)
TELEGRAM_WW_TOKEN=
TELEGRAM_WW_WORKSPACE=

# Discord Gateway (optional)
DISCORD_BOT_TOKEN=

# Service Settings
WW_PORT=9300
WW_HOME=$InstallDir
WW_CONFIG=$WWConfig
WW_MEMORY_SLEEP_HOUR=3
WW_HIPPOCAMPUS_CAP=100
"@ | Out-File -FilePath $EnvFile -Encoding UTF8
    Write-OK ".env template created → fill in your API keys"
} else {
    Write-Warn ".env already exists, skipping"
}

# Default config
$ConfigFile = "$WWConfig\config.json"
if (-not (Test-Path $ConfigFile)) {
    New-Item -ItemType Directory -Force -Path "$WWConfig\profiles" | Out-Null
    @"
{
    "model": "deepseek/deepseek-v4-flash",
    "provider": "deepseek",
    "memory_enabled": true,
    "subconscious_enabled": true,
    "tools_enabled": true
}
"@ | Out-File -FilePath $ConfigFile -Encoding UTF8

    @"
{
    "provider": "deepseek",
    "model": "deepseek/deepseek-v4-flash",
    "profile_name": "default"
}
"@ | Out-File -FilePath "$WWConfig\profiles\default.json" -Encoding UTF8
    Write-OK "Default configuration created (profile: default)"
}

# ═══════════════════════════════════════════════════
# CLI Setup
# ═══════════════════════════════════════════════════
Write-Header "CLI Setup"

$LocalBin = "$env:USERPROFILE\.local\bin"
New-Item -ItemType Directory -Force -Path $LocalBin | Out-Null

$BatPath = "$LocalBin\ww.bat"
$PowershellWrapper = "$LocalBin\ww.ps1"

# Batch wrapper for CMD
@"
@echo off
set WW_HOME=$InstallDir
if exist "$VenvDir\Scripts\python.exe" (
    "$VenvPython" "$InstallDir\ww_cli.py" %*
) else if exist "$PythonExe" (
    $PythonExe "$InstallDir\ww_cli.py" %*
) else (
    echo Python not found
    exit 1
)
"@ | Out-File -FilePath $BatPath -Encoding ASCII

# PowerShell wrapper
@"
# ww.ps1 — Worldwave CLI wrapper
param([string[]]`$Args)

`$env:WW_HOME = "$InstallDir"
`$venvPython = "$VenvPython"
`$wwCli = "$InstallDir\ww_cli.py"

if (Test-Path `$venvPython) {
    & `$venvPython `$wwCli @Args
} else {
    & $PythonExe `$wwCli @Args
}
"@ | Out-File -FilePath $PowershellWrapper -Encoding UTF8

Write-OK "ww command installed → $BatPath"

# PATH check
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -notlike "*$LocalBin*") {
    [Environment]::SetEnvironmentVariable("PATH", "$userPath;$LocalBin", "User")
    Write-Warn "Added $LocalBin to user PATH (restart terminal to use 'ww')"
    $env:PATH = "$env:PATH;$LocalBin"
}

# ═══════════════════════════════════════════════════
# Windows Service (optional)
# ═══════════════════════════════════════════════════
Write-Header "Windows Startup (optional)"

$choice = "n"
if (-not $NoOnboard) {
    $choice = Read-Host "Create Scheduled Task for auto-start on login? (y/N)"
}

if ($choice -eq "y") {
    $taskName = "Worldwave Server"
    $taskExists = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

    if (-not $taskExists) {
        $action = New-ScheduledTaskAction -Execute $VenvPython -Argument "`"$InstallDir\server.py`"" -WorkingDirectory $InstallDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBattery -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
        Write-OK "Scheduled Task '$taskName' created — WW starts on login"
    } else {
        Write-Warn "Scheduled Task already exists, skipping"
    }
} else {
    Write-Info "Skipped — start manually with: ww server start"
}

# ═══════════════════════════════════════════════════
# Complete
# ═══════════════════════════════════════════════════
Write-Header "Installation Complete!"

Write-Host "  Install:  $InstallDir"
Write-Host "  Venv:     $VenvDir"
Write-Host "  Config:   $WWConfig"
Write-Host "  CLI:      $BatPath"
Write-Host ""

Write-Host "  Next Steps:" -ForegroundColor White
Write-Host "    1. Edit .env and fill in API keys"
Write-Host "       notepad $InstallDir\.env"
Write-Host ""
Write-Host "    2. Start the server"
Write-Host "       ww server start"
Write-Host ""
Write-Host "    3. Run your first task"
Write-Host "       ww run 'What can you do?'"
Write-Host ""
Write-Host "  Docs: github.com/Clean-Dust/worldwave"
Write-Host ""
Write-Host "  PowerShell one-liner to reinstall:" -ForegroundColor DarkGray
Write-Host "  iwr -useb https://raw.githubusercontent.com/Clean-Dust/worldwave/main/install.ps1 | iex"
Write-Host ""
