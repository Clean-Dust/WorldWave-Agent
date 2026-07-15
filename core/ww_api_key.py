"""WW local HTTP API key (CLI ↔ server auth) — not LLM provider keys.

Two distinct key classes:
  1. LLM keys (DEEPSEEK_API_KEY, OPENAI_API_KEY, …) live in .env (gitignored).
     Updates must never wipe them.
  2. WW_API_KEY authenticates CLI/gateway → local HTTP API (/ww/*).
     Persisted under ~/.ww/api_key (or WW_CONFIG/api_key). Never auto-written
     into .env (avoids duplicate lines / corrupted comments).

Priority for resolve_ww_api_key():
  1. os.environ["WW_API_KEY"] (includes dotenv-loaded .env)
  2. Non-empty key file under config dir
  3. Generate secrets.token_urlsafe(32), write file mode 0600, set env
"""

from __future__ import annotations

import os
import secrets
import stat
from typing import Optional


def api_key_path(config_dir: Optional[str] = None) -> str:
    """Path to the persistent WW API key file."""
    base = config_dir or os.environ.get("WW_CONFIG") or os.path.expanduser("~/.ww")
    return os.path.join(base, "api_key")


def resolve_ww_api_key(config_dir: Optional[str] = None) -> str:
    """Load or create the local WW HTTP API key; always sets os.environ.

    Same source of truth for server.py and the CLI so restarts/updates
    do not desync and cause HTTP 401.
    """
    base = config_dir or os.environ.get("WW_CONFIG") or os.path.expanduser("~/.ww")
    key_file = os.path.join(base, "api_key")

    env_key = (os.environ.get("WW_API_KEY") or "").strip()
    if env_key:
        os.environ["WW_API_KEY"] = env_key
        try:
            file_key = ""
            if os.path.exists(key_file):
                with open(key_file) as f:
                    file_key = f.read().strip()
            if file_key != env_key:
                os.makedirs(base, exist_ok=True)
                with open(key_file, "w") as f:
                    f.write(env_key)
                os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return env_key

    if os.path.exists(key_file):
        try:
            with open(key_file) as f:
                key = f.read().strip()
            if key:
                os.environ["WW_API_KEY"] = key
                return key
        except OSError:
            pass

    key = secrets.token_urlsafe(32)
    os.makedirs(base, exist_ok=True)
    with open(key_file, "w") as f:
        f.write(key)
    os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
    os.environ["WW_API_KEY"] = key
    return key
