"""Shared chat-core slash commands for REPL (➤) and Telegram.

Frozen capability set (2026-07-16). Surfaces share the same verbs; syntax may
differ slightly (Telegram has no /exit; REPL always allows /gateway restart).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ── Catalog (help text + typo vocab) ──────────────────────────────────

CHAT_CORE_COMMANDS: Tuple[Tuple[str, str], ...] = (
    ("help", "List chat core commands"),
    ("new", "New session + clean working memory (keeps core prefs)"),
    ("clear", "Alias of /new"),
    ("model", "Show or switch model (/model [name])"),
    ("memory", "Memory stats; edit/set/del for LTM"),
    ("true", "Allow next tool past safety block once"),
    ("stop", "Stop current autonomous/run"),
    ("status", "Server status + model + version"),
    ("gateway restart", "Restart telegram gateway (owner-only on TG)"),
    ("exit", "Leave chat (REPL only)"),
)

# Single-token names handled by parse_chat_core_command
_CORE_SINGLE = frozenset(
    {
        "help",
        "new",
        "clear",
        "model",
        "memory",
        "true",
        "stop",
        "status",
        "exit",  # parse only; handle may no-op on telegram
    }
)

# Memory subcommands that require args after "memory"
_MEMORY_SUBS = frozenset({"edit", "set", "del", "delete"})


@dataclass
class ParsedChatCommand:
    """Parsed chat-core command."""

    name: str  # help|new|clear|model|memory|true|stop|status|gateway|exit
    args: str = ""  # remainder after command verb
    raw: str = ""


@dataclass
class ChatCommandContext:
    """Callbacks and identity for command handlers."""

    api_get: Callable[[str], Any]
    api_post: Callable[[str, Dict[str, Any]], Any]
    is_tty: bool = False
    platform: str = "repl"  # repl | telegram
    user_id: str = ""
    chat_id: str = ""
    is_owner: bool = True
    prompt_fn: Optional[Callable[[str], str]] = None
    entity_id: str = ""
    # Optional extra state bag (e.g. pending memory edit)
    state: Dict[str, Any] = field(default_factory=dict)


def _normalize_line(line: str) -> str:
    s = (line or "").strip().rstrip("\r").strip()
    if not s:
        return ""
    lower = s.lower()
    # fullwidth solidus → ASCII
    if lower.startswith("\uff0f"):
        lower = "/" + lower[1:]
    if lower.startswith("/"):
        lower = lower[1:].lstrip()
    if lower.startswith("ww "):
        lower = lower[3:].lstrip()
    elif lower == "ww":
        lower = ""
    return lower


def parse_chat_core_command(line: str) -> Optional[ParsedChatCommand]:
    """Parse a chat-core command line.

    Returns ParsedChatCommand when the line is clearly a core meta-command;
    None when it should fall through (LLM goal, update, other gateway, etc.).
    """
    raw = (line or "").strip().rstrip("\r").strip()
    lower = _normalize_line(raw)
    if not lower:
        return None

    parts = lower.split()
    cmd = parts[0]
    rest_parts = parts[1:]

    # /gateway restart [platform]
    if cmd == "gateway":
        if rest_parts and rest_parts[0] == "restart":
            args = " ".join(rest_parts)  # restart [platform]
            return ParsedChatCommand(name="gateway", args=args, raw=raw)
        return None  # setup/list/start/stop → existing gateway parser

    if cmd not in _CORE_SINGLE:
        return None

    # exit is REPL-only; still parse so telegram can say "REPL only"
    if cmd == "memory" and rest_parts:
        # keep full subcommand string: "edit" | "set id text" | "del id"
        return ParsedChatCommand(name="memory", args=" ".join(rest_parts), raw=raw)

    if cmd == "model":
        return ParsedChatCommand(name="model", args=" ".join(rest_parts), raw=raw)

    if rest_parts and cmd not in ("model", "memory"):
        # e.g. "status please" → not a bare meta command
        # Allow: status/true/stop/new/clear/help with no args only
        if cmd in ("status", "true", "stop", "new", "clear", "help", "exit"):
            return None

    return ParsedChatCommand(name=cmd, args=" ".join(rest_parts), raw=raw)


def format_help_text(platform: str = "repl") -> str:
    """User-facing /help body."""
    lines = ["**Chat commands**" if platform == "telegram" else "Chat core commands:", ""]
    rows = [
        ("/help", "List these commands"),
        ("/new", "New session; clean working memory (keeps core)"),
        ("/clear", "Same as /new"),
        ("/model [name]", "Show or switch model"),
        ("/memory", "Memory stats (atoms, WM capacity)"),
        ("/memory edit", "List recent LTM (then set/del by id)"),
        ("/memory set <id> <text>", "Update a memory atom"),
        ("/memory del <id>", "Delete a memory atom"),
        ("/true", "Next tool skips safety block once"),
        ("/stop", "Stop current run / autonomous loop"),
        ("/status", "Server status + model + version"),
        ("/gateway restart", "Restart telegram gateway"),
    ]
    if platform == "repl":
        rows.append(("/exit", "Leave chat (also: quit, q)"))
        rows.append(("/update", "Upgrade Worldwave (not an LLM goal)"))
        rows.append(("/gateway", "Gateway status / setup"))
        rows.append(("/gateway setup", "Interactive Telegram gateway setup"))
    for cmd, desc in rows:
        if platform == "telegram":
            lines.append(f"{cmd} — {desc}")
        else:
            lines.append(f"  {cmd:<28} {desc}")
    if platform == "repl":
        lines.append("")
        lines.append(
            "  Also bare: update · gateway · model …  "
            "Typos: Did you mean (no LLM)"
        )
    else:
        lines.append("")
        lines.append("Just send a message to start a task!")
    return "\n".join(lines)


def _safe_dict(val: Any) -> Dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _fmt_memory_stats(stats: Dict[str, Any]) -> str:
    hippo = stats.get("hippocampus") if isinstance(stats.get("hippocampus"), dict) else {}
    atoms = (
        hippo.get("count")
        or stats.get("total_atoms")
        or stats.get("atoms")
        or "N/A"
    )
    hippo_cap = hippo.get("capacity", "N/A")
    hippo_sz = hippo.get("count", hippo_cap)
    cortex = stats.get("cortex_size")
    if cortex is None:
        fs = stats.get("fact_store") if isinstance(stats.get("fact_store"), dict) else {}
        cortex = fs.get("total", "N/A")
    sleep = stats.get("sleep_cycles", "N/A")
    wm_cap = stats.get("working_memory_capacity", "N/A")
    return (
        f"**Memory**\n"
        f"atoms: {atoms}\n"
        f"hippocampus: {hippo_sz}/{hippo_cap}\n"
        f"cortex/facts: {cortex}\n"
        f"WM capacity: {wm_cap}\n"
        f"sleep cycles: {sleep}"
    )


def _fmt_recent_atoms(results: Sequence[Dict[str, Any]], limit: int = 15) -> str:
    if not results:
        return "No recent long-term memories.\nCommands: /memory set <id> <text>  |  /memory del <id>"
    lines = ["**Recent LTM** (edit with set/del):", ""]
    for i, a in enumerate(results[:limit], 1):
        if not isinstance(a, dict):
            continue
        mid = a.get("atom_id") or a.get("id") or "?"
        content = (a.get("content") or "").replace("\n", " ").strip()
        if len(content) > 80:
            content = content[:77] + "…"
        core = " [core]" if a.get("is_core") else ""
        lines.append(f"{i}. id=`{mid}`{core}  {content}")
    lines.append("")
    lines.append("Commands: /memory del <id>  |  /memory set <id> <text>")
    return "\n".join(lines)


def _handle_help(ctx: ChatCommandContext) -> str:
    return format_help_text(ctx.platform)


def _handle_new(ctx: ChatCommandContext, alias: bool = False) -> str:
    body: Dict[str, Any] = {}
    if ctx.entity_id:
        body["entity_id"] = ctx.entity_id
    result = ctx.api_post("/ww/chat/new", body) or {}
    if result.get("error"):
        return f"Could not start new session: {result.get('error')}"
    cleared = result.get("wm_cleared", 0)
    promoted = result.get("promoted", 0)
    kept = result.get("kept_core", 0)
    # User-facing: not “wipe all memory”
    head = "Working memory cleaned" if not alias else "Session reset (same as /new)"
    return (
        f"{head}: cleared {cleared}, promoted {promoted}, kept {kept} core.\n"
        f"Long-term memory was not wiped."
    )


def _handle_model(parsed: ParsedChatCommand, ctx: ChatCommandContext) -> str:
    name = (parsed.args or "").strip()
    if not name:
        info = _safe_dict(ctx.api_get("/ww/model"))
        current = info.get("model", "N/A")
        provider = info.get("provider", "N/A")
        show = f"**Model:** `{current}`\n**Provider:** `{provider}`"
        if ctx.is_tty and ctx.prompt_fn:
            try:
                entered = (ctx.prompt_fn("Model name? (Enter to keep current) ") or "").strip()
            except (EOFError, KeyboardInterrupt):
                return show + "\n(kept current)"
            if not entered:
                return show + "\n(kept current)"
            name = entered
        else:
            if ctx.platform == "telegram":
                return show + "\nSend /model <name> to switch."
            return show + "\nUse /model <name> to switch."

    result = _safe_dict(ctx.api_post("/ww/model", {"model": name}))
    if result.get("switched"):
        return f"✓ Switched: `{result.get('from')}` → `{result.get('to')}`"
    if result.get("error"):
        return f"✗ Failed: {result.get('error')}"
    # switch_model may return different shape
    if result.get("model") or result.get("to"):
        return f"✓ Model: `{result.get('to') or result.get('model')}`"
    return f"✗ Failed: {result or 'no response'}"


def _handle_memory(parsed: ParsedChatCommand, ctx: ChatCommandContext) -> str:
    args = (parsed.args or "").strip()
    if not args:
        stats = _safe_dict(ctx.api_get("/ww/memory/stats"))
        if not stats or stats.get("error"):
            return "**Memory:** Could not fetch stats"
        return _fmt_memory_stats(stats)

    parts = args.split(None, 2)
    sub = parts[0].lower()

    if sub == "edit":
        data = _safe_dict(ctx.api_get("/ww/memory/recent?limit=15"))
        results = data.get("results") or []
        return _fmt_recent_atoms(results, limit=15)

    if sub in ("del", "delete"):
        if len(parts) < 2:
            return "Usage: /memory del <id>"
        mid = parts[1]
        result = _safe_dict(
            ctx.api_post(
                "/ww/memory",
                {"action": "delete", "memory_id": mid, "query": mid},
            )
        )
        if result.get("error") == "is_core":
            return (
                f"Memory `{mid}` is core — not deleted.\n"
                f"To force: /memory del {mid}  with confirm via API (core protect)."
            )
        if result.get("error") == "not_found":
            return f"Memory `{mid}` not found."
        if result.get("status") == "deleted" or result.get("deleted"):
            return f"Deleted memory `{mid}`."
        return f"Delete failed: {result.get('error') or result}"

    if sub == "set":
        if len(parts) < 3:
            return "Usage: /memory set <id> <text>"
        mid = parts[1]
        text = parts[2]
        result = _safe_dict(
            ctx.api_post(
                "/ww/memory",
                {
                    "action": "update",
                    "memory_id": mid,
                    "query": mid,
                    "content": text,
                },
            )
        )
        if result.get("error") == "not_found":
            return f"Memory `{mid}` not found."
        if result.get("status") == "updated" or result.get("updated"):
            return f"Updated memory `{mid}`."
        return f"Update failed: {result.get('error') or result}"

    return f"Unknown /memory subcommand: {sub}\nTry: /memory | /memory edit | /memory set | /memory del"


def _handle_true(ctx: ChatCommandContext) -> str:
    result = _safe_dict(ctx.api_post("/ww/chat/true", {}))
    if result.get("error"):
        return f"Could not set force flag: {result.get('error')}"
    lines = [
        "Next tool call will skip the safety-system block **once**.",
        "(Does not bypass approval gates for unsafe actions.)",
    ]
    last = result.get("last_blocked")
    if isinstance(last, dict) and last.get("tool"):
        tool = last.get("tool")
        reason = last.get("reason") or ""
        n = last.get("n_score")
        n_part = f" (N-score {n})" if n is not None else ""
        lines.append(f"Last blocked: {tool}{n_part}")
        if reason:
            lines.append(f"  {reason[:120]}")
    else:
        lines.append("Last blocked: none recently.")
    return "\n".join(lines)


def _handle_stop(ctx: ChatCommandContext) -> str:
    result = _safe_dict(ctx.api_post("/ww/chat/stop", {}))
    status = result.get("status", "ok")
    if status in ("stopped", "ok"):
        return "⏹️ Stop signal sent."
    if result.get("error"):
        return f"Stop: {result.get('error')}"
    return f"Stop: {status}"


def _handle_status(ctx: ChatCommandContext) -> str:
    s = _safe_dict(ctx.api_get("/ww/status"))
    if not s:
        return "**Status:** Server not reachable"
    version = s.get("version", "N/A")
    model = s.get("model") or (s.get("ww") or {}).get("model") or "N/A"
    auto = s.get("autonomous")
    if isinstance(auto, dict):
        auto_s = auto.get("running", auto)
    else:
        auto_s = s.get("running", "N/A")
    tools = s.get("tool_count", "N/A")
    phase = s.get("current_phase", "N/A")
    spiral = s.get("current_spiral", s.get("session_spiral", "N/A"))
    return (
        f"**Status**\n"
        f"version: {version}\n"
        f"model: `{model}`\n"
        f"autonomous/running: {auto_s}\n"
        f"tools: {tools}\n"
        f"spiral: {spiral}  phase: {phase}"
    )


def _handle_gateway_restart(parsed: ParsedChatCommand, ctx: ChatCommandContext) -> str:
    if ctx.platform == "telegram" and not ctx.is_owner:
        return "⛔ /gateway restart is owner-only."
    # args: "restart" or "restart telegram"
    parts = (parsed.args or "restart").split()
    platform = "telegram"
    if len(parts) >= 2:
        platform = parts[1]
    # stop then start
    ctx.api_post("/ww/gateway/stop", {"platform": platform})
    result = ctx.api_post("/ww/gateway/start", {"platform": platform})
    if result:
        return f"✓ {platform} gateway restarted"
    return f"✗ Failed to restart {platform} gateway"


def handle_chat_core(parsed: ParsedChatCommand, ctx: ChatCommandContext) -> Optional[str]:
    """Execute a parsed chat-core command. Returns user-facing message.

    Returns None only if the command should not be considered handled
    (should not happen for a successful parse).
    """
    if parsed is None:
        return None
    name = parsed.name

    if name == "help":
        return _handle_help(ctx)
    if name == "new":
        return _handle_new(ctx, alias=False)
    if name == "clear":
        return _handle_new(ctx, alias=True)
    if name == "model":
        return _handle_model(parsed, ctx)
    if name == "memory":
        return _handle_memory(parsed, ctx)
    if name == "true":
        return _handle_true(ctx)
    if name == "stop":
        return _handle_stop(ctx)
    if name == "status":
        return _handle_status(ctx)
    if name == "gateway":
        return _handle_gateway_restart(parsed, ctx)
    if name == "exit":
        if ctx.platform == "telegram":
            return "/exit is only available in the local terminal chat."
        return None  # REPL handles exit before this usually
    return f"Unknown chat command: {name}"


def chat_core_command_names() -> List[str]:
    """Token names for typo suggestion vocab (no phrases)."""
    return [
        "help",
        "new",
        "clear",
        "model",
        "memory",
        "true",
        "stop",
        "status",
        "gateway",
        "exit",
        "update",
        "upgrade",
        "quit",
        "q",
    ]


def chat_core_phrases() -> List[str]:
    return [
        "gateway restart",
        "gateway list",
        "gateway setup",
        "gateway start",
        "gateway stop",
        "memory edit",
        "memory set",
        "memory del",
        "update status",
        "update --dry-run",
        "upgrade status",
        "upgrade --dry-run",
    ]
