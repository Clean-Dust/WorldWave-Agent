#!/usr/bin/env python3
"""
ww — Worldwave CLI v0.4

Usage:
    ww                        Interactive chat mode
    ww <task>                 Execute a one-shot task
    ww init                   First-time setup wizard
    ww config [key] [val]     View/set configuration
    ww model [name]           View/switch model
    ww tools                  List available tools
    ww status                 System status
    ww server start|stop      Launch/stop HTTP server
    ww logs [N]               View logs
    ww delegate <goal>        Delegate sub-tasks
    ww gateway [action]       Gateway management
    ww pairing [action]       DM pairing — approve/reject user access
    ww memory <action>        Memory operations
    ww profile                Profile management
    ww help                   Show help

Environment Variables:
    WW_HOME      WW root directory (default: ~/worldwave)
    WW_CONFIG    WW config directory (default: ~/.ww)
"""

from __future__ import annotations
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from typing import Dict, Optional

# Load .env for API keys (before any WW imports)
try:
    from dotenv import load_dotenv
    _ww_home = os.environ.get("WW_HOME", os.path.expanduser("~/worldwave"))
    for _env_candidate in [
        os.path.join(_ww_home, ".env"),
        os.path.expanduser("~/.ww/.env"),
    ]:
        if os.path.exists(_env_candidate):
            load_dotenv(_env_candidate)
            break
except ImportError:
    pass

# ── Updater (background check for new versions) ──
sys.path.insert(0, os.environ.get("WW_HOME", os.path.expanduser("~/worldwave")))
try:
    from core.updater import check_for_update, perform_update, get_local_version, get_update_info

    _UPDATE_AVAILABLE = None  # cached notification string

    def _notify_if_update() -> None:
        """Check for update (throttled) and set _UPDATE_AVAILABLE."""
        global _UPDATE_AVAILABLE
        msg = check_for_update(force=False)
        if msg:
            _UPDATE_AVAILABLE = msg
except ImportError:
    def check_for_update(*a, **kw):
        return None
    def perform_update(*a, **kw):
        return {"success": False, "message": "Updater not available"}
    def get_local_version():
        return "?"
    def get_update_info():
        return {"update_available": False, "local_version": "?", "error": "Updater not available"}
    def _notify_if_update():
        pass
    _UPDATE_AVAILABLE = None

# ── Paths ──

def _detect_ww_home() -> str:
    """Detect WW_HOME from env var, script location, or fallback."""
    # 1. Env var override (highest priority)
    if "WW_HOME" in os.environ:
        return os.environ["WW_HOME"]

    # 2. Use the directory containing ww_cli.py — works both when run
    #    directly from a source checkout and when imported from site-packages
    if __file__:
        return os.path.dirname(os.path.abspath(__file__))

    # 3. Fallback (no __file__, e.g. interactive/embedded)
    return os.path.expanduser("~/worldwave")

WW_HOME = _detect_ww_home()
WW_CONFIG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
WW_SERVICE = os.path.expanduser("~/.config/systemd/user/ww.service")
WW_PORT = os.environ.get("WW_PORT", "9300")


# ── Colors ──

