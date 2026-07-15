"""Worldwave update checker — proactive update notifications.

Periodically compares local HEAD with remote origin/main.
When an update is detected, stores the fact so any component
(CLI, server, gateway) can surface a notification to the user.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

# ── Paths ──
WW_HOME = os.environ.get("WW_HOME", os.path.expanduser("~/worldwave"))
WW_CONFIG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
LAST_CHECK_FILE = os.path.join(WW_CONFIG, "last_update_check")
CHECK_INTERVAL = 86400  # 24 hours (seconds)


def _git(*args: str) -> str | None:
    """Run a git command in the WW repo. Returns stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=WW_HOME,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _is_git_repo() -> bool:
    """Check if WW was installed via git (has a .git directory)."""
    return (Path(WW_HOME) / ".git").is_dir()


def _last_check_time() -> float:
    """Read the last check timestamp from disk."""
    try:
        with open(LAST_CHECK_FILE) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0.0


def _save_check_time() -> None:
    """Persist the current timestamp as last check time."""
    try:
        os.makedirs(os.path.dirname(LAST_CHECK_FILE), exist_ok=True)
        with open(LAST_CHECK_FILE, "w") as f:
            f.write(f"{time.time():.0f}\n")
    except OSError:
        pass


def get_local_version() -> str:
    """Return the version from version.txt, or '?' if unavailable."""
    try:
        with open(os.path.join(WW_HOME, "version.txt")) as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return "?"


def get_update_info() -> dict:
    """Return detailed info about update availability.

    Returns dict with:
    - update_available (bool)
    - local_version / remote_version (str)
    - behind (int, commits behind)
    - local_head / remote_head (str, short SHA)
    - commits (list of str, up to 20)
    - error (str, if applicable)
    """
    result = {
        "update_available": False,
        "local_version": get_local_version(),
        "remote_version": "?",
        "behind": 0,
        "local_head": "",
        "remote_head": "",
        "commits": [],
    }

    if not _is_git_repo():
        result["error"] = "Not a git repo"
        return result

    _git("fetch", "origin", "--depth=1", "-q")

    local_head = _git("rev-parse", "HEAD") or ""
    remote_head = _git("rev-parse", "origin/main") or ""
    result["local_head"] = local_head[:8] if local_head else ""
    result["remote_head"] = remote_head[:8] if remote_head else ""

    if remote_head:
        rv = _git("show", "origin/main:version.txt")
        result["remote_version"] = rv.strip() if rv else "newer"

    if not local_head or not remote_head:
        return result

    if local_head == remote_head:
        return result

    bc = _git("rev-list", "--count", f"{local_head}..origin/main")
    if bc and bc.isdigit():
        result["behind"] = int(bc)

    log_out = _git("log", "--oneline", f"{local_head}..origin/main")
    if log_out:
        result["commits"] = [l.strip() for l in log_out.split("\n") if l.strip()][:20]

    result["update_available"] = True
    return result


def check_for_update(force: bool = False) -> str | None:
    """Check if a newer version is available.

    Returns None if up-to-date or check skipped (within 24h throttle).
    Returns a human-readable message string if an update is available.
    """
    if not _is_git_repo():
        return None  # Installed via ZIP — can't auto-check

    # Throttle: skip if checked within 24h, unless forced
    if not force:
        elapsed = time.time() - _last_check_time()
        if elapsed < CHECK_INTERVAL:
            return None

    # Fetch latest remote info
    _save_check_time()

    local_head = _git("rev-parse", "HEAD")
    if not local_head:
        return None

    _git("fetch", "origin", "--depth=1", "-q")
    remote_head = _git("rev-parse", "origin/main")
    if not remote_head:
        return None

    if local_head == remote_head:
        return None  # Up-to-date

    # Count commits behind
    remote_count = _git("rev-list", "--count", f"{local_head}..origin/main")
    behind = int(remote_count) if remote_count and remote_count.isdigit() else 0
    if behind == 0:
        return None

    # Try to get latest version from remote
    remote_ver = _git("show", "origin/main:version.txt")
    remote_ver = remote_ver.strip() if remote_ver else "newer"
    local_ver = get_local_version()

    return (
        f"📦 Worldwave {remote_ver} available! "
        f"(you have {local_ver}, {behind} commit{'s' if behind != 1 else ''} behind)\n"
        f"   Type /update (chat) or: ww update (shell)"
    )


