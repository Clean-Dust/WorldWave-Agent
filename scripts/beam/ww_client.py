"""Product-path WW client for BEAM ingest/probe (/ww/run only)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


def resolve_api_key() -> str:
    key = (os.environ.get("WW_API_KEY") or "").strip()
    if key:
        return key
    try:
        p = Path.home() / ".ww" / "api_key"
        if p.is_file():
            key = p.read_text(encoding="utf-8").strip()
            if key:
                os.environ["WW_API_KEY"] = key
                return key
    except Exception:
        pass
    try:
        from core.ww_api_key import resolve_ww_api_key

        return (resolve_ww_api_key() or "").strip()
    except Exception:
        return ""


class WWRunClient:
    """Thin HTTP client for product-honest /ww/run."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 240.0,
    ):
        self.base = (
            base_url
            or os.environ.get("WW_BEAM_URL")
            or os.environ.get("WW_PROVE_URL")
            or "http://127.0.0.1:9300"
        ).rstrip("/")
        self.key = api_key or resolve_api_key()
        self.timeout = timeout

    def run(
        self,
        goal: str,
        *,
        entity_id: str,
        platform: str = "beam",
        user_id: str = "",
        chat_id: str = "",
        max_spirals: int = 5,
    ) -> Dict[str, Any]:
        body = {
            "goal": goal,
            "max_spirals": max_spirals,
            "entity_id": entity_id,
            "platform": platform,
            "user_id": user_id or f"u_{entity_id}",
            "chat_id": chat_id or f"c_{entity_id}",
        }
        return self._post("/ww/run", body)

    def _post(self, path: str, body: dict) -> Dict[str, Any]:
        if not self.key:
            raise RuntimeError(
                "WW_API_KEY missing (set env or ~/.ww/api_key) for live WW path"
            )
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:500]
            raise RuntimeError(f"HTTP {e.code} {path}: {err}") from e