class Colors:
    """ANSI color codes (auto-disable on non-tty).

    Windows 10+ supports ANSI escape codes natively since build 16257.
    On older Windows, enable via SetConsoleMode on first use.
    """
    _enabled = sys.stdout.isatty()
    _windows_initialized = False

    @classmethod
    def _init_windows(cls):
        """Enable ANSI escape codes on Windows consoles."""
        if cls._windows_initialized or os.name != "nt":
            cls._windows_initialized = True
            return
        cls._windows_initialized = True
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # STD_OUTPUT_HANDLE = -11
            handle = kernel32.GetStdHandle(-11)
            if handle:
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                mode = ctypes.c_uint32()
                kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            # If kernel32 access fails (e.g. redirected output), disable colors
            cls._enabled = False

    @classmethod
    def disable(cls):
        cls._enabled = False

    @classmethod
    def _c(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls._enabled else text

    green = classmethod(lambda cls, t: cls._c("32", t))
    yellow = classmethod(lambda cls, t: cls._c("33", t))
    red = classmethod(lambda cls, t: cls._c("31", t))
    blue = classmethod(lambda cls, t: cls._c("34", t))
    cyan = classmethod(lambda cls, t: cls._c("36", t))
    bold = classmethod(lambda cls, t: cls._c("1", t))
    dim = classmethod(lambda cls, t: cls._c("2", t))


# ── Helpers ──

def load_config() -> Dict:
    config_file = os.path.join(WW_CONFIG, "config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(config: Dict):
    os.makedirs(WW_CONFIG, exist_ok=True)
    config_file = os.path.join(WW_CONFIG, "config.json")
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_or_create_api_key() -> str:
    """Load API key from config dir, or generate and persist a new one.

    Ensures CLI and server share the same key across multiple invocations.
    """
    key_file = os.path.join(WW_CONFIG, "api_key")
    if os.path.exists(key_file):
        try:
            with open(key_file) as f:
                key = f.read().strip()
            if key:
                os.environ["WW_API_KEY"] = key
                return key
        except Exception:
            pass

    import secrets
    key = secrets.token_urlsafe(32)
    os.makedirs(WW_CONFIG, exist_ok=True)
    with open(key_file, "w") as f:
        f.write(key)
    os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
    os.environ["WW_API_KEY"] = key
    return key


def ensure_server_running(timeout: float = 2.0) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(("127.0.0.1", int(WW_PORT)))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def auto_start_server() -> bool:
    """Start WW server in the background if not already running.
    Returns True once the server is reachable, False on timeout."""
    load_or_create_api_key()

    if ensure_server_running():
        return True

    server_script = os.path.join(WW_HOME, "server.py")
    if not os.path.exists(server_script):
        print(f"{Colors.red('✗')} Server script not found: {server_script}")
        return False

    print(f"{Colors.cyan('⟳')} Starting WW server...")

    # Try systemd first
    if os.path.exists(WW_SERVICE):
        subprocess.run(["systemctl", "--user", "start", "ww.service"],
                       capture_output=True, timeout=10)
        for _ in range(15):
            time.sleep(1)
            if ensure_server_running():
                print(f"{Colors.green('✓')} Server started (systemd)")
                return True

    # Fallback: direct Popen — inherits os.environ including WW_API_KEY
    subprocess.Popen(
        [sys.executable, server_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=WW_HOME,
    )
    for _ in range(15):
        time.sleep(1)
        if ensure_server_running():
            print(f"{Colors.green('✓')} Server started (port {WW_PORT})")
            return True

    print(f"{Colors.red('✗')} Server start timeout")
    return False


def check_llm_api_key() -> Optional[str]:
    """Check all possible LLM API key env vars, return first provider found or None."""
    for provider in ("DEEPSEEK", "OPENAI", "ANTHROPIC", "OPENROUTER", "CUSTOM"):
        if os.environ.get(f"{provider}_API_KEY"):
            return provider.lower()
    return None


def api_get(endpoint: str) -> Optional[Dict]:
    import urllib.request
    import urllib.error
    try:
        url = f"http://127.0.0.1:{WW_PORT}{endpoint}"
        headers = {}
        api_key = os.environ.get("WW_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def api_post(endpoint: str, data: Dict) -> Optional[Dict]:
    import urllib.request
    import urllib.error
    try:
        url = f"http://127.0.0.1:{WW_PORT}{endpoint}"
        body = json.dumps(data).encode()
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("WW_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=body,
            headers=headers,
            method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        print(f"{Colors.red('✗')} HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"{Colors.red('✗')} {e}")
        return None


# ── commands ──

def cmd_init(args):
    """First-time setup wizard — complete onboarding experience"""
    version = get_local_version()
    art_lines = [
        "                   __        __",
        "         ____  ____/ /_  ____/ /___ _   __",
        "        / __ \\/ __  / / / / __/ __ \\ | / /",
        "       / /_/ / /_/ / /_/ / /_/ /_/ / |/ /",
        "       \\____/\\__,_/\\__, /\\__/\\____/|___/",
        "                  /____/",
    ]
    art = "\n".join(Colors.cyan(l) for l in art_lines)
    art += "\n" + Colors.dim(f"  v{version} -- Autonomous AI Agent Framework")
    print(f"\n{art}\n")

    # 1. System info
    print(f"  {Colors.bold('System Information')}")
    py_ver = sys.version.split()[0]
    os_name = os.uname().sysname if hasattr(os, "uname") else "Windows" if os.name == "nt" else "?"
    arch = os.uname().machine if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE", "?")
    ww_home = os.environ.get("WW_HOME", os.path.expanduser("~/worldwave"))
    print(f"    Python:   {py_ver}")
    print(f"    Platform: {os_name} / {arch}")
    print(f"    WW Home:  {ww_home}")
    print()

    # 2. Check if already running
    if ensure_server_running():
        print(f"  {Colors.green('●')} WW server is already running on port {WW_PORT}")
        print(f"  {Colors.dim('(init skipped — server already configured)')}\n")
        return

    # 3. Config directory
    os.makedirs(WW_CONFIG, exist_ok=True)
    print(f"  {Colors.green('✓')} Config directory: {WW_CONFIG}")

    # 4. Data directory
    data_dir = os.path.join(ww_home, "data", "subconscious")
    os.makedirs(data_dir, exist_ok=True)
    print(f"  {Colors.green('✓')} Data directory: {data_dir}")

    # 5. API key check
    config = load_config()
    provider = config.get("provider", "")
    api_key = os.environ.get(f"{provider.upper()}_API_KEY" if provider else "DEEPSEEK_API_KEY", "")

    if not api_key:
        print(f"\n  {Colors.yellow('⚠')} No API key detected")
        print("  Edit your .env to add at least one provider:")
        print(f"    {Colors.dim('nano ' + os.path.join(ww_home, '.env'))}")
        print("  Supported: DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY")
        print()
    else:
        print(f"  {Colors.green('✓')} API key: {provider.upper()} configured")

    # 6. Test connection if key present
    if api_key:
        print(f"  {Colors.cyan('⟳')} Testing API connection...", end=" ", flush=True)
        try:
            sys.path.insert(0, ww_home)
            from core.llm import create_llm
            llm = create_llm()
            result = llm.chat("Respond with just: ok")
            if result and "ok" in str(result).lower():
                print(f"{Colors.green('✓')}")
            else:
                print(f"{Colors.yellow('⚠')}")
        except Exception as e:
            print(f"{Colors.yellow('⚠')} ({e})")

    # 7. Seed weights (subconscious pre-training)
    seed_file = os.path.join(data_dir, "model.json")
    if os.path.isfile(seed_file):
        size_kb = os.path.getsize(seed_file) / 1024
        print(f"  {Colors.green('✓')} Seed weights: {size_kb:.1f} KB (already exists)")
    else:
        print(f"  {Colors.cyan('⟳')} Generating subconscious seed weights...")
        try:
            sys.path.insert(0, ww_home)
            from scripts.pretrain_seed import main as pretrain_main
            # Override args to run quietly with defaults
            import argparse
            pa = argparse.Namespace(
                trees=20, samples=2000, depth=5,
                seed=42,
                output=seed_file
            )
            pretrain_main(pa)
            size_kb = os.path.getsize(seed_file) / 1024
            print(f"  {Colors.green('✓')} Seed weights generated: {size_kb:.1f} KB")
        except Exception as e:
            print(f"  {Colors.yellow('⚠')} Seed generation skipped ({e})")
            print(f"  {Colors.dim('    Run manually: python scripts/pretrain_seed.py')}")

    # 8. Profile check
    profiles_dir = os.path.join(WW_CONFIG, "profiles")
    profiles = [f[:-5] for f in sorted(os.listdir(profiles_dir))
                if f.endswith(".json")] if os.path.isdir(profiles_dir) else []
    if not profiles:
        os.makedirs(profiles_dir, exist_ok=True)
        with open(os.path.join(profiles_dir, "default.json"), "w") as f:
            json.dump({"model": "deepseek/deepseek-v4-flash",
                      "provider": "deepseek",
                      "profile_name": "default"}, f, indent=2)
        profiles = ["default"]
    print(f"  {Colors.green('✓')} Profile{'s' if len(profiles) > 1 else ''}: {', '.join(profiles)}")

    # 9. Dependency check
    deps_ok = True
    for dep in ["fastapi", "uvicorn", "pydantic", "httpx", "requests"]:
        try:
            __import__(dep)
        except ImportError:
            deps_ok = False
            print(f"  {Colors.yellow('⚠')} Missing dep: {dep} (pip install {dep})")
    if deps_ok:
        print(f"  {Colors.green('✓')} Dependencies: all core packages installed")

    # 10. Complete
    print(f"\n  {Colors.green(Colors.bold('═══ Setup Complete! ═══'))}\n")
    print(f"  {Colors.bold('Next Steps:')}")
    print(f"    {Colors.cyan('ww')}               Interactive chat mode")
    print( "    ww 'hello'          Run your first task")
    print(f"    {Colors.cyan('ww server start')}  Launch HTTP API server")
    print(f"    {Colors.cyan('ww help')}          See all commands")
    print()
    print(f"  {Colors.dim('Edit .env to add Telegram/Discord/SSH gateways:')}")
    print(f"    {Colors.dim(os.path.join(ww_home, '.env'))}")
    print()


def cmd_run(args):
    """Execute a task via WW server (auto-starts if needed).
    
    With a goal argument: one-shot task execution.
    Without a goal: interactive chat mode (REPL).
    """
    goal = " ".join(args.goal) if args.goal else ""
    max_spirals_default = getattr(args, "spirals", None) or 5
    effort = getattr(args, "reasoning_effort", None) or ""

    # Proactive update check
    _notify_if_update()
    if _UPDATE_AVAILABLE:
        print(f"{Colors.yellow(_UPDATE_AVAILABLE)}\n")

    # LLM API key pre-check — fail fast before starting server
    llm_provider = check_llm_api_key()
    if not llm_provider:
        ww_home_env = os.environ.get("WW_HOME", os.path.expanduser("~/worldwave"))
        print(f"\n  {Colors.yellow('⚠')} No LLM API key detected")
        print("  Set an API key via environment or .env file:")
        print(f"    {Colors.dim('export DEEPSEEK_API_KEY=sk-...')}")
        print(f"    {Colors.dim('  — OR edit .env: nano ' + os.path.join(ww_home_env, '.env'))}")
        print(f"  Get a free key: {Colors.dim('platform.deepseek.com → API Keys')}")
        print(f"  Supported: DEEPSEEK_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY, CUSTOM_API_KEY")
        print()
        return

    # Ensure API key is loaded (even if server is already running)
    load_or_create_api_key()

    # Ensure server is running
    if not auto_start_server():
        print(f"{Colors.red('✗')} Cannot start WW server")
        return

    # ── Interactive mode (no goal provided) ──
    if not goal:
        print(f"\n{Colors.cyan('═══ Worldwave ═══')}")
        print(f"Enter a goal, or type {Colors.yellow('/exit')} to exit\n")
        max_spirals = getattr(args, "spirals", None) or 3
        while True:
            try:
                line = input(f"{Colors.green('➤ ')}")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/help":
                print("  /exit to quit, /clear to clear context")
                continue
            if line == "/clear":
                print(f"{Colors.dim('Context cleared')}")
                continue
            print(f"{Colors.cyan('⟳')} Thinking...", end="", flush=True)
            payload = {"goal": line, "max_spirals": max_spirals}
            if effort:
                payload["reasoning_effort"] = effort
            result = api_post("/ww/run", payload)
            print("\r", end="", flush=True)
            if result:
                response = ""
                for r in result.get("results", []):
                    ev = r.get("evaluation", {})
                    if ev.get("response"):
                        response = ev["response"]
                        break
                if response:
                    print(f"\n{response}\n")
                else:
                    summary = result.get("summary", "") or str(result.get("status", "done"))
                    print(f"\n{summary}\n")
            else:
                print(f"\r{Colors.red('✗')} Server returned no response\n")
        return

    # ── One-shot mode ──
    max_spirals = getattr(args, "spirals", None) or max_spirals_default
    payload = {"goal": goal, "max_spirals": max_spirals}
    if effort:
        payload["reasoning_effort"] = effort
    result = api_post("/ww/run", payload)
    if result:
        status = result.get("status", "?")
        spirals = result.get("spirals_completed", 0)
        if status == "completed":
            print(f"\n{Colors.green('✓')} Task completed ({spirals} spirals)")
        else:
            print(f"\n{Colors.yellow('⚠')} Task {status} ({spirals} spirals)")
        for r in result.get("results", []):
            ev = r.get("evaluation", {})
            resp = ev.get("response", "") or ev.get("summary", "") or ev.get("reason", "")
            if resp:
                print(f"\n{resp}")
                break
        return

    print(f"{Colors.red('✗')} Task failed — server returned no response")


def cmd_config(args):
    """View/set configuration"""
    config = load_config()

    if args.profile:
        # Profile management
        profiles_dir = os.path.join(WW_CONFIG, "profiles")
        os.makedirs(profiles_dir, exist_ok=True)

        if args.profile_action == "list":
            profiles = [f[:-5] for f in sorted(os.listdir(profiles_dir))
                       if f.endswith(".json")] if os.path.isdir(profiles_dir) else []
            current = config.get("default_profile", "default")
            print(f"\n{Colors.bold('Profiles:')}\n")
            for p in profiles:
                mark = Colors.green("*") if p == current else " "
                print(f"  {mark} {Colors.cyan(p)}")
            if not profiles:
                print(f"  {Colors.dim('(no profiles)')}")
            print("\\n  Use: ww config profile create <name>")

        elif args.profile_action == "create":
            name = args.profile_name or "default"
            provider = config.get("provider", "deepseek")
            model = config.get("model", "deepseek/deepseek-v4-flash")
            profile = {"provider": provider, "model": model, "profile_name": name}
            path = os.path.join(profiles_dir, f"{name}.json")
            with open(path, "w") as f:
                json.dump(profile, f, indent=2)
            print(f"{Colors.green('✓')} Profile '{name}' created")

        elif args.profile_action == "switch":
            name = args.profile_name or "default"
            path = os.path.join(profiles_dir, f"{name}.json")
            if not os.path.isfile(path):
                print(f"{Colors.red('✗')} Profile '{name}' does not exist")
                return
            config["default_profile"] = name
            save_config(config)
            print(f"{Colors.green('✓')} Switched to profile '{name}'")

        elif args.profile_action == "delete":
            name = args.profile_name
            if not name:
                print(f"{Colors.red('✗')} Please specify the profile name to delete")
                return
            path = os.path.join(profiles_dir, f"{name}.json")
            if not os.path.isfile(path):
                print(f"{Colors.red('✗')} Profile '{name}' does not exist")
                return
            os.remove(path)
            if config.get("default_profile") == name:
                config["default_profile"] = "default"
                save_config(config)
            print(f"{Colors.green('✓')} Profile '{name}' deleted")
        return

    if args.set_key and args.set_value:
        # Set
        value = " ".join(args.set_value)
        try:
            parsed = json.loads(value)
            config[args.set_key] = parsed
        except (json.JSONDecodeError, TypeError):
            config[args.set_key] = value
        save_config(config)
        print(f"{Colors.green('✓')} {args.set_key} = {value}")

    elif args.set_key:
        # Get
        if args.set_key in config:
            val = config[args.set_key]
            print(json.dumps(val, indent=2, ensure_ascii=False)
                  if isinstance(val, (dict, list)) else val)
        else:
            # Try layered config
            try:
                sys.path.insert(0, WW_HOME)
                from core.config import ConfigManager
                cfg = ConfigManager()
                val = cfg.get(args.set_key)
                if val is not None:
                    print(json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else val)
                else:
                    print(f"{Colors.yellow('?')} {args.set_key} not set")
            except Exception:
                print(f"{Colors.yellow('?')} {args.set_key} not set")

    else:
        # List all
        if not config:
            print(f"\n{Colors.dim('(no custom config, using defaults)')}\n")

        # Try to show merged view from ConfigManager
        try:
            sys.path.insert(0, WW_HOME)
            from core.config import ConfigManager
            cfg = ConfigManager()
            all_config = cfg.all()
            print(f"\n{Colors.bold('WW Configuration (merged view):')}\n")
            for k, v in sorted(all_config.items()):
                if k == "available_keys":
                    continue
                val_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                marker = Colors.green("*") if k in config else Colors.dim(" ")
                print(f"  {marker} {Colors.cyan(k)} = {val_str}")
            print(f"\n{Colors.dim('  (*) = custom value')}")
        except Exception:
            # Fallback
            print(f"\n{Colors.bold('Configuration:')}\n")
            for k, v in config.items():
                val_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                print(f"  {Colors.cyan(k)} = {val_str}")


def cmd_model(args):
    """View/switch model"""
    config = load_config()

    if args.name:
        config["model"] = args.name
        model_lower = args.name.lower()
        if model_lower.startswith("claude"):
            config["provider"] = "anthropic"
        elif model_lower.startswith(("gpt", "o1", "o3")):
            config["provider"] = "openai"
        elif model_lower.startswith("deepseek"):
            config["provider"] = "deepseek"
        elif "/" in model_lower:
            config["provider"] = "openrouter"
        save_config(config)

        # Try API
        api_post("/ww/config/set", {"model": args.name})
        print(f"{Colors.green('✓')} Model switched to: {args.name}")
    else:
        model = config.get("model", "deepseek/deepseek-v4-flash")
        provider = config.get("provider", "deepseek")
        print(f"  Model: {Colors.bold(model)}")
        print(f"  Provider: {Colors.cyan(provider)}")

        if ensure_server_running():
            status = api_get("/ww/status")
            if status:
                providers = status.get("available_providers", [])
                if providers:
                    print(f"  Available: {', '.join(providers)}")


def cmd_tools(args):
    """List available tools"""
    if ensure_server_running():
        status = api_get("/ww/skills/list")
        if status and isinstance(status, dict):
            tools = status.get("tools", [])
            if tools:
                cats = {}
                for t in tools:
                    cat = t.get("category", "general")
                    cats.setdefault(cat, []).append(t["name"])
                print(f"\n{Colors.bold(f'Available tools ({len(tools)} )')}\n")
                for cat, names in sorted(cats.items()):
                    print(f"  {Colors.cyan(cat)} ({len(names)}):")
                    for n in names:
                        print(f"    • {n}")
                return

    tools_dir = os.path.join(WW_HOME, "tools")
    if os.path.isdir(tools_dir):
        files = sorted([f.replace(".py", "") for f in os.listdir(tools_dir)
                       if f.endswith(".py") and not f.startswith("_")])
        if files:
            print(f"\n{Colors.bold(f'Tool files ({len(files)} ):')}\n")
            for f in files:
                print(f"  • {f}")


def cmd_status(args):
    """System status"""
    print(f"\n{Colors.bold('═══ Worldwave System Status ═══')}\n")

    # Server
    running = ensure_server_running()
    print(f"  HTTP: {'● running' if running else '○ stopped'}  (port {WW_PORT})")

    # Config
    sys.path.insert(0, WW_HOME)
    try:
        from core.config import ConfigManager
        cfg = ConfigManager()
        model = cfg.get("model")
        provider = cfg.get("provider")
        print(f"  Model: {Colors.cyan(provider)}/{Colors.bold(model)}")
        profile = cfg.get("default_profile", "default")
        print(f"  Profile: {Colors.dim(profile)}")
    except Exception:
        config = load_config()
        model = config.get("model", "deepseek/deepseek-v4-flash")
        provider = config.get("provider", "deepseek")
        print(f"  Model: {Colors.cyan(provider)}/{Colors.bold(model)}")

    # API detail
    if running:
        status = api_get("/ww/status")
        if status:
            uptime = status.get("uptime", 0)
            spirals = status.get("total_spirals", 0)
            tasks = status.get("total_tasks", 0)
            if uptime:
                h, m, s = int(uptime // 3600), int((uptime % 3600) // 60), int(uptime % 60)
                print(f"  Uptime: {h}h {m}m {s}s")
            if spirals:
                print(f"  spirals: {spirals}")
            if tasks:
                print(f"  Task: {tasks}")
            providers = status.get("available_providers", [])
            if providers:
                print(f"  Providers: {', '.join(providers)}")

    # System
    total, used, free = shutil.disk_usage(os.path.expanduser("~"))
    print(f"  Disk: {free // (2**30)}G free / {total // (2**30)}G total")

    # Version
    version = get_local_version()
    print(f"  Version: {Colors.bold(version)}")

    # Update notification
    if _UPDATE_AVAILABLE:
        msg = '📦 Update available! Run "ww update" to upgrade'
        print(f"  {Colors.yellow(msg)}")
    print()


def cmd_server(args):
    """launch/stop HTTP server"""
    if args.action == "start":
        # Proactive update check on server start
        _notify_if_update()
        running = ensure_server_running()
        if running:
            print(f"  {Colors.green('●')} Server is already running")
            return

        # Try systemd first
        if os.path.exists(WW_SERVICE):
            result = subprocess.run(
                ["systemctl", "--user", "start", "ww.service"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"  {Colors.green('✓')} Server started (systemd)")
                return

        # Direct
        print(f"  {Colors.cyan('⟳')} launching server...")
        server_script = os.path.join(WW_HOME, "server.py")
        if os.path.exists(server_script):
            proc = subprocess.Popen(
                [sys.executable, server_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=WW_HOME,
            )
            # Track PID for clean shutdown
            pid_file = os.path.join(WW_CONFIG, "server.pid")
            try:
                with open(pid_file, "w") as f:
                    f.write(str(proc.pid))
            except OSError:
                pass
            for _ in range(10):
                time.sleep(1)
                if ensure_server_running():
                    print(f"  {Colors.green('✓')} Server started (port {WW_PORT})")
                    return
            print(f"  {Colors.red('✗')} Server start timeout")

    elif args.action == "stop":
        if os.path.exists(WW_SERVICE):
            subprocess.run(["systemctl", "--user", "stop", "ww.service"], capture_output=True)
        else:
            # Try tracked PID file first (cross-platform), then pkill/taskkill
            pid_file = os.path.join(WW_CONFIG, "server.pid")
            killed = False
            if os.path.exists(pid_file):
                try:
                    with open(pid_file) as f:
                        pid = int(f.read().strip())
                    import signal
                    os.kill(pid, signal.SIGTERM)
                    killed = True
                except (ProcessLookupError, OSError, ValueError):
                    pass
                try:
                    os.unlink(pid_file)
                except OSError:
                    pass
            if not killed:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/FI",
                                    "WINDOWTITLE eq worldwave*"], capture_output=True)
                else:
                    subprocess.run(["pkill", "-f", "server.py"], capture_output=True)
        print(f"  {Colors.yellow('○')} Server stopped")
    elif args.action == "restart":
        cmd_server(ArgsObj(action="stop"))
        time.sleep(2)
        cmd_server(ArgsObj(action="start"))

    elif args.action == "status":
        if ensure_server_running():
            print(f"  {Colors.green('●')} running (port {WW_PORT})")
        else:
            print(f"  {Colors.red('○')} stopped")


def cmd_update(args):
    """One-click update to latest version.

    Subcommands:
      ww update              Normal update (check → pull → reinstall)
      ww update status       Show version comparison
      ww update --dry-run    Preview incoming changes
    """
    global _UPDATE_AVAILABLE
    action = getattr(args, 'update_action', None)

    if action == "status":
        _cmd_update_status()
        return

    if action == "--dry-run":
        _cmd_update_dryrun()
        return

    # ── Normal update ──
    print(f"{Colors.cyan('⟳')} Checking for updates...")
    msg = check_for_update(force=True)
    if not msg:
        local_ver = get_local_version()
        print(f"{Colors.green('✓')} Worldwave {local_ver} is already up to date!")
        _UPDATE_AVAILABLE = None
        return

    print(f"  {msg}\n")
    print(f"{Colors.yellow('⟳')} Updating...")
    result = perform_update()
    if result["success"]:
        _UPDATE_AVAILABLE = None
        print(f"\n{Colors.green(result['message'])}")
    else:
        print(f"\n{Colors.red('✗')} {result['message']}")


def _cmd_update_status():
    """Show version status."""
    info = get_update_info()
    local_ver = info["local_version"]
    remote_ver = info.get("remote_version", "?")

    print(f"\n  {Colors.bold('Update Status')}\n")
    print(f"  Local:  {Colors.bold(local_ver)} ({info.get('local_head', '?')})")

    if info.get("error"):
        print(f"  Remote: {Colors.yellow(info['error'])}")
        print()
        return

    remote_str = Colors.green(remote_ver) if info.get("update_available") else Colors.dim(remote_ver)
    print(f"  Remote: {remote_str} ({info.get('remote_head', '?')})")

    if info.get("update_available"):
        print(f"  Behind: {Colors.yellow(info.get('behind', '?'))} commits")
        commits = info.get("commits", [])
        if commits:
            print(f"\n  {Colors.bold('Incoming changes:')}")
            for c in commits:
                print(f"    • {c}")
        print(f"\n  Run {Colors.cyan('ww update')} to upgrade.")
    else:
        print(f"  Status: {Colors.green('✓ Up to date')}")
    print()


def _cmd_update_dryrun():
    """Preview update without applying."""
    info = get_update_info()

    if info.get("error"):
        print(f"\n  {Colors.yellow(info['error'])}\n")
        return

    if not info.get("update_available"):
        print(f"\n  {Colors.green('✓')} Worldwave {info['local_version']} is up to date!\n")
        return

    print(f"\n  {Colors.bold('Update Preview')}\n")
    print(f"  Current: {Colors.bold(info['local_version'])} ({info['local_head']})")
    print(f"  Latest:  {Colors.green(info['remote_version'])} ({info['remote_head']})")
    print(f"  Behind:  {info['behind']} commit{'s' if info['behind'] != 1 else ''}\n")

    commits = info.get("commits", [])
    if commits:
        print(f"  {Colors.bold('Changes to be applied:')}")
        for c in commits:
            print(f"    • {c}")
    print(f"\n  Run {Colors.cyan('ww update')} to apply.")
    print()


def cmd_logs(args):
    """View logs"""
    n = args.n or 20

    # Try journalctl
    result = subprocess.run(
        ["journalctl", "--user", "-u", "ww.service", "-n", str(n),
         "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0 and result.stdout.strip():
        lines = result.stdout.strip().split("\n")
        for line in lines[-n:]:
            print(line)
        return

    log_file = os.path.join(WW_CONFIG, "server.log")
    if os.path.exists(log_file):
        with open(log_file) as f:
            lines = f.readlines()
        for line in lines[-n:]:
            print(line.rstrip())
    else:
        print(f"{Colors.yellow('?')} No logs available")


def cmd_delegate(args):
    """Delegate sub-tasks"""
    goal = " ".join(args.goal)

    result = api_post("/ww/run", {"goal": goal, "max_children": args.parallel or 3})
    if result:
        print(f"\n{Colors.bold('Sub-task results:')}")
        for r in result.get("results", []):
            icon = Colors.green("\u2713") if r.get("status") == "done" else Colors.red("\u2717")
            print(f"  {icon} [{r.get('task_id', '?')}] {r.get('goal', '')[:60]}")


def cmd_goal(args):
    """Goal Mode — autonomous background task execution.

    Commands:
        ww goal start <description>  — Start a new goal
        ww goal status [id]          — Show goal status (or list all)
        ww goal stop <id>            — Cancel a running goal
        ww goal list                 — List all goals (active + completed)
    """
    action = getattr(args, "action", "list")
    goal_id = getattr(args, "goal_id", "")

    try:
        from gateway.goal import GoalRunner
        runner = GoalRunner()
    except ImportError as e:
        print(f"  Failed to load GoalRunner: {e}")
        return

    if action == "start":
        goal_text = goal_id  # reused field — goal_id holds goal text for start
        if not goal_text:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww goal start <description>")
            return
        task_id = runner.submit(goal_text)
        print(f"  Goal started: {Colors.cyan(task_id)}")
        print(f"  {Colors.dim(goal_text[:80])}")

    elif action == "status":
        if goal_id:
            status = runner.get_status(goal_id)
            if status:
                print(f"\n{Colors.bold('Goal:')} {Colors.cyan(goal_id)}")
                for k, v in status.items():
                    print(f"  {k}: {v}")
            else:
                print(f"  {Colors.red(chr(0x2717))} Goal not found: {goal_id}")
        else:
            active = runner.list_active()
            if active:
                print(f"\n{Colors.bold('Active goals:')}\n")
                for g in active:
                    gid = g.get("task_id", "?")
                    phase = g.get("phase", "?")
                    goal_text = g.get("goal", "")[:60]
                    print(f"  {Colors.cyan(gid)}  [{phase}] {goal_text}")
            else:
                print(f"  {Colors.dim('(no active goals)')}")

    elif action == "stop":
        if not goal_id:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww goal stop <id>")
            return
        if runner.cancel(goal_id):
            print(f"  {Colors.green(chr(0x2713))} Goal cancelled: {goal_id}")
        else:
            print(f"  {Colors.red(chr(0x2717))} Goal not found or already finished: {goal_id}")

    elif action == "list":
        all_goals = runner.list_all()
        if all_goals:
            print(f"\n{Colors.bold('All goals:')}\n")
            for g in all_goals:
                gid = g.get("task_id", "?")
                phase = g.get("phase", g.get("status", "?"))
                goal_text = g.get("goal", "")[:60]
                icon = Colors.green(chr(0x25CF)) if phase in ("completed", "done") else Colors.yellow(chr(0x25CB))
                print(f"  {icon} {Colors.cyan(gid)}  [{phase}] {goal_text}")
        else:
            print(f"  {Colors.dim('(no goals)')}")

    else:
        print(f"  {Colors.yellow(chr(9888))} Unknown action: {action}")
        print(f"  Usage: ww goal [start|status|stop|list] [args...]")


def cmd_pairing(args):
    """DM pairing management — approve/reject/list pairing codes"""
    action = getattr(args, "action", "list")
    code = getattr(args, "code", "").upper()
    platform = getattr(args, "platform", "")
    CHECK = chr(0x2713)    # ✓
    CROSS = chr(0x2717)    # ✗
    CIRCLE = chr(0x25CB)   # ○

    try:
        from gateway.pairing import PairingManager
        pm = PairingManager()
    except ImportError as e:
        print(f"  Failed to load PairingManager: {e}")
        return

    if action == "list":
        pending = pm.list_pending()
        if pending:
            print(f"\n{Colors.bold('Pending pairing codes:')}\n")
            for p in pending:
                remaining = f"{int(p.expires_in // 60)}m" if p.expires_in < 3600 else f"{int(p.expires_in // 3600)}h"
                print(f"  {Colors.cyan(p.code)}  {p.display_name} ({p.platform}/{p.user_id})  expires in {remaining}")
        else:
            print(f"  {Colors.dim('(no pending codes)')}")

        whitelist = pm.list_whitelist()
        if whitelist:
            print(f"\n{Colors.bold('Whitelisted users:')}\n")
            for w in whitelist:
                print(f"  {Colors.green(CHECK)} {w.display_name} ({w.platform}/{w.user_id})")
            print()

    elif action == "approve":
        if not code:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww pairing approve <CODE>")
            return
        entry = pm.approve(code)
        if entry:
            print(f"  {Colors.green(CHECK)} Approved {entry.display_name} ({entry.platform}/{entry.user_id})")
        else:
            print(f"  {Colors.red(CROSS)} Invalid or expired code: {code}")

    elif action == "reject":
        if not code:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww pairing reject <CODE>")
            return
        result = pm.reject(code)
        if result:
            print(f"  {Colors.yellow(CIRCLE)} Rejected {result.display_name} ({result.platform}/{result.user_id})")
        else:
            print(f"  {Colors.red(CROSS)} Code not found: {code}")

    elif action == "remove":
        if not platform or not code:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww pairing remove <PLATFORM> <USER_ID>")
            return
        user_id = code  # second arg is user_id in this case
        if pm.remove_from_whitelist(platform, user_id):
            print(f"  {Colors.yellow(CIRCLE)} Removed {platform}/{user_id} from whitelist")
        else:
            print(f"  {Colors.red(CROSS)} {platform}/{user_id} not in whitelist")

    else:
        print(f"  {Colors.yellow(chr(9888))} Unknown action: {action}")
        print("  Actions: list, approve <CODE>, reject <CODE>, remove <PLATFORM> <USER_ID>")


def cmd_gateway(args):
    """Gateway management"""
    if args.action == "list":
        status = api_get("/ww/gateway/list") if ensure_server_running() else None
        if status and isinstance(status, dict):
            gateways = status.get("gateways", status)
            print(f"\n{Colors.bold('Gateway:')}\n")
            if isinstance(gateways, dict):
                for name, info in gateways.items():
                    running = info.get("running", False)
                    icon = Colors.green("●") if running else Colors.red("○")
                    platform = info.get("platform", name)
                    print(f"  {icon} {Colors.cyan(platform)}")
                    for k, v in info.items():
                        if k not in ("platform", "running"):
                            print(f"      {k}: {v}")
        else:
            print(f"  {Colors.dim('(no gateway)')}")

    elif args.action == "start":
        platform = args.platform or "telegram"
        result = api_post("/ww/gateway/start", {"platform": platform})
        if result:
            print(f"  {Colors.green('✓')} {platform} gateway started")
        else:
            print(f"  {Colors.red('✗')} Failed to start (server not running?)")

    elif args.action == "stop":
        platform = args.platform or "telegram"
        api_post("/ww/gateway/stop", {"platform": platform})
        print(f"  {Colors.yellow('○')} {platform} gateway stopped")


def cmd_memory(args):
    """Memory operations"""
    if not ensure_server_running():
        print(f"{Colors.yellow(chr(9888))} Need a running WW server")
        return

    if args.action == "stats":
        result = api_get("/ww/memory/stats")
        if result:
            print(f"\n{Colors.bold('Memory system stats:')}\n")
            for k, v in result.items():
                print(f"  {Colors.cyan(k)}: {v}")
        else:
            print(f"  {Colors.yellow('?')} Cannot get memory stats")

    elif args.action == "search":
        query = " ".join(args.query) if args.query else ""
        result = api_post("/ww/recall", {"query": query})
        if result:
            memories = result.get("memories", result.get("results", []))
            print(f"\n{Colors.bold('Memory search results:')}\n")
            for m in memories[:10]:
                if isinstance(m, dict):
                    print(f"  • {m.get('content', str(m))[:200]}")
                else:
                    print(f"  • {str(m)[:200]}")
        else:
            print(f"  {Colors.yellow('?')} No results")

    elif args.action == "sleep":
        result = api_post("/ww/sleep", {})
        if result:
            print(f"  {Colors.green('✓')} Memory consolidation triggered")
        else:
            print(f"  {Colors.yellow('?')} Trigger failed")


def cmd_mascot(args):
    """Launch/control the mascot"""
    import os
    action = getattr(args, 'action', 'open')

    if action == 'open':
        # Check if server is running
        running = ensure_server_running()
        if not running:
            print(f"{Colors.yellow('⚠')} WW server is not running. Run {Colors.cyan('ww server start')}")
            return

        # Open with default browser
        wsl = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "core", "mascot", "launcher.ps1")
        if os.path.exists(wsl) and os.path.exists(script):
            import subprocess
            subprocess.Popen([
                wsl, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", script,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"{Colors.green('🐋')} Opened (browser)")
        else:
            print(f"  Mascot URL: http://localhost:{WW_PORT}/ww/mascot")

    elif action == 'tray':
        # System tray mode -- no browser needed
        wsl = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "core", "mascot", "launcher.ps1")
        if os.path.exists(wsl) and os.path.exists(script):
            import subprocess
            subprocess.Popen([
                wsl, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", script, "-Tray",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"{Colors.green('🐋')} Tray started (system tray)")
        else:
            print(f"{Colors.yellow('⚠')} Tray mode only supported on Windows")

    elif action == 'state' and getattr(args, 'state_name', None):
        # ww mascot state happy / sad / thinking / idle
        from core.mascot import mascot
        mascot.set_state(args.state_name)
        print(f"  Mascot state set to: {Colors.cyan(args.state_name)}")

    elif action == 'state':
        # ww mascot state
        try:
            import requests
            r = requests.get(f"http://localhost:{WW_PORT}/ww/mascot/state", timeout=3)
            data = r.json()
            print(f"  Mascot: {Colors.cyan(data['state'])} — {data['message']}")
        except Exception:
            print(f"{Colors.yellow('⚠')} Cannot get state")

    else:
        print(f"{Colors.bold('🐋 🐋 WW Mascot')}")
        print("  Usage: ww mascot                     # Open window")
        print("        ww mascot open                 # Open window")
        print("        ww mascot state                # View current state")
        print("        ww mascot state <emotion>      # Set state")
        print("")
        print("  States: idle / thinking / happy / sad / excited / sleep / error")

def cmd_migrate(args):
    """Cross-generation migration: import configs from other AI agent systems."""
    try:
        from core.migrate import detect_and_list, migrate_source, SourceKind
    except ImportError as e:
        print(f"{Colors.red('✗')} Migration module not available: {e}")
        return

    action = getattr(args, 'action', 'scan')
    source = getattr(args, 'source', None)
    dry_run = getattr(args, 'dry_run', False)
    rollback_id = getattr(args, 'rollback', None)

    if rollback_id:
        from core.migrate import MigrationEngine
        engine = MigrationEngine()
        success = engine.rollback()
        if success:
            print(f"{Colors.green('✓')} Rolled back to snapshot: {rollback_id}")
        else:
            print(f"{Colors.red('✗')} Rollback failed")
        return

    if action == 'scan' or (action == 'migrate' and not source):
        # Scan environment
        print(f"\n  {Colors.bold('Scanning for AI agent installations...')}\n")
        detected = detect_and_list()

        if not detected:
            print(f"  {Colors.dim('No other AI agent systems detected.')}")
            return

        for d in detected:
            status = Colors.yellow('⚠ running') if d['running'] else Colors.green('idle')
            print(f"  {Colors.bold(d['source']):15} {Colors.cyan(str(d['items']))} items  {status}")
            if d['services']:
                for svc in d['services']:
                    print(f"    {Colors.dim('•')} {svc}")
            if d['warnings']:
                for w in d['warnings']:
                    print(f"    {Colors.yellow('⚠')} {w}")

        if action == 'scan':
            print(f"\n  {Colors.dim('Ready to migrate. Use: ww migrate <source>')}\n"
                  f"  {Colors.dim('Sources: openclaw, claude_code, hermes, codex')}")
        return

    if action == 'migrate' and source:
        print(f"\n  {Colors.bold(f'Migrating from {source}...')}\n")
        if dry_run:
            print(f"  {Colors.yellow('DRY RUN — no changes will be made')}\n")

        result = migrate_source(source, dry_run=dry_run)

        if result.success:
            print(f"  {Colors.green('✓')} Migration complete")
            print(f"    Items: {Colors.cyan(str(result.items_migrated))}")
            if result.snapshot_id:
                print(f"    Snapshot: {Colors.dim(result.snapshot_id)}")
            print(f"\n  {Colors.dim('Rollback: ww migrate --rollback ' + (result.snapshot_id or ''))}")
        else:
            print(f"  {Colors.red('✗')} Migration failed")
            for err in result.errors:
                print(f"    {Colors.red('•')} {err}")
        return

    print(f"\n  {Colors.bold('ww migrate')}")
    print("  Scan:       ww migrate [scan]")
    print("  Migrate:    ww migrate <openclaw|claude_code|hermes|codex>")
    print("  Dry run:    ww migrate <source> --dry-run")
    print("  Rollback:   ww migrate --rollback <snapshot-id>")


def cmd_help(args):
    """Show help"""
    print("""

  ww <command> [options]

  (no command)           Interactive chat mode
  <task>                 Execute a one-shot task
  init                   First-time setup wizard
  config [key] [val]     View/set configuration

  config profile create <name>  Create profile
  config profile switch <name>  Switch profile
  model [name]           View/switch model

  server start|stop      Explicit server control (usually not needed)
  status                 System status
  update                 One-click update (check + pull + reinstall)
  update status          Show version comparison
  update --dry-run       Preview incoming changes
  logs [N]               View logs

  tools                  List available tools
  delegate <goal>        Delegate sub-tasks
  gateway list|start|stop Gateway management
  memory stats|search|sleep  Memory operations
  mascot [open|tray|state]  Mascot (browser/tray)
  migrate [scan|<source>]   Cross-generation migration

  --home PATH            Specify WW path
  --no-color             Disable colors
  --effort LEVEL         Reasoning effort: low/medium/high/xhigh
  -h, --help             Show help
""")


# ── Parser ──

class ArgsObj:
    """Simple object for passing args."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Worldwave CLI",
        add_help=False,
    )
    parser.add_argument("--home", help="WW root directory")
    parser.add_argument("--no-color", action="store_true", help="Disable colors")
    parser.add_argument("-h", "--help", action="store_true", help="Show help")
    parser.add_argument("--compat", dest="compat_mode", help="Compatibility mode (claude, openclaw, hermes, codex)")
    parser.add_argument("--effort", dest="reasoning_effort", help="Reasoning effort: low/medium/high/xhigh")
    parser.add_argument("command", nargs="?", help="command")
    parser.add_argument("goal", nargs="*", help="Task goal")
    return parser


# ── command Map ──

def _build_sub_parsers():
    """Check args for subcommands and route appropriately."""
    parser = build_parser()
    return parser


COMMANDS = {
    "init": cmd_init,
    "config": cmd_config,
    "model": cmd_model,
    "tools": cmd_tools,
    "status": cmd_status,
    "server": cmd_server,
    "update": cmd_update,
    "logs": cmd_logs,
    "delegate": cmd_delegate,
    "goal": cmd_goal,
    "gateway": cmd_gateway,
    "pairing": cmd_pairing,
    "memory": cmd_memory,
    "mascot": cmd_mascot,
    "migrate": cmd_migrate,
    "run": cmd_run,
    "help": cmd_help,
}


def _maybe_apply_compat_alias():
    """If invoked via a known tool alias (claude, codex, etc.), rewrite argv.

    Called before arg parsing so compat-mode flags are transparent to the CLI.
    Sets WW_COMPAT_MODE env var for downstream consumers.
    """
    import sys
    try:
        from core.migrate.alias_layer import AliasLayer
        alias = AliasLayer()
        compat_mode, translated = alias.resolve_compat_mode(sys.argv)
        if compat_mode:
            # Rewrite argv: ["claude", "-p", "query"] → ["ww", "run", "--query", "query"]
            sys.argv = [sys.argv[0]] + translated
            os.environ["WW_COMPAT_MODE"] = compat_mode
            return compat_mode
    except ImportError:
        pass
    return None


def main():
    Colors._init_windows()  # Enable ANSI on Windows 10+

    # Check if invoked via compat alias (claude, codex, etc.)
    _compat_mode = _maybe_apply_compat_alias()

    parser = build_parser()
    args, extra = parser.parse_known_args()

    # Surface compat mode to command handlers
    if _compat_mode:
        args.compat_mode = _compat_mode

    if args.no_color:
        Colors.disable()

    if args.home:
        global WW_HOME
        WW_HOME = args.home

    if args.help:
        cmd_help(args)
        return
    
    if not args.command:
        # Bare 'ww' → interactive chat mode
        args.goal = []
        args.spirals = 3
        cmd_run(args)
        return

    cmd = args.command
    if cmd in ("server",):
        # server start/stop/restart/status
        action = extra[0] if extra else "status"
        args.action = action
        COMMANDS[cmd](args)

    elif cmd in ("update",):
        args.update_action = extra[0] if extra else None
        COMMANDS[cmd](args)

    elif cmd in ("config",):
        # config: subcommands or key/value
        if extra and extra[0] == "profile":
            args.profile = True
            args.profile_action = extra[1] if len(extra) > 1 else "list"
            args.profile_name = extra[2] if len(extra) > 2 else ""
            COMMANDS[cmd](args)
        else:
            args.profile = False
            args.set_key = extra[0] if extra else None
            args.set_value = extra[1:] if len(extra) > 1 else None
            COMMANDS[cmd](args)

    elif cmd in ("model",):
        args.name = extra[0] if extra else None
        COMMANDS[cmd](args)

    elif cmd in ("logs",):
        args.n = int(extra[0]) if extra else 20
        COMMANDS[cmd](args)

    elif cmd in ("gateway",):
        args.action = extra[0] if extra else "list"
        args.platform = extra[1] if len(extra) > 1 else None
        COMMANDS[cmd](args)

    elif cmd in ("pairing",):
        args.action = extra[0] if extra else "list"
        if args.action == "remove":
            args.platform = extra[1] if len(extra) > 1 else ""
            args.code = extra[2] if len(extra) > 2 else ""
        else:
            args.code = extra[1] if len(extra) > 1 else ""
        COMMANDS[cmd](args)

    elif cmd in ("memory",):
        args.action = extra[0] if extra else "stats"
        args.query = extra[1:] if len(extra) > 1 else []
        COMMANDS[cmd](args)

    elif cmd in ("delegate",):
        args.goal = extra
        args.parallel = 3
        COMMANDS[cmd](args)

    elif cmd in ("goal",):
        goal_args = getattr(args, 'goal', []) or []
        args.action = goal_args[0] if goal_args else "list"
        if args.action == "start":
            args.goal_id = " ".join(goal_args[1:]) if len(goal_args) > 1 else ""
        else:
            args.goal_id = goal_args[1] if len(goal_args) > 1 else ""
        COMMANDS[cmd](args)

    elif cmd in ("mascot",):
        args.action = extra[0] if extra else "open"
        if args.action == 'state' and len(extra) > 1:
            args.state_name = extra[1]
        COMMANDS[cmd](args)

    elif cmd in ("migrate",):
        # migrate: scan, migrate <source>, --dry-run, --rollback
        # Use args.goal (positional) since argparse nargs="*" consumes migrate args
        goal_args = getattr(args, 'goal', []) or []
        all_args = goal_args + (extra if extra else [])
        if goal_args and goal_args[0] in ("--rollback",):
            args.action = "migrate"
            args.rollback = goal_args[1] if len(goal_args) > 1 else None
        elif goal_args and goal_args[0] in ("openclaw", "claude_code", "hermes", "codex"):
            args.action = "migrate"
            args.source = goal_args[0]
            args.dry_run = "--dry-run" in all_args
        elif goal_args and goal_args[0] in ("--dry-run",):
            args.action = "migrate"
            args.source = goal_args[1] if len(goal_args) > 1 else None
            args.dry_run = True
        else:
            args.action = goal_args[0] if goal_args else "scan"
            args.source = goal_args[1] if len(goal_args) > 1 else None
        COMMANDS[cmd](args)

    elif cmd in ("tools",):
        cmd_tools(args)

    elif cmd in ("run",):
        args.spirals = None
        cmd_run(args)

    else:
        # Unrecognized command → treat everything as a task goal
        # e.g. 'ww write a script' → goal = "write a script"
        goal_words = [cmd]
        if extra:
            goal_words += extra
        elif args.goal:
            goal_words += args.goal
        args.goal = goal_words
        args.spirals = 5
        for i, e in enumerate(extra):
            if e == "--spirals" and i + 1 < len(extra):
                args.spirals = int(extra[i + 1])
        cmd_run(args)


if __name__ == "__main__":
    main()