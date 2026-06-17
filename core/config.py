"""ww/core/config.py — Worldwave layered configuration system v0.2

Three-layer overlay (priority from low to high):
  1. DEFAULT_CONFIG — Built-in default values in code
  2. User Config     — ~/.ww/config.json
  3. Profile Config  — ~/.ww/profiles/<name>.json
  4. Environment variable — .env + os.environ (highest priority)

usage:
    config = ConfigManager()
    config.get("model")         # Read (including all overlay layers)
    config.get("memory_url")    # autoresolve
    config.set("model", "...")  # write user config
    config.all()                # Complete merged view
"""

from __future__ import annotations
import json
import os
from typing import Any, Dict, List, Optional


# ── defaultvalue ────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "model": "deepseek/deepseek-v4-flash",
    "provider": "deepseek",
    "provider_base_url": "https://api.deepseek.com",
    "memory_enabled": True,
    "subconscious_enabled": True,
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

# Provider → default environment variable mapping
PROVIDER_ENV_MAP: Dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "custom": "CUSTOM_API_KEY",
}

ENV_PREFIX = "WW_"


class ConfigManager:
    """Hierarchical configuration management. Supports three-layer overlay + env var override."""

    def __init__(self, home_dir: str = ""):
        self._home_dir = home_dir or os.environ.get(
            "WW_HOME", os.path.expanduser("~/worldwave")
        )
        self._config_dir = os.environ.get(
            "WW_CONFIG", os.path.expanduser("~/.ww")
        )
        self._profiles_dir = os.path.join(self._config_dir, "profiles")
        self._env_loaded = False
        self._env_cache: Dict[str, str] = {}
        self._user_config: Dict[str, Any] = {}
        self._defaults = dict(DEFAULT_CONFIG)

        # Ensure directories exist
        os.makedirs(self._profiles_dir, exist_ok=True)

        # Load layers
        self._load_env()
        self._load_user_config()

    # ── environment variableload ─────────────────────────────────

    def _load_env(self):
        """Load .env file and scan environment."""
        if self._env_loaded:
            return

        # Try .env in home dir
        env_paths = [
            os.path.join(self._home_dir, ".env"),
            os.path.join(os.path.expanduser("~"), ".ww", ".env"),
        ]
        for env_path in env_paths:
            if os.path.isfile(env_path):
                try:
                    with open(env_path) as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#") or "=" not in line:
                                continue
                            key, _, val = line.partition("=")
                            key = key.strip()
                            val = val.strip().strip("\"'")
                            self._env_cache[key] = val
                except OSError:
                    pass

        # OS environ overrides .env
        self._env_cache.update({
            k: v for k, v in os.environ.items()
            if k.endswith("_API_KEY") or k.startswith("WW_") or k.startswith("TELEGRAM_")
        })
        self._env_loaded = True

    # ── User Config (~/.ww/config.json) ───────────────

    def _user_config_path(self) -> str:
        return os.path.join(self._config_dir, "config.json")

    def _load_user_config(self):
        path = self._user_config_path()
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    self._user_config = {k: v for k, v in json.load(f).items()}
            except (json.JSONDecodeError, Exception):
                pass

    def _save_user_config(self):
        path = self._user_config_path()
        os.makedirs(self._config_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self._user_config, f, indent=2)

    # ── core API ─────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Read configuration value (three-layer overlay + env override).

        Priority: environment variable > Profile > User Config > DEFAULT_CONFIG
        """
        # 1. Check environment variable (WW_<KEY> or TELEGRAM_* or *_API_KEY)
        env_key = ENV_PREFIX + key.upper()
        if env_key in self._env_cache:
            return self._env_cache[env_key]
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
        profile = self._user_config.get("default_profile", "default")
        profile_data = self.profile_get(profile) if profile != "default" else {}
        if profile_data and key in profile_data:
            return profile_data[key]

        # 3. Check user config
        if key in self._user_config:
            return self._user_config[key]

        # 4. Check defaults
        if key in self._defaults:
            return self._defaults[key]

        return default

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
        profile = self._user_config.get("default_profile", "default")
        if profile != "default":
            profile_data = self.profile_get(profile) or {}
            merged.update(profile_data)

        # Apply env overrides
        for key in list(merged.keys()):
            env_key = ENV_PREFIX + key.upper()
            if env_key in self._env_cache:
                merged[key] = self._env_cache[env_key]

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
        return os.path.join(self._profiles_dir, name)

    def profile_list(self) -> List[str]:
        if not os.path.isdir(self._profiles_dir):
            return []
        return sorted([
            f[:-5] for f in os.listdir(self._profiles_dir)
            if f.endswith(".json")
        ])

    def profile_get(self, name: str) -> Optional[Dict[str, Any]]:
        path = self.profile_path(name)
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def profile_set(self, name: str, data: Dict[str, Any]) -> bool:
        os.makedirs(self._profiles_dir, exist_ok=True)
        path = self.profile_path(name)
        existing = {}
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                existing.update(data)
            except Exception:
                existing = data
        else:
            existing = data

        # Ensure required fields
        if "model" not in existing:
            existing["model"] = self.get("model", "deepseek/deepseek-v4-flash")
        if "provider" not in existing:
            existing["provider"] = self.get("provider", "deepseek")

        with open(path, "w") as f:
            json.dump(existing, f, indent=2)
        return True

    def profile_delete(self, name: str) -> bool:
        path = self.profile_path(name)
        if os.path.isfile(path):
            os.remove(path)
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
        """Expand ~ and $WW_HOME in paths."""
        path = path.replace("$WW_HOME", self._home_dir)
        path = path.replace("${WW_HOME}", self._home_dir)
        return os.path.expanduser(path)


def default_config() -> ConfigManager:
    return ConfigManager()
