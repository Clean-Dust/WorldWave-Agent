#!/usr/bin/env python3
"""WW E2E Test Runner — zero user involvement."""
import subprocess
import sys
import os
import json
import time
from pathlib import Path

WW_DIR = Path(__file__).resolve().parent.parent
PORT = 9302
BASE = f"http://localhost:{PORT}"
CHAT_ID = "-1003841986648"

def load_dotenv(path):
    d = {}
    if path.exists():
        for line in path.read_text().split(chr(10)):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def get_key():
    """Read API key from .env"""
    env = load_dotenv(WW_DIR / ".env")
    for k, v in env.items():
        if k.endswith("API_KEY"):
            return v
    return "localtest"

PASS = 0
FAIL = 0

def ok(n, d=""): 
    global PASS; PASS += 1
    print(f"  \033[32m✅\033[0m {n}{' — '+d if d else ''}")
def bad(n, d=""): 
    global FAIL; FAIL += 1
    print(f"  \033[31m❌\033[0m {n}{' — '+d if d else ''}")

def api(path, method="GET", data=None, timeout=30):
    cmd = ["curl", "-sf", "--max-time", str(timeout),
           "-H", "Authorization: Bearer " + get_key()]
    if method == "POST" and data:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json",
                "-d", json.dumps(data)]
    cmd.append(f"{BASE}{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
    return r.stdout.strip(), r.returncode

def tg(method, data=None, timeout=15):
    env = load_dotenv(WW_DIR / ".env")
    tok = env.get("TELEGRAM_WW_TOKEN", "")
    cmd = ["curl", "-sf", "--max-time", str(timeout)]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"https://api.telegram.org/bot{tok}/{method}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
    return r.stdout.strip(), r.returncode

def wait_server(timeout=30):
    for i in range(timeout):
        out, rc = api("/ww/health")
        if rc == 0: return True
        time.sleep(1)
    return False

def test_api():
    print("\n━━━ API Tests ━━━")
    out, rc = api("/ww/health")
    if rc == 0: ok("GET /ww/health")
    else: bad("GET /ww/health", f"rc={rc}")
    
    out, rc = api("/ww/status")
    if rc == 0: ok("GET /ww/status")
    else: bad("GET /ww/status", f"rc={rc}")
    
    out, rc = api("/ww/tools")
    if rc == 0:
        try:
            d = json.loads(out)
            tools = d.get("tools", d)
            n = len(tools) if isinstance(tools, list) else 0
            ok("GET /ww/tools", f"{n} tools")
        except:
            bad("GET /ww/tools", "bad json")
    else:
        bad("GET /ww/tools", f"rc={rc}")

def test_telegram():
    print("\n━━━ Telegram Tests ━━━")
    out, rc = tg("getMe")
    if rc == 0:
        try:
            d = json.loads(out)
            if d.get("ok"):
                ok("Bot getMe", f"@{d['result']['username']}")
            else:
                bad("Bot getMe", "not ok"); return
        except:
            bad("Bot getMe", "bad json"); return
    else:
        bad("Bot getMe", f"rc={rc}"); return
    
    out, rc = tg("sendMessage", {"chat_id": int(CHAT_ID), "text": "E2E test: say exactly PONG"})
    if rc == 0:
        d = json.loads(out)
        ok("Send msg", f"id={d.get('result',{}).get('message_id')}")
        print("  ⏳ Waiting for WW response...")
        for i in range(15):
            time.sleep(2)
            u, _ = tg("getUpdates", {"offset": -5, "timeout": 2})
            if "PONG" in u or "pong" in u:
                ok("WW response", "PONG received"); return
        bad("WW response", "no PONG in 30s")
    else:
        bad("Send msg", f"rc={rc}")

def main():
    global PASS, FAIL
    api_only = "--api-only" in sys.argv
    tg_only = "--telegram-only" in sys.argv
    
    mode = "Telegram" if tg_only else "API" if api_only else "Full"
    print(f"🌊 WW E2E — {mode}")
    
    if not tg_only:
        print("Starting WW server...")
        env = os.environ.copy()
        dotenv = load_dotenv(WW_DIR / ".env")
        env.update(dotenv)
        env.setdefault("WW_PORT", str(PORT))
        env.setdefault("WW_SKIP_AUTO_EVOLUTION", "true")
        env.setdefault("WW_PAIRING_AUTO_APPROVE", "true")
        proc = subprocess.Popen([sys.executable, "server.py"], env=env,
                                cwd=WW_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait_server():
                print("  ❌ Server failed to start"); proc.kill(); return 1
            if not tg_only:
                test_api()
            if not api_only:
                test_telegram()
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except: proc.kill()
    else:
        test_telegram()
    
    print(f"\n{'='*40}")
    print(f"Result: \033[32m{PASS} passed\033[0m / \033[31m{FAIL} failed\033[0m")
    return FAIL

if __name__ == "__main__":
    sys.exit(main())
