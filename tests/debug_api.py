#!/usr/bin/env python3
"""Quick debug script"""
import json, urllib.request, sys

def api(method, path, data=None):
    url = f"http://localhost:9300{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.request.HTTPError as e:
        return {"http_error": e.code, "detail": e.read().decode()[:500]}
    except Exception as e:
        return {"error": str(e)}

# 1. Code
r = api("POST", "/ww/code", {"code": "x=42; print(f'Result: {x}')"})
print("CODE:", json.dumps(r, indent=2)[:400])
print()

# 2. Run
r2 = api("POST", "/ww/run", {"goal": "test", "max_spirals": 1})
print("RUN:", json.dumps(r2, indent=2)[:400])
print()

# 3. Memory
r3 = api("POST", "/ww/memory", {"action": "snapshot", "limit": 2})
print("MEMORY:", json.dumps(r3, indent=2)[:400])
print()

# 4. Interrupt
r4 = api("POST", "/ww/interrupt", {"reason": "test"})
print("INTERRUPT:", json.dumps(r4, indent=2)[:400])
