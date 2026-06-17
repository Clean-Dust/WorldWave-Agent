"""
ww/core/credentials.py — Credential Pools v0.2

Enhanced with multi-key pools, automatic rotation on exhaustion/failure,
tiered key access, and health tracking.
"""

from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger("ww.credentials")


class KeyStatus(Enum):
    ACTIVE = "active"
    EXHAUSTED = "exhausted"      # Rate limit / quota exceeded
    FAILED = "failed"             # Auth failure (401/403)
    RATE_LIMITED = "rate_limited" # 429, temporary
    DISABLED = "disabled"         # Manually disabled


@dataclass
class PooledKey:
    """A single API key in a credential pool."""
    key: str
    label: str = ""
    status: KeyStatus = KeyStatus.ACTIVE
    provider: str = ""
    priority: int = 0  # Lower = preferred
    models: List[str] = field(default_factory=list)  # Models this key can access
    rate_limit_rpm: int = 0  # 0 = unknown
    last_used: float = 0
    error_count: int = 0
    total_calls: int = 0
    metadata: Dict = field(default_factory=dict)
    
    @property
    def is_available(self) -> bool:
        return self.status in (KeyStatus.ACTIVE, KeyStatus.RATE_LIMITED)
    
    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining in cooldown for rate-limited keys."""
        if self.status == KeyStatus.RATE_LIMITED:
            elapsed = time.time() - self.last_used
            remaining = 60 - elapsed  # Default 60s cooldown
            return max(0, remaining)
        return 0


@dataclass
class CredentialPool:
    """A pool of API keys for a single provider with automatic rotation."""
    provider: str
    keys: List[PooledKey] = field(default_factory=list)
    _current_index: int = 0
    _rotation_strategy: str = "round_robin"  # round_robin | priority | failover
    
    def add_key(self, key: str, label: str = "", priority: int = 0,
                models: List[str] = None, rate_limit_rpm: int = 0):
        """Add a key to the pool."""
        # Avoid duplicates
        for existing in self.keys:
            if existing.key == key:
                logger.warning(f"Key '{label}' already in pool {self.provider}")
                return existing
                
        pk = PooledKey(
            key=key,
            label=label or f"key-{len(self.keys)+1}",
            provider=self.provider,
            priority=priority,
            models=models or [],
            rate_limit_rpm=rate_limit_rpm,
        )
        self.keys.append(pk)
        # Sort by priority
        self.keys.sort(key=lambda k: k.priority)
        return pk
        
    def remove_key(self, label_or_index):
        """Remove a key by label or index."""
        if isinstance(label_or_index, str):
            self.keys = [k for k in self.keys if k.label != label_or_index]
        else:
            if 0 <= label_or_index < len(self.keys):
                self.keys.pop(label_or_index)
                
    def get_key(self, model: str = None) -> Optional[PooledKey]:
        """Get the next available key. Respects model access restrictions."""
        available = [k for k in self.keys if k.is_available]
        
        if not available:
            # Check if any rate-limited keys have cooled down
            for k in self.keys:
                if k.status == KeyStatus.RATE_LIMITED and k.cooldown_remaining == 0:
                    k.status = KeyStatus.ACTIVE
                    available.append(k)
                    
        if not available:
            logger.error(f"No available keys in pool {self.provider}")
            return None
            
        # Filter by model access if specified
        if model:
            model_accessible = [k for k in available if not k.models or model in k.models]
            if model_accessible:
                available = model_accessible
                
        if self._rotation_strategy == "priority":
            return available[0]  # Lowest priority number
            
        # Round-robin
        key = available[self._current_index % len(available)]
        self._current_index = (self._current_index + 1) % len(available)
        key.last_used = time.time()
        key.total_calls += 1
        return key
        
    def mark_exhausted(self, key_label: str):
        """Mark a key as exhausted (quota exceeded)."""
        for k in self.keys:
            if k.label == key_label:
                k.status = KeyStatus.EXHAUSTED
                k.error_count += 1
                logger.warning(f"Key '{key_label}' in pool {self.provider} marked exhausted")
                return
                
    def mark_failed(self, key_label: str):
        """Mark a key as failed (auth error)."""
        for k in self.keys:
            if k.label == key_label:
                k.status = KeyStatus.FAILED
                k.error_count += 1
                logger.error(f"Key '{key_label}' in pool {self.provider} marked failed")
                return
                
    def mark_rate_limited(self, key_label: str):
        """Mark a key as temporarily rate limited."""
        for k in self.keys:
            if k.label == key_label:
                k.status = KeyStatus.RATE_LIMITED
                k.last_used = time.time()
                logger.info(f"Key '{key_label}' in pool {self.provider} rate limited")
                return
                
    def reset_key(self, key_label: str):
        """Reset a key back to active."""
        for k in self.keys:
            if k.label == key_label:
                k.status = KeyStatus.ACTIVE
                k.error_count = 0
                return
                
    def reset_all(self):
        """Reset all keys to active."""
        for k in self.keys:
            k.status = KeyStatus.ACTIVE
            k.error_count = 0
            
    def health_report(self) -> Dict:
        """Generate a health report for the pool."""
        total = len(self.keys)
        active = sum(1 for k in self.keys if k.status == KeyStatus.ACTIVE)
        exhausted = sum(1 for k in self.keys if k.status == KeyStatus.EXHAUSTED)
        failed = sum(1 for k in self.keys if k.status == KeyStatus.FAILED)
        rate_limited = sum(1 for k in self.keys if k.status == KeyStatus.RATE_LIMITED)
        
        return {
            "provider": self.provider,
            "total_keys": total,
            "active": active,
            "exhausted": exhausted,
            "failed": failed,
            "rate_limited": rate_limited,
            "health_pct": (active / total * 100) if total > 0 else 0,
            "keys": [
                {
                    "label": k.label,
                    "status": k.status.value,
                    "calls": k.total_calls,
                    "errors": k.error_count,
                    "priority": k.priority,
                }
                for k in self.keys
            ],
        }


class CredentialManager:
    """Manages multiple credential pools across providers."""
    
    def __init__(self, storage_path: str = None, config_dir: str = None):
        self._pools: Dict[str, CredentialPool] = {}
        path = storage_path or config_dir
        if path:
            self._storage_path = os.path.join(path, "credentials.json") if os.path.isdir(path) else path
        else:
            self._storage_path = os.path.expanduser("~/.worldwave/credentials.json")
        
    def get_or_create_pool(self, provider: str) -> CredentialPool:
        """Get an existing pool or create a new one."""
        if provider not in self._pools:
            self._pools[provider] = CredentialPool(provider=provider)
        return self._pools[provider]
        
    def get_key(self, provider: str, model: str = None) -> Optional[str]:
        """Get an API key for a provider. Returns the key string."""
        pool = self._pools.get(provider)
        if pool:
            key = pool.get_key(model)
            return key.key if key else None
        # Fall back to environment variable
        env_var = f"{provider.upper()}_API_KEY"
        return os.getenv(env_var)
        
    def add_key(self, provider: str, key: str, label: str = "", **kwargs):
        """Add a key to a provider's pool."""
        pool = self.get_or_create_pool(provider)
        return pool.add_key(key, label=label, **kwargs)
        
    def handle_error(self, provider: str, key_label: str, status_code: int):
        """Handle an API error by marking the key appropriately."""
        pool = self._pools.get(provider)
        if not pool:
            return
            
        if status_code == 401 or status_code == 403:
            pool.mark_failed(key_label)
        elif status_code == 429:
            pool.mark_rate_limited(key_label)
        elif status_code == 402 or status_code == 429:
            pool.mark_exhausted(key_label)
            
    def health_report(self) -> Dict:
        """Generate health report for all pools."""
        return {
            "pools": {p: pool.health_report() for p, pool in self._pools.items()},
            "total_providers": len(self._pools),
        }
        
    def save(self):
        """Persist credential state to disk."""
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)
        data = {}
        for provider, pool in self._pools.items():
            data[provider] = {
                "keys": [
                    {
                        "label": k.label,
                        "status": k.status.value,
                        "priority": k.priority,
                        "models": k.models,
                        "total_calls": k.total_calls,
                        "error_count": k.error_count,
                        # NEVER save the actual key to disk
                    }
                    for k in pool.keys
                ],
                "rotation_strategy": pool._rotation_strategy,
            }
        with open(self._storage_path, 'w') as f:
            json.dump(data, f, indent=2)
            
    def load(self):
        """Load credential state from disk. Keys must be re-added separately."""
        if not os.path.isfile(self._storage_path):
            return
        try:
            with open(self._storage_path) as f:
                data = json.load(f)
            for provider, pool_data in data.items():
                pool = self.get_or_create_pool(provider)
                pool._rotation_strategy = pool_data.get("rotation_strategy", "round_robin")
                # Restore state for existing keys
                for key_data in pool_data.get("keys", []):
                    for k in pool.keys:
                        if k.label == key_data["label"]:
                            k.status = KeyStatus(key_data["status"])
                            k.total_calls = key_data.get("total_calls", 0)
                            k.error_count = key_data.get("error_count", 0)
        except Exception as e:
            logger.error(f"Failed to load credentials: {e}")


