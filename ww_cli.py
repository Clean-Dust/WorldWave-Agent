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
    ww telegram status        Telegram bot config (token/workspace/pairing)
    ww memory <action>        Memory operations
    ww profile                Profile management
    ww help                   Show help

Environment Variables:
    WW_HOME      WW root directory (default: ~/worldwave)
    WW_CONFIG    WW config directory (default: ~/.ww)
"""

from __future__ import annotations
import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

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
    """Load API key with env-first priority so CLI matches a running server.

    Priority (shared with server via core.ww_api_key):
      1. WW_API_KEY already set (e.g. from .env via load_dotenv) — use it and
         rewrite ~/.ww/api_key if the file differs so future runs stay consistent.
      2. Non-empty key file under WW_CONFIG — load into env and return.
      3. Generate a new key, persist to file, set env, return.

    This is the local HTTP API key, not LLM provider keys in .env.
    """
    from core.ww_api_key import resolve_ww_api_key
    return resolve_ww_api_key(WW_CONFIG)


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

    print(f"{Colors.red('✗')} Server failed to start on port {WW_PORT}")
    print(f"  Check port:  ss -tlnp | grep {WW_PORT}   (or: lsof -i :{WW_PORT})")
    print(f"  Or run:      ww server start")
    return False


def check_llm_api_key() -> Optional[str]:
    """Check all possible LLM API key env vars, return first provider found or None.

    Empty strings and common placeholders count as missing.
    """
    _placeholders = {
        "",
        "sk-your-deepseek-key-here",
        "your-key-here",
        "sk-xxx",
        "changeme",
        "placeholder",
        "none",
        "null",
    }
    # (env_var, provider_id) — order matches failover preference roughly
    _key_vars = (
        ("DEEPSEEK_API_KEY", "deepseek"),
        ("OPENAI_API_KEY", "openai"),
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("OPENROUTER_API_KEY", "openrouter"),
        ("GEMINI_API_KEY", "gemini"),
        ("GOOGLE_API_KEY", "gemini"),
        ("XAI_API_KEY", "xai"),
        ("GROQ_API_KEY", "groq"),
        ("FIREWORKS_API_KEY", "fireworks"),
        ("TOGETHER_API_KEY", "together"),
        ("MISTRAL_API_KEY", "mistral"),
        ("MOONSHOT_API_KEY", "moonshot"),
        ("DEEPINFRA_API_KEY", "deepinfra"),
        ("OLLAMA_API_KEY", "ollama"),
        ("CUSTOM_API_KEY", "custom"),
    )
    for env_var, provider in _key_vars:
        val = (os.environ.get(env_var) or "").strip()
        if val and val not in _placeholders:
            return provider
    # Local Ollama without a key
    flag = (os.environ.get("WW_USE_OLLAMA") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return "ollama"
    if (os.environ.get("OLLAMA_BASE_URL") or "").strip() or (
        os.environ.get("OLLAMA_HOST") or ""
    ).strip():
        return "ollama"
    return None


def _warn_api_key_mismatch():
    """On 401: do not invent a new key; point user at restart with file key."""
    print(
        f"{Colors.yellow('⚠')} API key mismatch (HTTP 401). "
        f"Local server key may be out of sync with ~/.ww/api_key.\n"
        f"  Restart server so it reloads the key:  ww server restart"
    )


def api_get(endpoint: str) -> Optional[Dict]:
    import urllib.request
    import urllib.error
    try:
        url = f"http://127.0.0.1:{WW_PORT}{endpoint}"
        headers = {}
        api_key = os.environ.get("WW_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _warn_api_key_mismatch()
        return None
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
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(url, data=body,
            headers=headers,
            method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        print(f"{Colors.red('✗')} HTTP {e.code}: {body}")
        if e.code == 401:
            _warn_api_key_mismatch()
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

    llm_found = check_llm_api_key()
    if not api_key and not llm_found:
        print(f"\n  {Colors.yellow('⚠')} No API key detected")
        print("  Edit your .env to add at least one provider, or:")
        print(f"    {Colors.cyan('ww key set <key> [provider]')}")
        print(f"  See .env.example / {Colors.dim('ww key')} for the full provider list.")
        print()
    else:
        shown = (provider or llm_found or "deepseek").upper()
        print(f"  {Colors.green('✓')} API key: {shown} configured")

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


# Shared extractor — single source of truth with server + gateway
from core.public_reply import (  # noqa: E402
    extract_user_response,
    is_internal_response_text as _is_internal_response_text,
)


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
        print(f"\n  {Colors.yellow('⚠')} No LLM API key found")
        print(f"  Fix:  {Colors.cyan('ww key set <key> [provider]')}")
        print(f"  Providers: deepseek · openai · anthropic · openrouter · custom")
        print(f"  Example:  {Colors.dim('ww key set sk-xxx openai')}")
        print()
        return

    # Ensure API key is loaded (even if server is already running)
    load_or_create_api_key()

    # Ensure server is running
    if not auto_start_server():
        print(f"{Colors.red('✗')} Cannot start WW server")
        print(f"  Port {WW_PORT} may be busy, or check: ww server start")
        return

    # ── Interactive mode (no goal provided) ──
    if not goal:
        print(f"\n{Colors.cyan('═══ Worldwave ═══')}")
        print(
            f"Enter a goal, or type {Colors.yellow('/exit')} / "
            f"{Colors.yellow('/help')} / {Colors.yellow('/update')} / "
            f"{Colors.yellow('/gateway')}\n"
        )
        max_spirals = getattr(args, "spirals", None) or 3
        while True:
            try:
                line = input(f"{Colors.green('➤ ')}")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            # Normalize: strip whitespace + trailing CR (Windows paste / TTY)
            line = line.strip().rstrip("\r").strip()
            if not line:
                continue
            if is_chat_exit_command(line):
                print("Bye.")
                break
            if line == "/help":
                print(
                    "  /exit · /quit · /q  Leave chat (also: exit, quit, q)\n"
                    "  /clear              Clear context note\n"
                    "  /update             Upgrade Worldwave (not an LLM goal)\n"
                    "  /update status      Version comparison\n"
                    "  /update --dry-run   Preview incoming commits\n"
                    "  /gateway            Gateway status / setup (not an LLM goal)\n"
                    "  /gateway setup      Interactive Telegram gateway setup\n"
                    "  Also: update · upgrade · ww update · gateway · "
                    "ww gateway · /ww gateway\n"
                    "  Typos: close matches get Did you mean (no LLM)"
                )
                continue
            if line == "/clear":
                print(f"{Colors.dim('Context cleared')}")
                continue
            # Intercept update commands — never send as /ww/run goals
            update_action = parse_chat_update_command(line)
            if update_action is not None:
                handle_chat_update(update_action)
                continue
            # Intercept gateway commands — never send as /ww/run goals
            gateway_cmd = parse_chat_gateway_command(line)
            if gateway_cmd is not None:
                handle_chat_gateway(gateway_cmd[0], gateway_cmd[1])
                continue
            # Mistyped meta-commands — Did you mean (never spiral/LLM)
            chat_sugs = suggest_chat_commands(line)
            if chat_sugs is not None:
                print_chat_command_suggestions(line, chat_sugs)
                continue
            print(f"{Colors.cyan('⟳')} Thinking...", end="", flush=True)
            payload = {"goal": line, "max_spirals": max_spirals}
            if effort:
                payload["reasoning_effort"] = effort
            result = api_post("/ww/run", payload)
            print("\r", end="", flush=True)
            if result:
                response = extract_user_response(result)
                if response:
                    print(f"\n{response}\n")
                else:
                    print(f"\n{Colors.yellow('No reply text from server')}\n")
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
        response = extract_user_response(result)
        if response:
            print(f"\n{response}")
        else:
            print(f"\n{Colors.yellow('No reply text from server')}")
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
    """View/switch model (core). Bare `ww model` prompts for name on TTY."""
    config = load_config()
    name = getattr(args, "name", None)

    model = config.get("model", "deepseek/deepseek-v4-flash")
    provider = config.get("provider", "deepseek")
    print(f"  Current model: {Colors.bold(model)}")
    print(f"  Provider: {Colors.cyan(provider)}")
    if ensure_server_running():
        status = api_get("/ww/status")
        if status:
            providers = status.get("available_providers", [])
            if providers:
                print(f"  Available providers: {', '.join(providers)}")

    if not name:
        if sys.stdin.isatty():
            print()
            try:
                name = input(f"  {Colors.green('Model name?')} (Enter to keep current): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not name:
                print(f"  {Colors.dim('Unchanged.')}")
                return
        else:
            return

    config["model"] = name
    try:
        from core.transports.registry import infer_provider
        config["provider"] = infer_provider(name)
    except Exception:
        model_lower = name.lower()
        if model_lower.startswith("claude"):
            config["provider"] = "anthropic"
        elif model_lower.startswith(("gpt", "o1", "o3")):
            config["provider"] = "openai"
        elif model_lower.startswith("deepseek"):
            config["provider"] = "deepseek"
        elif model_lower.startswith("gemini"):
            config["provider"] = "gemini"
        elif model_lower.startswith("grok"):
            config["provider"] = "xai"
        elif "/" in model_lower:
            config["provider"] = "openrouter"
    save_config(config)

    result = api_post("/ww/model", {"model": name})
    if result and result.get("switched"):
        print(f"{Colors.green('✓')} {result['from']} → {result['to']}")
    else:
        print(f"{Colors.green('✓')} Config updated — restart to apply if server was offline")


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
        msg = "📦 Update available! Type /update (chat) or: ww update (shell)"
        print(f"  {Colors.yellow(msg)}")
    print()


def cmd_server(args):
    """launch/stop HTTP server"""
    if args.action == "start":
        # Proactive update check on server start
        _notify_if_update()
        # Ensure ~/.ww/api_key exists and env is set before spawn (matches server).
        load_or_create_api_key()
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

        # Direct — inherits WW_API_KEY from env (file-backed)
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


def is_chat_exit_command(line: str) -> bool:
    """True if *line* is a local REPL exit (never send to LLM / api_post).

    Accepted (case-insensitive; whitespace and trailing CR stripped):
      /exit · /quit · /q
      exit · quit · q
      fullwidth solidus forms: ／exit · ／quit · ／q
    """
    s = (line or "").strip().rstrip("\r").strip()
    if not s:
        return False
    lower = s.lower()
    # Normalize fullwidth solidus U+FF0F → ASCII slash
    if lower.startswith("\uff0f"):
        lower = "/" + lower[1:]
    if lower.startswith("/"):
        lower = lower[1:].lstrip()
    return lower in ("exit", "quit", "q")


def parse_chat_update_command(line: str) -> Optional[str]:
    """If *line* is an in-chat update command, return the action; else None.

    Accepted (case-insensitive, surrounding whitespace ignored):
      /update | update | ww update
      /upgrade | upgrade | ww upgrade   (alias of update)
      … status | … --dry-run | … dry-run

    Returns:
      ""          — full update
      "status"    — version comparison only
      "--dry-run" — preview only
      None        — not an update command (treat as LLM goal)
    """
    s = (line or "").strip().rstrip("\r").strip()
    if not s:
        return None
    lower = s.lower()
    # Normalize fullwidth solidus U+FF0F → ASCII slash
    if lower.startswith("\uff0f"):
        lower = "/" + lower[1:]
    # Optional leading "ww " (user typed shell form inside chat)
    if lower.startswith("ww "):
        lower = lower[3:].lstrip()
    # Optional slash command form
    if lower.startswith("/"):
        lower = lower[1:].lstrip()

    parts = lower.split()
    if not parts or parts[0] not in ("update", "upgrade"):
        return None
    if len(parts) == 1:
        return ""
    if parts[1] == "status" and len(parts) == 2:
        return "status"
    if parts[1] in ("--dry-run", "dry-run") and len(parts) == 2:
        return "--dry-run"
    # e.g. "update my docs" → not a CLI update; let the LLM handle it
    return None


def handle_chat_update(action: str) -> None:
    """Run update machinery from the interactive REPL (never via /ww/run)."""
    global _UPDATE_AVAILABLE

    if action == "status":
        _cmd_update_status()
        return
    if action == "--dry-run":
        _cmd_update_dryrun()
        return

    # Full update — same end state as shell `ww update` (deploy.sh preferred)
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
        print(
            f"  {Colors.dim('If this chat session feels stale:')} "
            f"{Colors.yellow('/exit')} {Colors.dim('then')} {Colors.cyan('ww')}"
        )
    else:
        print(f"\n{Colors.red('✗')} {result['message']}")


def parse_chat_gateway_command(line: str) -> Optional[tuple]:
    """If *line* is an in-chat gateway command, return (action, platform); else None.

    Accepted (case-insensitive, surrounding whitespace ignored):
      gateway | /gateway | ww gateway | /ww gateway
      … setup | list | start | stop
      start/stop may take an optional platform (e.g. telegram)

    Returns:
      ("", None)           — bare gateway (status if configured, else setup)
      ("setup", None)
      ("list", None)
      ("start", platform_or_None)
      ("stop", platform_or_None)
      None                 — not a gateway command (treat as LLM goal)
    """
    s = (line or "").strip().rstrip("\r").strip()
    if not s:
        return None
    lower = s.lower()
    # Normalize fullwidth solidus U+FF0F → ASCII slash
    if lower.startswith("\uff0f"):
        lower = "/" + lower[1:]
    # Optional leading slash (covers /gateway and /ww gateway …)
    if lower.startswith("/"):
        lower = lower[1:].lstrip()
    # Optional leading "ww " (shell form inside chat, including after /)
    if lower.startswith("ww "):
        lower = lower[3:].lstrip()

    parts = lower.split()
    if not parts or parts[0] != "gateway":
        return None
    if len(parts) == 1:
        return ("", None)
    action = parts[1]
    if action == "setup" and len(parts) == 2:
        return ("setup", None)
    if action == "list" and len(parts) == 2:
        return ("list", None)
    if action in ("start", "stop") and len(parts) in (2, 3):
        platform = parts[2] if len(parts) == 3 else None
        return (action, platform)
    # e.g. "gateway my bot token" → not a CLI gateway command
    return None


def handle_chat_gateway(action: str, platform: Optional[str] = None) -> None:
    """Run gateway CLI from the interactive REPL (never via /ww/run)."""
    # Auth before any /ww/gateway/* call so setup cannot 401 from a missing key
    load_or_create_api_key()
    cmd_gateway(ArgsObj(action=action or None, platform=platform))


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

    # ── Normal update (shared path with chat /update) ──
    handle_chat_update("")


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
        print(
            f"\n  Type {Colors.cyan('/update')} (chat) or: "
            f"{Colors.cyan('ww update')} (shell)"
        )
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
    print(
        f"\n  Type {Colors.cyan('/update')} (chat) or: "
        f"{Colors.cyan('ww update')} (shell)"
    )
    print()


def cmd_logs(args):
    """View logs (core). Bare `ww logs` prompts for line count on TTY."""
    n = getattr(args, "n", None)
    if n is None:
        if sys.stdin.isatty():
            try:
                raw = input(f"  {Colors.green('How many log lines?')} [20]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not raw:
                n = 20
            else:
                try:
                    n = max(1, min(int(raw), 5000))
                except ValueError:
                    print(f"  {Colors.yellow('⚠')} Invalid number — using 20")
                    n = 20
        else:
            n = 20
    else:
        try:
            n = max(1, min(int(n), 5000))
        except (TypeError, ValueError):
            n = 20

    try:
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
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    candidates = [
        os.path.join(WW_HOME, "server.log"),
        os.path.join(WW_CONFIG, "server.log"),
        os.path.join(os.path.expanduser("~"), "worldwave", "server.log"),
    ]
    for log_file in candidates:
        if os.path.exists(log_file):
            with open(log_file) as f:
                lines = f.readlines()
            for line in lines[-n:]:
                print(line.rstrip())
            return
    print(f"{Colors.yellow('?')} No logs available")
    print(f"  Tried journalctl ww.service and server.log under {Colors.dim(WW_HOME)}")


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
        ww goal start <description>  — Start a new goal (add --server for server-side execution)
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
            print(f"  {Colors.yellow(chr(9888))} Usage: ww goal start <description> [--server]")
            return

        use_server = getattr(args, "goal_use_server", False)

        if use_server:
            # POST to gateway HTTP API for server-side execution
            if not ensure_server_running():
                print(f"  {Colors.yellow(chr(9888))} WW server must be running for --server mode")
                print(f"  Start it with: {Colors.cyan('ww server start')}")
                return
            result = api_post("/ww/run/background", {"goal": goal_text})
            if result:
                task_id = result.get("task_id", "?")
                print(f"  {Colors.green(chr(0x2713))} Goal submitted to server: {Colors.cyan(task_id)}")
                print(f"  {Colors.dim(goal_text[:80])}")
                print(f"  Check status: {Colors.cyan('ww status')}")
            else:
                print(f"  {Colors.red(chr(0x2717))} Server failed to accept goal")
            return

        # Local GoalRunner (in-process)
        print(f"  {Colors.yellow(chr(9888))} Local goal execution needs a running WW engine.")
        print(f"  For autonomous background goals, use: {Colors.cyan('ww goal start --server <description>')}")
        print(f"  Starting local runner anyway (best-effort)...")

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


def cmd_identity(args):
    """Show or link identity (Same Timeline).

    Commands:
        ww identity / ww whoami              — entity + platform links
        ww identity primary                  — show primary entity
        ww identity link <platform> <user_id> [chat_id]
    """
    action = getattr(args, "action", "") or "show"
    # "whoami" maps here with action show
    if action in ("whoami", "show", "list", ""):
        action = "show"

    try:
        from wavegate.identity import IdentityResolver, is_single_user_mode
        resolver = IdentityResolver()
    except Exception as e:
        print(f"  Failed to load identity: {e}")
        return

    if action == "show":
        primary = resolver.get_primary_entity_id()
        entities = resolver.get_all_entities()
        mode = "single-user" if is_single_user_mode() else "multi-user"
        print(f"\n{Colors.bold('Identity')}  ({mode})\n")
        if primary:
            print(f"  Primary: {Colors.cyan(primary)}")
        else:
            print(f"  Primary: {Colors.dim('(not set)')}")
        if not entities:
            print(f"  {Colors.dim('(no entities yet — send a message or run a task)')}")
            return
        print()
        for ent in entities:
            eid = ent["entity_id"]
            mark = " *" if primary and eid == primary else ""
            name = ent.get("display_name") or ""
            print(f"  {Colors.cyan(eid)}{mark}  {name}")
            links = resolver.get_platform_ids(eid)
            if links:
                for lk in links:
                    chat = f" chat={lk['chat_id']}" if lk.get("chat_id") else ""
                    print(f"    - {lk['platform']}:{lk['user_id']}{chat}")
            else:
                print(f"    {Colors.dim('(no platform links)')}")
        if primary:
            print(f"\n  {Colors.dim('* primary entity')}")
        print()

    elif action == "primary":
        primary = resolver.get_primary_entity_id()
        if primary:
            print(f"  Primary: {Colors.cyan(primary)}")
            links = resolver.get_platform_ids(primary)
            for lk in links:
                chat = f" chat={lk['chat_id']}" if lk.get("chat_id") else ""
                print(f"    - {lk['platform']}:{lk['user_id']}{chat}")
        else:
            print(f"  Primary: {Colors.dim('(not set)')}")

    elif action == "link":
        # Forms:
        #   ww identity link <platform> <user_id> [chat_id]
        #   ww identity link <entity_id> <platform> <user_id> [chat_id]
        parts = list(getattr(args, "link_parts", None) or [])
        entity_id = ""
        platform = ""
        user_id = ""
        chat_id = ""
        if parts and str(parts[0]).startswith("ent_"):
            if len(parts) < 3:
                print(f"  {Colors.yellow(chr(9888))} Usage: ww identity link <entity_id> <platform> <user_id> [chat_id]")
                return
            entity_id, platform, user_id = parts[0], parts[1], parts[2]
            chat_id = parts[3] if len(parts) > 3 else ""
        else:
            if len(parts) < 2:
                print(f"  {Colors.yellow(chr(9888))} Usage: ww identity link <platform> <user_id> [chat_id]")
                return
            platform, user_id = parts[0], parts[1]
            chat_id = parts[2] if len(parts) > 2 else ""
            entity_id = resolver.get_primary_entity_id() or ""
            if not entity_id:
                entity_id = resolver.resolve_local("http", "default", "User")
        if not resolver.get_entity(entity_id):
            print(f"  {Colors.red(chr(0x2717))} Entity not found: {entity_id}")
            return
        resolver.link(entity_id, platform, user_id, chat_id)
        print(f"  {Colors.green(chr(0x2713))} Linked {platform}:{user_id} → {Colors.cyan(entity_id)}")

    else:
        print(f"  {Colors.yellow(chr(9888))} Unknown action: {action}")
        print(f"  Usage: ww identity [primary|link ...] | ww whoami")


def cmd_whoami(args):
    """Alias for ww identity."""
    args.action = "show"
    cmd_identity(args)


def cmd_tenant(args):
    """Multi-tenant management — create/list/delete tenants.

    Commands:
        ww tenant list                  — List all tenants
        ww tenant create <id> [name]    — Create a new tenant
        ww tenant delete <id>           — Delete a tenant
        ww tenant rotate-key <id>       — Generate new API key
        ww tenant disable <id>          — Disable a tenant
        ww tenant enable <id>           — Re-enable a tenant
    """
    action = getattr(args, "action", "list")
    tenant_id = getattr(args, "tenant_id", "")

    try:
        from gateway.tenant import TenantManager
        tm = TenantManager()
    except ImportError as e:
        print(f"  Failed to load TenantManager: {e}")
        return

    if action == "list":
        tenants = tm.list_all()
        if tenants:
            print(f"\n{Colors.bold('Tenants:')}\n")
            for t in tenants:
                status = Colors.green(chr(0x25CF)) if t.get("enabled") else Colors.red(chr(0x25CB))
                print(f"  {status} {Colors.cyan(t['tenant_id']):20s}  {t['display_name']}  "
                      f"sessions={t['active_sessions']}  "
                      f"rpm={t['quota']['max_rpm']}")
        else:
            print(f"  {Colors.dim('(no tenants)')}")

    elif action == "create":
        if not tenant_id:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww tenant create <id> [display_name]")
            return
        name = getattr(args, "display_name", tenant_id) or tenant_id
        try:
            tenant = tm.create(tenant_id, display_name=name)
            key = getattr(tenant, "_plaintext_key", "")
            print(f"  {Colors.green(chr(0x2713))} Tenant created: {Colors.cyan(tenant_id)}")
            print(f"  Display name: {name}")
            if key:
                print(f"  API key: {Colors.bold(key)}")
                print(f"  {Colors.yellow(chr(9888))} Store this key — it won't be shown again.")
        except ValueError as e:
            print(f"  {Colors.red(chr(0x2717))} {e}")

    elif action == "delete":
        if not tenant_id:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww tenant delete <id>")
            return
        try:
            if tm.delete(tenant_id):
                print(f"  {Colors.green(chr(0x2713))} Tenant deleted: {tenant_id}")
            else:
                print(f"  {Colors.red(chr(0x2717))} Tenant not found: {tenant_id}")
        except ValueError as e:
            print(f"  {Colors.red(chr(0x2717))} {e}")

    elif action == "rotate-key":
        if not tenant_id:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww tenant rotate-key <id>")
            return
        new_key = tm.rotate_key(tenant_id)
        if new_key:
            print(f"  {Colors.green(chr(0x2713))} Key rotated for: {Colors.cyan(tenant_id)}")
            print(f"  New API key: {Colors.bold(new_key)}")
            print(f"  {Colors.yellow(chr(9888))} Store this key — old key is now invalid.")
        else:
            print(f"  {Colors.red(chr(0x2717))} Tenant not found: {tenant_id}")

    elif action in ("disable", "enable"):
        if not tenant_id:
            print(f"  {Colors.yellow(chr(9888))} Usage: ww tenant {action} <id>")
            return
        try:
            if action == "disable":
                ok = tm.disable(tenant_id)
            else:
                ok = tm.enable(tenant_id)
            if ok:
                print(f"  {Colors.green(chr(0x2713))} Tenant {action}d: {tenant_id}")
            else:
                print(f"  {Colors.red(chr(0x2717))} Tenant not found: {tenant_id}")
        except ValueError as e:
            print(f"  {Colors.red(chr(0x2717))} {e}")

    else:
        print(f"  {Colors.yellow(chr(9888))} Unknown action: {action}")
        print(f"  Usage: ww tenant [list|create|delete|rotate-key|disable|enable] [args...]")


def _mask_secret(val: str, head: int = 6, tail: int = 4) -> str:
    """Mask a token/secret for display."""
    if not val:
        return "(not set)"
    if len(val) <= head + tail:
        return val[:2] + "…" if len(val) > 2 else "***"
    return f"{val[:head]}…{val[-tail:]}"


def _read_env_file_value(env_path: str, key: str) -> str:
    """Read KEY=value from a .env file (no export, first match)."""
    if not env_path or not os.path.exists(env_path):
        return ""
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def cmd_telegram(args):
    """Telegram gateway helpers — status (token/workspace/pairing)."""
    action = getattr(args, "action", "status") or "status"
    CHECK = chr(0x2713)
    CROSS = chr(0x2717)

    if action != "status":
        print(f"  {Colors.yellow(chr(9888))} Usage: ww telegram status")
        print("  Token is set via .env (TELEGRAM_WW_TOKEN) or: ww gateway setup")
        return

    env_file = os.path.join(WW_HOME, ".env")
    token = (
        os.environ.get("TELEGRAM_WW_TOKEN", "").strip()
        or _read_env_file_value(env_file, "TELEGRAM_WW_TOKEN")
    )
    workspace = (
        os.environ.get("TELEGRAM_WW_WORKSPACE", "").strip()
        or _read_env_file_value(env_file, "TELEGRAM_WW_WORKSPACE")
    )
    auto_raw = (
        os.environ.get("WW_PAIRING_AUTO_APPROVE", "").strip()
        or _read_env_file_value(env_file, "WW_PAIRING_AUTO_APPROVE")
        or "false"
    )
    auto = str(auto_raw).strip().lower() in ("1", "true", "yes", "on", "y")

    print(f"\n{Colors.bold('Telegram gateway')}\n")
    if token:
        print(f"  {Colors.green(CHECK)} Token:     {_mask_secret(token)}")
    else:
        print(f"  {Colors.red(CROSS)} Token:     (not set)")
        print(f"     Set TELEGRAM_WW_TOKEN in {env_file}")
        print(f"     or run: {Colors.cyan('ww gateway setup')}")
        print(f"     DMs work with token alone — workspace is optional.")

    if workspace:
        print(f"  {Colors.green(CHECK)} Workspace: {workspace} (DM + that group)")
    else:
        print(f"  {Colors.yellow('○')} Workspace: (not set — DM-only mode)")
        print(f"     Optional TELEGRAM_WW_WORKSPACE=<group_chat_id> for group chats")

    if auto:
        print(f"  {Colors.yellow(chr(9888))} Pairing:   AUTO-APPROVE on (any DM is whitelisted)")
        print(f"     Set WW_PAIRING_AUTO_APPROVE=false for multi-user nodes")
    else:
        print(f"  {Colors.green(CHECK)} Pairing:   require approval (ww pairing approve CODE)")

    try:
        from gateway.pairing import PairingManager
        pm = PairingManager()
        pending = pm.list_pending()
        wl = pm.list_whitelist()
        print(f"  Pending:   {len(pending)} code(s)  |  Whitelist: {len(wl)} user(s)")
    except Exception:
        pass
    print()


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
    """Gateway management — connect to Telegram, Discord, etc."""
    action = args.action

    # Shared HTTP auth: env/file key before any /ww/gateway/* call.
    # Prevents 401 when the server was started with a different in-memory key.
    load_or_create_api_key()

    # ── Setup mode: interactive gateway configuration ──
    if action == "setup" or not action:
        if not auto_start_server():
            print(f"{Colors.red('✗')} Cannot start WW server")
            print(f"  Fix:  {Colors.cyan('ww server restart')}  then retry setup")
            return

        # Check existing gateways
        status = api_get("/ww/gateway/list") or {}
        raw_gw = status.get("gateways", {}) if isinstance(status, dict) else {}
        gateways = raw_gw if isinstance(raw_gw, dict) else {}
        configured = {k: v for k, v in gateways.items() if isinstance(v, dict) and v.get("configured")}
        running = {k: v for k, v in gateways.items() if isinstance(v, dict) and v.get("running")}

        if configured and not action:
            # Already configured — show status
            print(f"\n{Colors.bold('🌐 Gateways:')}\n")
            for name, info in gateways.items():
                r = info.get("running", False)
                c = info.get("configured", False)
                icon = Colors.green("●") if r else Colors.yellow("○") if c else Colors.red("○")
                plat = info.get("platform", name)
                print(f"  {icon} {Colors.cyan(plat)}")
                if not c:
                    print(f"      {Colors.dim('not configured')}")
            if running:
                print(f"\n{Colors.green('✓')} Gateway active — chat via your configured platforms")
            else:
                print(f"\n{Colors.yellow('○')} Gateway configured but not running — run 'ww gateway start'")
            return

        # Interactive setup needs a real TTY (not pipes / non-interactive)
        if not sys.stdin.isatty():
            print(f"\n{Colors.red('✗')} Gateway setup requires an interactive terminal")
            print(f"  Run in a real TTY (not a pipe or non-interactive shell):")
            print(f"    {Colors.cyan('ww gateway setup')}")
            print(f"  Or set the token in .env:")
            print(f"    TELEGRAM_WW_TOKEN=<bot-token-from-BotFather>")
            print(f"  Then: {Colors.cyan('ww gateway start')}\n")
            return

        # Nothing configured → interactive setup
        print(f"\n{Colors.bold('🌐 Gateway Setup')}\n")
        print(f"  Connect WW to a messaging platform so you can chat from anywhere.\n")
        print(f"  Available: {Colors.cyan('Telegram')} | {Colors.dim('Discord (soon)')} | {Colors.dim('Signal (soon)')}\n")

        platform = args.platform
        if not platform:
            try:
                platform = input(f"  {Colors.green('Platform?')} [telegram]: ").strip().lower() or "telegram"
            except (EOFError, KeyboardInterrupt):
                print(f"\n{Colors.yellow('⚠')} Setup cancelled")
                return

        if platform == "telegram":
            print(f"\n  {Colors.bold('Telegram Bot Setup')}")
            print(f"  {Colors.dim('1. Open Telegram → search @BotFather')}")
            print(f"  {Colors.dim('2. Send /newbot → follow prompts')}")
            print(f"  {Colors.dim('3. Copy the bot token (looks like 123:ABC...)')}\n")

            try:
                token = input(f"  {Colors.green('Bot token?')} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{Colors.yellow('⚠')} No token entered — setup cancelled")
                return
            if not token:
                print(f"\n{Colors.yellow('⚠')} No token entered — setup cancelled")
                return

            # Save to .env
            env_file = os.path.join(WW_HOME, ".env")
            _upsert_env(env_file, "TELEGRAM_WW_TOKEN", token)
            print(f"\n{Colors.green('✓')} Telegram token saved to .env")

            # Workspace is optional — DMs work with token alone
            print(f"\n  {Colors.dim('Optional group: add bot to a group, get ID via @userinfobot.')}")
            print(f"  {Colors.dim('Skip for DM-only mode.')}\n")
            try:
                ws = input(f"  {Colors.green('Group ID?')} (press Enter to skip / DM-only): ").strip()
            except (EOFError, KeyboardInterrupt):
                ws = ""
            if ws:
                _upsert_env(env_file, "TELEGRAM_WW_WORKSPACE", ws)
                print(f"{Colors.green('✓')} Workspace saved (DM + group)")
            else:
                print(f"{Colors.green('✓')} DM-only mode (no TELEGRAM_WW_WORKSPACE)")

            # Restart gateway
            print(f"\n{Colors.cyan('⟳')} Starting gateway...")
            result = api_post("/ww/gateway/start", {"platform": "telegram"})
            if result:
                print(f"{Colors.green('✓')} Telegram gateway started!")
                print(f"  {Colors.dim('Try sending /start to your bot on Telegram')}")
            else:
                print(
                    f"{Colors.yellow('⚠')} Gateway start failed — "
                    f"run {Colors.cyan('ww server restart')} then "
                    f"{Colors.cyan('ww gateway setup')} again"
                )

            return

        print(f"\n{Colors.yellow('⚠')} Only Telegram is supported for now")
        return

    # ── List ──
    if action == "list":
        if not auto_start_server():
            print(f"\n  {Colors.dim('(no gateway configured)')}")
            print(f"  {Colors.yellow('→')} Run {Colors.cyan('ww gateway setup')} to get started\n")
            return
        status = api_get("/ww/gateway/list")
        gateways = {}
        if status and isinstance(status, dict):
            raw = status.get("gateways", status)
            if isinstance(raw, dict):
                gateways = raw
        # Empty or missing → never print a blank "Gateway:" header alone
        if not gateways:
            print(f"\n  {Colors.dim('(no gateway configured)')}")
            print(f"  {Colors.yellow('→')} Run {Colors.cyan('ww gateway setup')} to get started\n")
            return
        print(f"\n{Colors.bold('Gateway:')}\n")
        for name, info in gateways.items():
            if not isinstance(info, dict):
                print(f"  {Colors.cyan(str(name))}: {info}")
                continue
            running = info.get("running", False)
            icon = Colors.green("●") if running else Colors.red("○")
            platform = info.get("platform", name)
            print(f"  {icon} {Colors.cyan(platform)}")
            for k, v in info.items():
                if k not in ("platform", "running"):
                    print(f"      {k}: {v}")

    elif action == "start":
        if not auto_start_server():
            print(f"  {Colors.red('✗')} Cannot start WW server")
            print(f"  Fix:  {Colors.cyan('ww server restart')}  then retry")
            return
        platform = args.platform or "telegram"
        result = api_post("/ww/gateway/start", {"platform": platform})
        if result:
            print(f"  {Colors.green('✓')} {platform} gateway started")
        else:
            print(
                f"  {Colors.red('✗')} Failed to start — "
                f"try {Colors.cyan('ww server restart')} then "
                f"{Colors.cyan('ww gateway start')}"
            )

    elif action == "stop":
        if not auto_start_server():
            print(f"  {Colors.red('✗')} Cannot start WW server")
            return
        platform = args.platform or "telegram"
        api_post("/ww/gateway/stop", {"platform": platform})
        print(f"  {Colors.yellow('○')} {platform} gateway stopped")

    elif action == "restart":
        if not auto_start_server():
            print(f"  {Colors.red('✗')} Cannot start WW server")
            print(f"  Fix:  {Colors.cyan('ww server restart')}  then retry")
            return
        platform = args.platform or "telegram"
        print(f"  {Colors.cyan('⟳')} Restarting {platform} gateway...")
        api_post("/ww/gateway/stop", {"platform": platform})
        result = api_post("/ww/gateway/start", {"platform": platform})
        if result:
            print(f"  {Colors.green('✓')} {platform} gateway restarted")
        else:
            print(
                f"  {Colors.red('✗')} Restart failed — "
                f"try {Colors.cyan('ww server restart')} then "
                f"{Colors.cyan('ww gateway restart')}"
            )

    else:
        # Unknown gateway subaction — suggest closest known action
        known = list(_GATEWAY_ACTIONS)
        close = difflib.get_close_matches(str(action), known, n=3, cutoff=0.55)
        print(f"{Colors.red('✗')} Unknown gateway action: {action}")
        if close:
            print(f"  Did you mean: {' | '.join(close)}")
        else:
            print(f"  Did you mean: {' | '.join(known)}")
        print(f"  See: {Colors.cyan('ww gateway setup')} | {Colors.cyan('ww help')}")


def _upsert_env(path, key, value):
    """Update or add a key=value line in a .env file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"# {key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines)


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
    """Show help — core surface first (user lock 2026-07-16)."""
    print("""

  ww <command> [options]

  ── Core ──
  ww                     Enter chat (same as: ww chat)
  ww chat                Enter interactive chat
  ww update              Update Worldwave
  ww key setup           Set LLM API key (alias: ww key set)
  ww model               Switch model (prompts for name; or: ww model <name>)
  ww gateway             Platform gateway status / entry
  ww gateway setup       Configure chat platforms (e.g. Telegram)
  ww gateway restart     Restart messaging gateway
  ww status              System health overview
  ww logs                Show logs (prompts for line count; or: ww logs 50)
  ww help                This help (not bash "help")

  ── Also ──
  ww "task…"             One-shot task
  ww upgrade             Alias for update
  ww key set|show|test   Key management
  ww gateway list|start|stop
  ww server start|stop   Explicit server control
  ww tools · memory · pairing · identity · migrate · config · init …

  --home PATH  --no-color  --effort LEVEL  -h/--help

  Typos get "Did you mean" suggestions.
  Shell tip: use ww help — bare "help" is the bash builtin.
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
    "upgrade": cmd_update,  # alias
    "chat": cmd_run,  # core: enter interactive chat (empty goal)
    "logs": cmd_logs,
    "delegate": cmd_delegate,
    "goal": cmd_goal,
    "gateway": cmd_gateway,
    "telegram": cmd_telegram,
    "pairing": cmd_pairing,
    "memory": cmd_memory,
    "mascot": cmd_mascot,
    "migrate": cmd_migrate,
    "run": cmd_run,
    "help": cmd_help,
    "tenant": cmd_tenant,
    "identity": cmd_identity,
    "whoami": cmd_whoami,
}

# Known CLI vocabulary for typo suggestions (stdlib difflib only).
KNOWN_CLI_COMMANDS: tuple[str, ...] = tuple(sorted(COMMANDS.keys()))
KNOWN_CLI_PHRASES: tuple[str, ...] = (
    "gateway setup",
    "gateway list",
    "gateway start",
    "gateway stop",
    "gateway restart",
    "key setup",
    "key set",
    "server start",
    "server stop",
    "server restart",
    "server status",
    "update status",
    "telegram status",
    "memory stats",
    "memory search",
    "memory sleep",
    "pairing list",
    "pairing approve",
    "pairing reject",
    "identity primary",
    "identity link",
    "identity show",
)
_GATEWAY_ACTIONS = ("setup", "list", "start", "stop", "restart")
_TYPO_CUTOFF = 0.55
_TYPO_MAX_REST = 2


def suggest_cli_commands(
    token: str, rest: Optional[List[str]] = None
) -> List[str]:
    """Return full ``ww …`` suggestion strings for a mistyped token.

    Prefer multi-word phrase matches when a second token is present, then
    single-command matches from COMMANDS.
    """
    rest = list(rest or [])
    out: List[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        if s not in seen:
            seen.add(s)
            out.append(s)

    cmd_hits = difflib.get_close_matches(
        token, list(KNOWN_CLI_COMMANDS), n=3, cutoff=_TYPO_CUTOFF
    )

    if rest:
        two = f"{token} {rest[0]}"
        # Best phrase only — avoid flooding with sibling subcommands
        # (e.g. gataway setup → gateway setup, not list/stop/start too).
        phrase_hits = difflib.get_close_matches(
            two, KNOWN_CLI_PHRASES, n=2, cutoff=_TYPO_CUTOFF
        )
        for phrase in phrase_hits:
            head = phrase.split()[0]
            # Keep phrase if its command head is a close match for token
            if head in cmd_hits or difflib.SequenceMatcher(
                None, token, head
            ).ratio() >= _TYPO_CUTOFF:
                _add(f"ww {phrase}")
                break  # top qualifying phrase is enough
        # Exact reconstructed phrase from corrected command + rest[0]
        for cmd in cmd_hits:
            candidate = f"{cmd} {rest[0]}"
            if candidate in KNOWN_CLI_PHRASES:
                _add(f"ww {candidate}")

    for cmd in cmd_hits:
        _add(f"ww {cmd}")

    return out


def is_likely_command_typo(
    token: str, rest: Optional[List[str]] = None
) -> bool:
    """True when ``token`` looks like a misspelled WW command (not free-text task).

    Typo path when close command/phrase matches exist and remaining tokens
    are few (``<= 2``). Longer free-text after a weak match stays LLM goal.
    """
    rest = list(rest or [])
    if not token or " " in token or len(token) > 24:
        return False
    if len(rest) > _TYPO_MAX_REST:
        return False

    matches = difflib.get_close_matches(
        token, list(KNOWN_CLI_COMMANDS), n=1, cutoff=_TYPO_CUTOFF
    )
    phrase_matches: List[str] = []
    if rest:
        two = f"{token} {rest[0]}"
        phrase_matches = difflib.get_close_matches(
            two, KNOWN_CLI_PHRASES, n=1, cutoff=_TYPO_CUTOFF
        )
    return bool(matches or phrase_matches)


def print_command_suggestions(
    token: str, rest: Optional[List[str]] = None
) -> None:
    """Print unknown-command message with Did you mean suggestions."""
    suggestions = suggest_cli_commands(token, rest)
    print(f"{Colors.red('✗')} Unknown command: {token}")
    if suggestions:
        print("  Did you mean:")
        for s in suggestions:
            print(f"    {Colors.cyan(s)}")
    print(f"  See: {Colors.cyan('ww help')}")
    tip_example = 'ww "write a script"'
    print(f"  Tip: multi-word goals as a task:  {Colors.cyan(tip_example)}")


# Known interactive-chat vocabulary (commands the REPL actually intercepts).
KNOWN_CHAT_COMMANDS: tuple[str, ...] = (
    "clear",
    "exit",
    "gateway",
    "help",
    "q",
    "quit",
    "update",
    "upgrade",
)
KNOWN_CHAT_PHRASES: tuple[str, ...] = (
    "gateway list",
    "gateway setup",
    "gateway start",
    "gateway stop",
    "update --dry-run",
    "update status",
    "upgrade --dry-run",
    "upgrade status",
)


def _suggest_chat_from_tokens(
    token: str, rest: Optional[List[str]] = None
) -> List[str]:
    """Build slash-form suggestions from a token + rest (stdlib difflib)."""
    rest = list(rest or [])
    out: List[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        if s not in seen:
            seen.add(s)
            out.append(s)

    cmd_hits = difflib.get_close_matches(
        token, list(KNOWN_CHAT_COMMANDS), n=3, cutoff=_TYPO_CUTOFF
    )

    if rest:
        two = f"{token} {rest[0]}"
        phrase_hits = difflib.get_close_matches(
            two, KNOWN_CHAT_PHRASES, n=2, cutoff=_TYPO_CUTOFF
        )
        for phrase in phrase_hits:
            head = phrase.split()[0]
            if head in cmd_hits or difflib.SequenceMatcher(
                None, token, head
            ).ratio() >= _TYPO_CUTOFF:
                _add(f"/{phrase}")
                break
        for cmd in cmd_hits:
            candidate = f"{cmd} {rest[0]}"
            if candidate in KNOWN_CHAT_PHRASES:
                _add(f"/{candidate}")

    for cmd in cmd_hits:
        _add(f"/{cmd}")

    return out


def suggest_chat_commands(line: str) -> Optional[List[str]]:
    """Suggest slash-form chat commands for a mistyped meta line.

    Returns:
      None — not a command typo; treat as free-text LLM goal
      list — print Did-you-mean (possibly empty) and skip LLM
    """
    s = (line or "").strip().rstrip("\r").strip()
    if not s:
        return None

    lower = s.lower()
    had_slash = False
    had_ww = False

    # Normalize fullwidth solidus U+FF0F → ASCII slash
    if lower.startswith("\uff0f"):
        lower = "/" + lower[1:]
    if lower.startswith("/"):
        had_slash = True
        lower = lower[1:].lstrip()
    if lower.startswith("ww "):
        had_ww = True
        lower = lower[3:].lstrip()
    elif lower == "ww":
        had_ww = True
        lower = ""

    parts = lower.split()
    looks_like_meta = had_slash or had_ww
    if not parts:
        return [] if looks_like_meta else None

    token = parts[0]
    rest = parts[1:]
    if not token or len(token) > 24:
        return [] if looks_like_meta else None
    # Align shell: long free-text after a weak first word stays LLM
    if len(rest) > _TYPO_MAX_REST:
        return None

    suggestions = _suggest_chat_from_tokens(token, rest)
    if suggestions:
        return suggestions
    if looks_like_meta:
        return []
    return None


def print_chat_command_suggestions(
    line: str, suggestions: Optional[List[str]] = None
) -> None:
    """Print chat unknown-command message with Did you mean suggestions."""
    display = (line or "").strip().rstrip("\r").strip()
    if display.startswith("\uff0f"):
        display = "/" + display[1:]
    if suggestions is None:
        suggestions = suggest_chat_commands(line) or []
    print(f"{Colors.red('✗')} Unknown command: {display}")
    if suggestions:
        print("  Did you mean:")
        for s in suggestions:
            print(f"    {Colors.cyan(s)}")
    print(f"  Type {Colors.cyan('/help')} for chat commands")


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
        # Bare 'ww' → interactive chat mode (core)
        args.goal = []
        args.spirals = 3
        cmd_run(args)
        return

    cmd = args.command
    if cmd in ("chat",):
        # ww chat → same as bare ww
        args.goal = []
        args.spirals = 3
        cmd_run(args)
        return
    # Subcommands often land in args.goal (argparse nargs="*"), not only parse_known_args extra
    def _pos():
        return list(getattr(args, "goal", []) or []) + list(extra or [])

    if cmd in ("server",):
        # server start/stop/restart/status
        pos = _pos()
        args.action = pos[0] if pos else "status"
        COMMANDS[cmd](args)

    elif cmd in ("update", "upgrade"):
        pos = _pos()
        args.update_action = pos[0] if pos else None
        COMMANDS["update"](args)

    elif cmd in ("config",):
        # config: subcommands or key/value
        pos = _pos()
        if pos and pos[0] == "profile":
            args.profile = True
            args.profile_action = pos[1] if len(pos) > 1 else "list"
            args.profile_name = pos[2] if len(pos) > 2 else ""
            COMMANDS[cmd](args)
        else:
            args.profile = False
            args.set_key = pos[0] if pos else None
            args.set_value = pos[1:] if len(pos) > 1 else None
            COMMANDS[cmd](args)

    elif cmd in ("model",):
        pos = _pos()
        args.name = pos[0] if pos else None
        COMMANDS[cmd](args)

    elif cmd in ("logs",):
        pos = _pos()
        if pos:
            try:
                args.n = int(pos[0])
            except ValueError:
                print(f"{Colors.red('✗')} Invalid line count: {pos[0]}")
                print(f"  Usage: {Colors.cyan('ww logs')}  or  {Colors.cyan('ww logs 50')}")
                return
        else:
            args.n = None  # prompt on TTY
        COMMANDS[cmd](args)

    elif cmd in ("gateway",):
        # bare → None (setup/status); explicit list|setup|start|stop still work
        pos = _pos()
        args.action = pos[0] if pos else None
        args.platform = pos[1] if len(pos) > 1 else None
        COMMANDS[cmd](args)

    elif cmd in ("help",):
        # ww help — never treat as one-shot LLM goal
        cmd_help(args)

    elif cmd in ("telegram",):
        pos = _pos()
        args.action = pos[0] if pos else "status"
        COMMANDS[cmd](args)

    elif cmd in ("pairing",):
        pos = _pos()
        args.action = pos[0] if pos else "list"
        if args.action == "remove":
            args.platform = pos[1] if len(pos) > 1 else ""
            args.code = pos[2] if len(pos) > 2 else ""
        else:
            args.code = pos[1] if len(pos) > 1 else ""
        COMMANDS[cmd](args)

    elif cmd in ("memory",):
        pos = _pos()
        args.action = pos[0] if pos else "stats"
        args.query = pos[1:] if len(pos) > 1 else []
        COMMANDS[cmd](args)

    elif cmd in ("delegate",):
        args.goal = _pos()
        args.parallel = 3
        COMMANDS[cmd](args)

    elif cmd in ("goal",):
        goal_args = getattr(args, 'goal', []) or []
        # Filter out flags from goal args
        flags = set(a for a in goal_args if a.startswith("--"))
        positional = [a for a in goal_args if not a.startswith("--")]
        args.action = positional[0] if positional else "list"
        if args.action == "start":
            args.goal_id = " ".join(positional[1:]) if len(positional) > 1 else ""
        else:
            args.goal_id = positional[1] if len(positional) > 1 else ""
        args.goal_use_server = "--server" in flags
        COMMANDS[cmd](args)

    elif cmd in ("mascot",):
        pos = _pos()
        args.action = pos[0] if pos else "open"
        if args.action == "state" and len(pos) > 1:
            args.state_name = pos[1]
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

    elif cmd in ("tenant",):
        goal_args = getattr(args, 'goal', []) or []
        args.action = goal_args[0] if goal_args else "list"
        if args.action == "create":
            args.tenant_id = goal_args[1] if len(goal_args) > 1 else ""
            args.display_name = goal_args[2] if len(goal_args) > 2 else ""
        else:
            args.tenant_id = goal_args[1] if len(goal_args) > 1 else ""
        cmd_tenant(args)

    elif cmd in ("identity", "whoami"):
        goal_args = list(getattr(args, "goal", []) or [])
        if extra:
            goal_args = goal_args + list(extra)
        if cmd == "whoami":
            args.action = "show"
        else:
            args.action = goal_args[0] if goal_args else "show"
            if args.action == "link":
                args.link_parts = goal_args[1:]
            elif args.action == "primary":
                pass
            elif args.action not in ("show", "list", "primary", "link"):
                # bare "ww identity" or unknown → show
                args.action = "show"
        COMMANDS["identity" if cmd == "identity" else "whoami"](args)

    elif cmd in ("run",):
        args.spirals = None
        cmd_run(args)

    else:
        # Unrecognized command → typo suggestions, else one-shot LLM task
        # e.g. 'ww updat' → Did you mean: ww update
        # e.g. 'ww write a script' → goal = "write a script"
        goal_words = [cmd]
        if extra:
            goal_words += extra
        elif args.goal:
            goal_words += args.goal
        rest_tokens = goal_words[1:]
        if is_likely_command_typo(cmd, rest_tokens):
            print_command_suggestions(cmd, rest_tokens)
            sys.exit(1)
        args.goal = goal_words
        args.spirals = 5
        for i, e in enumerate(extra):
            if e == "--spirals" and i + 1 < len(extra):
                args.spirals = int(extra[i + 1])
        cmd_run(args)


if __name__ == "__main__":
    main()