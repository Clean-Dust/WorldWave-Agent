#!/usr/bin/env python3
"""WW Memory multi-day soak (automated).

Daily job:
  1) Remember today's SOAK-{YYYYMMDD} via /ww/run + remember tool (no store cheat)
  2) Ask for all codes from the last N days (default 7)
  3) Write results to ~/.ww/memory_soak.jsonl and ~/.ww/memory_soak_state.json
  4) Exit 1 if any expected code is missing (so cron/systemd can alert)

Env:
  WW_PROVE_URL, WW_API_KEY  (required)
  WW_SOAK_DAYS=7
  WW_SOAK_DIR=~/.ww
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _http(base: str, key: str, path: str, body: dict, timeout: float = 240) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    base = os.environ.get("WW_PROVE_URL") or os.environ.get("WW_URL") or ""
    key = os.environ.get("WW_API_KEY", "")
    if not base or not key:
        print("FAIL: set WW_PROVE_URL and WW_API_KEY")
        return 2

    days = int(os.environ.get("WW_SOAK_DAYS", "7"))
    soak_dir = Path(os.environ.get("WW_SOAK_DIR", os.path.expanduser("~/.ww")))
    soak_dir.mkdir(parents=True, exist_ok=True)
    state_path = soak_dir / "memory_soak_state.json"
    log_path = soak_dir / "memory_soak.jsonl"

    today = date.today()
    code = f"SOAK-{today.isoformat().replace('-', '')}"
    key_name = "soak_code"

    # 1) plant today
    plant = _http(
        base,
        key,
        "/ww/run",
        {
            "goal": (
                f"Call remember tool: key={key_name} value={code}. "
                f"Also remember key=soak_{today.isoformat()} value={code}. "
                f"Reply REMEMBERED:{code}"
            ),
            "max_spirals": 5,
        },
    )
    plant_resp = str(plant.get("response") or "")
    plant_ok = code in plant_resp

    # 2) load previous expected codes
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    history = state.get("history", {})  # date_iso -> code
    history[today.isoformat()] = code

    # prune older than days
    cutoff = today - timedelta(days=days - 1)
    history = {d: c for d, c in history.items() if date.fromisoformat(d) >= cutoff}

    # 3) recall each
    results = {}
    all_ok = plant_ok
    for d, c in sorted(history.items()):
        ask = _http(
            base,
            key,
            "/ww/run",
            {
                "goal": (
                    f"What is soak_{d} or the soak code for {d}? "
                    f"If you know SOAK codes, list any you recall. "
                    f"Prefer exact value for soak_{d}. Reply with the code if known, else UNKNOWN."
                ),
                "max_spirals": 3,
            },
            timeout=180,
        )
        resp = str(ask.get("response") or "")
        hit = c in resp
        results[d] = {"code": c, "hit": hit, "response": resp[:200]}
        if not hit:
            all_ok = False

        # atom check
        search = _http(
            base,
            key,
            "/ww/memory",
            {"action": "search", "query": c, "limit": 5},
        )
        results[d]["atom_hit"] = c in json.dumps(search)

    # 4) persist
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "history": history,
        "last_plant_ok": plant_ok,
        "last_all_ok": all_ok,
        "last_results": results,
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False) + "\n")

    print(json.dumps({"plant_ok": plant_ok, "all_ok": all_ok, "results": results}, indent=2))
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