# Singleton
_credential_manager: Optional[CredentialManager] = None


def get_credential_manager() -> CredentialManager:
    global _credential_manager
    if _credential_manager is None:
        _credential_manager = CredentialManager()
        _credential_manager.load()
    return _credential_manager


# ── Backward-compat aliases ──

class CredentialStore:
    """Simple key-value credential store (backward-compat API).

    Maps (service, key) → value pairs with JSON persistence.
    Used by older code that expects set/get/list/delete methods.
    """

    def __init__(self, config_dir: str = None, storage_path: str = None):
        path = config_dir or storage_path or os.path.expanduser("~/.worldwave")
        if os.path.isdir(path):
            self._path = os.path.join(path, "credentials.json")
        else:
            self._path = path
        self._data: Dict[str, Dict[str, str]] = {}
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except Exception:
                pass

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def set(self, service: str, key: str, value: str):
        if service not in self._data:
            self._data[service] = {}
        self._data[service][key] = value
        self._save()

    def get(self, service: str, key: str, default: str = ""):
        return self._data.get(service, {}).get(key, default)

    def delete(self, service: str, key: str) -> bool:
        svc = self._data.get(service, {})
        if key in svc:
            del svc[key]
            self._save()
            return True
        return False

    def list_services(self) -> List[str]:
        return list(self._data.keys())

    def list_keys(self, service: str) -> List[str]:
        return list(self._data.get(service, {}).keys())


def mask_secret(value: str, show: int = 4) -> str:
    """Mask a secret, showing only the last N characters."""
    if not value:
        return ""
    if len(value) <= show * 2:
        return "*" * len(value)
    return "*" * (len(value) - show) + value[-show:]

def sanitize_output(text: str) -> str:
    """Remove secrets from output text."""
    import re
    patterns = [
        r'sk-[a-zA-Z0-9]{20,}',
        r'sk-or-[a-zA-Z0-9]{20,}',
        r'[a-zA-Z0-9]{32,}:[a-zA-Z0-9]{32,}',  # generic token:secret
    ]
    result = text
    for pat in patterns:
        result = re.sub(pat, "****", result)
    return result

def get_credential_store() -> CredentialManager:
    """Alias for get_credential_manager (backward compat)."""
    return get_credential_manager()