def perform_update() -> dict:
    """Pull the latest code and reinstall dependencies.

    Prefer shell parity with ``deploy.sh update`` when available (git reset,
    requirements.txt, reinstall ``~/.local/bin/ww``, optional server restart).
    Falls back to an in-process git pull + pip path when deploy.sh is missing.

    Returns dict with 'success' (bool) and 'message' (str).
    """
    if not _is_git_repo():
        return {
            "success": False,
            "message": "Not a git repo — cannot auto-update. Re-download from GitHub.",
        }

    deploy_sh = os.path.join(WW_HOME, "deploy.sh")
    if os.path.isfile(deploy_sh):
        try:
            result = subprocess.run(
                ["bash", deploy_sh, "update"],
                cwd=WW_HOME,
                timeout=300,
            )
            if result.returncode == 0:
                new_ver = get_local_version()
                return {
                    "success": True,
                    "message": (
                        f"✅ Updated to Worldwave {new_ver}! "
                        "Restart chat if needed: /exit then ww "
                        "(or restart the server if it was not auto-restarted)."
                    ),
                }
            return {
                "success": False,
                "message": f"Update failed (deploy.sh exit {result.returncode}).",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "Update timed out."}
        except OSError as e:
            return {"success": False, "message": f"Could not run deploy.sh: {e}"}

    return _perform_update_inline()


def _perform_update_inline() -> dict:
    """In-process update when deploy.sh is unavailable."""
    old_head = _git("rev-parse", "HEAD")

    # Fetch and pull
    _git("fetch", "origin", "--depth=1", "-q")
    pull_out = _git("pull", "--rebase", "origin", "main")
    if pull_out is None:
        _rollback(old_head)
        return {"success": False, "message": "Git pull failed — auto-rolled back."}

    # Count new commits
    new_ver = get_local_version()
    count = None
    if old_head:
        c = _git("rev-list", "--count", f"{old_head}..HEAD")
        if c and c.isdigit():
            count = int(c)

    # Reinstall dependencies (requirements.txt + editable install, like deploy.sh)
    venv_pip = os.path.join(WW_HOME, ".venv", "bin", "pip")
    if os.path.exists(venv_pip):
        try:
            req = os.path.join(WW_HOME, "requirements.txt")
            if os.path.isfile(req):
                result = subprocess.run(
                    [venv_pip, "install", "--quiet", "-r", req],
                    cwd=WW_HOME,
                    timeout=180,
                    capture_output=True,
                )
                if result.returncode != 0:
                    _rollback(old_head)
                    return {"success": False, "message": "Dependency install failed — auto-rolled back."}
            result = subprocess.run(
                [venv_pip, "install", "--quiet", "-e", "."],
                cwd=WW_HOME,
                timeout=120,
                capture_output=True,
            )
            if result.returncode != 0:
                _rollback(old_head)
                return {"success": False, "message": "Dependency install failed — auto-rolled back."}
        except (subprocess.TimeoutExpired, OSError):
            _rollback(old_head)
            return {"success": False, "message": "Dependency install error — auto-rolled back."}

    # Refresh ~/.local/bin/ww (parity with deploy.sh)
    try:
        src = os.path.join(WW_HOME, "bin", "ww")
        local_bin = os.path.expanduser("~/.local/bin")
        if os.path.isfile(src):
            os.makedirs(local_bin, exist_ok=True)
            dest = os.path.join(local_bin, "ww")
            shutil.copy2(src, dest)
            os.chmod(dest, 0o755)
    except OSError:
        pass

    parts = [f"✅ Updated to Worldwave {new_ver}"]
    if count and count > 0:
        parts.append(f"({count} new commit{'s' if count != 1 else ''})")
    return {
        "success": True,
        "message": " ".join(parts)
        + "! Restart chat if needed: /exit then ww "
        "(or: ww server restart if the server is still running old code).",
    }


def _rollback(old_head: str | None) -> None:
    """Rollback to old HEAD after a failed update."""
    if not old_head:
        return
    _git("reset", "--hard", old_head)
    venv_pip = os.path.join(WW_HOME, ".venv", "bin", "pip")
    if os.path.exists(venv_pip):
        try:
            subprocess.run(
                [venv_pip, "install", "--quiet", "-e", "."],
                cwd=WW_HOME, timeout=120, capture_output=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
