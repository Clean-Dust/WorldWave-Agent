"""ww/core/config.py — Worldwave layered configuration system v0.3

Four-layer overlay (priority from low to high):
  1. DEFAULT_CONFIG    — Built-in default values in code
  2. User Config       — ~/.ww/config.json
  3. Profile Config    — ~/.ww/profiles/<name>.json
  4. Environment vars  — .env + os.environ (highest priority)

usage:
    config = ConfigManager()
    config.get("model")         # Read (including all overlay layers)
    config.get("memory_url")    # autoresolve
    config.set("model", "...")  # write user config
    config.all()                # Complete merged view
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import dotenv_values

logger = logging.getLogger(__name__)


# ── default values ────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "model": "deepseek/deepseek-v4-flash",
    "provider": "deepseek",
    "provider_base_url": "https://api.deepseek.com",
    "memory_enabled": True,
    # Subconscious referee/gate (BG + optional WM tie-break). Env: WW_SUBCONSCIOUS_ENABLED
    "subconscious_enabled": True,
    # Optional WM eviction tie-break from numeric risk only. Env: WW_WM_SUBCONSCIOUS_TIEBREAK (default 0/off)
    "wm_subconscious_tiebreak": False,
    "tools_enabled": True,
    "gateway_enabled": False,
    "server_port": 9300,
    "server_host": "0.0.0.0",
    "max_spirals": 10,
    "log_level": "INFO",
    # Sandbox: default ON. Set to false to run code directly on host.
    "sandbox_enabled": True,
    "sandbox_memory": "256m",
    "sandbox_cpu": "1.0",
    "sandbox_timeout": 30,
    "sandbox_network": "none",
}

# Keys that env/config strings should coerce to bool (WW_<KEY>).
_BOOL_CONFIG_KEYS = frozenset({
    "subconscious_enabled",
    "wm_subconscious_tiebreak",
    "memory_enabled",
    "tools_enabled",
    "gateway_enabled",
    "sandbox_enabled",
})

# Provider → default environment variable mapping
PROVIDER_ENV_MAP: Dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "custom": "CUSTOM_API_KEY",
}

ENV_PREFIX = "WW_"


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce config/env values to bool. Accepts 0/1, true/false, yes/no, on/off."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("", "0", "false", "no", "off", "n"):
        return False
    if s in ("1", "true", "yes", "on", "y"):
        return True
    return default


class ConfigManager:
    """Hierarchical configuration management. Supports four-layer overlay + env var override."""

    def __init__(self, home_dir: str = ""):
        self._home_dir = Path(
            home_dir or os.environ.get("WW_HOME", os.path.expanduser("~/worldwave"))
        )
        self._config_dir = Path(
            os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
        )
        self._profiles_dir = self._config_dir / "profiles"
        self._env_loaded = False
        self._env_cache: Dict[str, str] = {}
        self._user_config: Dict[str, Any] = {}
        self._defaults = dict(DEFAULT_CONFIG)

        # Ensure directories exist
        self._profiles_dir.mkdir(parents=True, exist_ok=True)

        # Load layers
        self._load_env()
        self._load_user_config()

        # Log loaded sources
        sources: List[str] = []
        if self._env_cache:
            sources.append("env")
        if self._user_config:
            sources.append(f"user config ({self._user_config_path()})")
        profile = self._user_config.get("default_profile", "default")
        if profile != "default":
            sources.append(f"profile '{profile}'")
        logger.info(
            "Config loaded from: %s",
            ", ".join(sources) if sources else "defaults only",
        )

    # ── environment variable load ─────────────────────────────────

    def _load_env(self):
        """Load .env files via python-dotenv, then overlay os.environ."""
        if self._env_loaded:
            return

        # Try .env files using python-dotenv for robust parsing
        # (handles quotes, comments, multiline values correctly)
        env_paths = [
            self._home_dir / ".env",
            Path.home() / ".ww" / ".env",
        ]
        for env_path in env_paths:
            if env_path.is_file():
                try:
                    raw = dotenv_values(env_path, encoding="utf-8")
                    values = {k: v for k, v in raw.items() if v is not None}
                    self._env_cache.update(values)
                except Exception:
                    logger.warning(
                        "Failed to load .env from %s", env_path, exc_info=True
                    )

        # OS environ overrides .env (higher priority)
        self._env_cache.update(
            {
                k: v
                for k, v in os.environ.items()
                if k.endswith("_API_KEY")
                or k.startswith("WW_")
                or k.startswith("TELEGRAM_")
            }
        )
        self._env_loaded = True

    # ── User Config (~/.ww/config.json) ───────────────

    def _user_config_path(self) -> str:
        return str(self._config_dir / "config.json")

    def _load_user_config(self):
        path = Path(self._user_config_path())
        if not path.is_file():
            return
        try:
            self._user_config = dict(json.loads(path.read_text()))
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in user config: %s", path)
        except OSError:
            logger.warning("Failed to read user config: %s", path)

    def _save_user_config(self):
        path = Path(self._user_config_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._user_config, indent=2))

    # ── core API ─────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Read configuration value (four-layer overlay + env override).

        Priority: environment variable > Profile > User Config > DEFAULT_CONFIG
        Bool keys (e.g. subconscious_enabled) coerce env strings via coerce_bool.
        """
        # 1. Check environment variable (WW_<KEY> or TELEGRAM_* or *_API_KEY)
        env_key = ENV_PREFIX + key.upper()
        if env_key in self._env_cache:
            return self._coerce_value(key, self._env_cache[env_key], default)
        if key == "api_key":
            # Auto-resolve provider's API key
            provider = self.get("provider", "deepseek")
            env_var = PROVIDER_ENV_MAP.get(provider, "")
            if env_var and env_var in self._env_cache:
                return self._env_cache[env_var]
        if key == "provider":
            # Check if we have any API keys set
            for prov, env_var in PROVIDER_ENV_MAP.items():
                if env_var in self._env_cache and self._env_cache[env_var]:
                    return prov

        # 2. Check profile config
        profile = self.active_profile()
        if profile != "default":
            profile_data = self.profile_get(profile) or {}
            if key in profile_data:
                return self._coerce_value(key, profile_data[key], default)

        # 3. Check user config
        if key in self._user_config:
            return self._coerce_value(key, self._user_config[key], default)

        # 4. Check defaults
        if key in self._defaults:
            return self._defaults[key]

        return default

    def _coerce_value(self, key: str, value: Any, default: Any = None) -> Any:
        """Coerce known bool config keys from env/json strings."""
        if key in _BOOL_CONFIG_KEYS:
            fb = default if isinstance(default, bool) else bool(
                self._defaults.get(key, False)
            )
            return coerce_bool(value, default=fb)
        return value

    def set(self, key: str, value: Any) -> bool:
        """Write configuration (store in User Config layer)."""
        self._user_config[key] = value
        self._save_user_config()
        return True

    def delete(self, key: str) -> bool:
        """Delete configuration item."""
        if key in self._user_config:
            del self._user_config[key]
            self._save_user_config()
            return True
        return False

    def all(self) -> Dict[str, Any]:
        """Return complete merged configuration view (including env override)."""
        merged = dict(self._defaults)
        merged.update(self._user_config)

        # Apply profile overrides
        profile = self.active_profile()
        if profile != "default":
            profile_data = self.profile_get(profile) or {}
            merged.update(profile_data)

        # Apply env overrides (bool keys coerced)
        for key in list(merged.keys()):
            env_key = ENV_PREFIX + key.upper()
            if env_key in self._env_cache:
                merged[key] = self._coerce_value(key, self._env_cache[env_key], merged.get(key))

        # Add API key from env
        provider = merged.get("provider", "deepseek")
        env_var = PROVIDER_ENV_MAP.get(provider, "")
        if env_var and env_var in self._env_cache:
            merged["api_key"] = self._env_cache[env_var]

        # Add all API keys for reference
        merged["available_keys"] = {
            prov: self._env_cache[ev]
            for prov, ev in PROVIDER_ENV_MAP.items()
            if ev in self._env_cache and self._env_cache[ev]
        }

        return merged

    def keys(self) -> List[str]:
        return list(self.all().keys())

    # ── Profile management ─────────────────────────────────

    def profile_path(self, name: str) -> str:
        if not name.endswith(".json"):
            name += ".json"
        return str(self._profiles_dir / name)

    def profile_list(self) -> List[str]:
        if not self._profiles_dir.is_dir():
            return []
        return sorted(
            [p.stem for p in self._profiles_dir.iterdir() if p.suffix == ".json"]
        )

    def profile_get(self, name: str) -> Optional[Dict[str, Any]]:
        path = Path(self.profile_path(name))
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in profile: %s", path)
            return None
        except OSError:
            logger.warning("Failed to read profile: %s", path)
            return None

    def profile_set(self, name: str, data: Dict[str, Any]) -> bool:
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        path = Path(self.profile_path(name))
        existing: Dict[str, Any] = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning(
                    "Could not read existing profile, starting fresh: %s", path
                )
            existing.update(data)
        else:
            existing = data

        # Ensure required fields
        existing.setdefault("model", self.get("model", "deepseek/deepseek-v4-flash"))
        existing.setdefault("provider", self.get("provider", "deepseek"))

        path.write_text(json.dumps(existing, indent=2))
        return True

    def profile_delete(self, name: str) -> bool:
        path = Path(self.profile_path(name))
        if path.is_file():
            path.unlink()
            if self._user_config.get("default_profile") == name:
                self._user_config["default_profile"] = "default"
                self._save_user_config()
            return True
        return False

    def profile_activate(self, name: str) -> bool:
        profile = self.profile_get(name)
        if profile is None and name != "default":
            return False
        self._user_config["default_profile"] = name
        self._save_user_config()
        return True

    def active_profile(self) -> str:
        return self._user_config.get("default_profile", "default")

    def active_profile_config(self) -> Dict[str, Any]:
        profile_name = self.active_profile()
        merged = self.all()
        if profile_name != "default":
            profile = self.profile_get(profile_name) or {}
            merged.update(profile)
        merged["profile_name"] = profile_name
        return merged

    # ── Utility tools ─────────────────────────────────────

    def env(self, key: str, default: str = "") -> str:
        """Read from env cache (including .env)."""
        return self._env_cache.get(key, default)

    def expand_path(self, path: str) -> str:
        """Expand ~, $HOME, $WW_HOME in paths."""
        path = path.replace("$WW_HOME", str(self._home_dir))
        path = path.replace("${WW_HOME}", str(self._home_dir))
        path = path.replace("$HOME", str(Path.home()))
        path = path.replace("${HOME}", str(Path.home()))
        return str(Path(path).expanduser())


def default_config() -> ConfigManager:
    return ConfigManager()
