"""
ww/core/commands.py — Slash Command System v0.1

Implements a Claude Code / Hermes Agent-style slash command registry.
Commands can be defined inline or loaded from skills/ directory as YAML.

Built-in commands: /help, /clear, /status, /model, /tools, /memory, 
  /config, /goal, /rollback, /compress, /new, /stop, /diff, /plan,
  /review, /retry, /undo, /save, /usage, /debug, /agents
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


class CommandCategory(Enum):
    SESSION = "session"
    CONFIG = "config"
    TOOLS = "tools"
    WORKFLOW = "workflow"
    INFO = "info"
    DEBUG = "debug"


@dataclass
class CommandDef:
    """Definition of a slash command."""
    name: str
    description: str
    handler: Callable  # async fn(args: str, context: Dict) -> str
    category: CommandCategory = CommandCategory.SESSION
    aliases: List[str] = field(default_factory=list)
    usage: Optional[str] = None
    requires_confirmation: bool = False
    hidden: bool = False
    
    @property
    def help_text(self) -> str:
        usage = self.usage or f"/{self.name}"
        aliases = f" (aliases: {', '.join('/' + a for a in self.aliases)})" if self.aliases else ""
        return f"/{self.name}{aliases} — {self.description}\n  Usage: {usage}"


class CommandRegistry:
    """Central slash command registry."""
    
    def __init__(self):
        self._commands: Dict[str, CommandDef] = {}
        self._aliases: Dict[str, str] = {}  # alias → canonical name
        
    def register(self, cmd: CommandDef):
        """Register a command."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._aliases[alias] = cmd.name
            
    def unregister(self, name: str):
        """Remove a command."""
        if name in self._commands:
            cmd = self._commands.pop(name)
            for alias in cmd.aliases:
                self._aliases.pop(alias, None)
                
    def resolve(self, name: str) -> Optional[CommandDef]:
        """Resolve a command name (including aliases)."""
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)
    
    def list_all(self, category: CommandCategory = None) -> List[CommandDef]:
        """List all commands, optionally filtered by category."""
        cmds = list(self._commands.values())
        if category:
            cmds = [c for c in cmds if c.category == category]
        return sorted(cmds, key=lambda c: c.name)
    
    def format_help(self, query: str = None) -> str:
        """Generate help text."""
        if query:
            cmd = self.resolve(query)
            if cmd:
                return cmd.help_text
            return f"No command: /{query}"
            
        lines = ["## Slash Commands", ""]
        by_cat = {}
        for cmd in self._commands.values():
            if cmd.hidden:
                continue
            by_cat.setdefault(cmd.category.value, []).append(cmd)
            
        for cat_name in sorted(by_cat):
            lines.append(f"### {cat_name.title()}")
            for cmd in sorted(by_cat[cat_name], key=lambda c: c.name):
                lines.append(f"- `/{cmd.name}` — {cmd.description}")
            lines.append("")
        return "\n".join(lines)
    
    async def execute(self, name: str, args: str, context: Dict = None) -> str:
        """Execute a command by name. Returns result string."""
        cmd = self.resolve(name)
        if not cmd:
            return f"Unknown command: /{name}. Type /help to see available commands."
        
        context = context or {}
        try:
            result = cmd.handler(args, context)
            if hasattr(result, '__await__'):
                result = await result
            return result or ""
        except Exception as e:
            return f"Command /{name} failed: {e}"


# Singleton
_command_registry: Optional[CommandRegistry] = None


def get_command_registry() -> CommandRegistry:
    global _command_registry
    if _command_registry is None:
        _command_registry = CommandRegistry()
        _register_builtins(_command_registry)
    return _command_registry


def _register_builtins(reg: CommandRegistry):
    """Register all built-in slash commands."""
    
    # ── Session Control ──
    
    def cmd_new(args: str, ctx: Dict) -> str:
        ctx['_signal_new_session'] = True
        return "Starting new session..."
    
    reg.register(CommandDef(
        name="new", description="Start a fresh session",
        handler=cmd_new, category=CommandCategory.SESSION,
        aliases=["reset", "clear"],
        usage="/new — clears all context and starts fresh"
    ))
    
    def cmd_stop(args: str, ctx: Dict) -> str:
        ctx['_signal_stop'] = True
        return "Stopping all background processes..."
    
    reg.register(CommandDef(
        name="stop", description="Stop background processes",
        handler=cmd_stop, category=CommandCategory.SESSION,
        usage="/stop — terminates running background tasks"
    ))
    
    def cmd_retry(args: str, ctx: Dict) -> str:
        ctx['_signal_retry'] = True
        return "Retrying last message..."
    
    reg.register(CommandDef(
        name="retry", description="Resend last message",
        handler=cmd_retry, category=CommandCategory.SESSION,
        usage="/retry — resends the previous user message"
    ))
    
    def cmd_undo(args: str, ctx: Dict) -> str:
        ctx['_signal_undo'] = True
        return "Undoing last exchange..."
    
    reg.register(CommandDef(
        name="undo", description="Remove last exchange",
        handler=cmd_undo, category=CommandCategory.SESSION,
        usage="/undo — removes the last user+assistant exchange"
    ))
    
    def cmd_compress(args: str, ctx: Dict) -> str:
        ctx['_signal_compress'] = True
        return "Compressing context..."
    
    reg.register(CommandDef(
        name="compress", description="Manually compress context",
        handler=cmd_compress, category=CommandCategory.SESSION,
        usage="/compress — triggers context compression"
    ))
    
    def cmd_goal(args: str, ctx: Dict) -> str:
        if not args.strip():
            return "Usage: /goal <description> — set a standing goal"
        ctx['_set_goal'] = args.strip()
        return f"Goal set: {args.strip()}"
    
    reg.register(CommandDef(
        name="goal", description="Set a standing goal for the agent",
        handler=cmd_goal, category=CommandCategory.SESSION,
        usage="/goal <description> — agent works toward this across turns"
    ))
    
    def cmd_rollback(args: str, ctx: Dict) -> str:
        n = int(args.strip() or "1")
        ctx['_signal_rollback'] = n
        return f"Rolling back {n} checkpoint(s)..."
    
    reg.register(CommandDef(
        name="rollback", description="Restore filesystem checkpoint",
        handler=cmd_rollback, category=CommandCategory.SESSION,
        usage="/rollback [N] — rollback N checkpoints (default 1)"
    ))
    
    # ── Configuration ──
    
    def cmd_model(args: str, ctx: Dict) -> str:
        current = ctx.get('current_model', 'unknown')
        if args.strip():
            ctx['_set_model'] = args.strip()
            return f"Switching model to: {args.strip()}"
        return f"Current model: {current}"
    
    reg.register(CommandDef(
        name="model", description="Show or change model",
        handler=cmd_model, category=CommandCategory.CONFIG,
        usage="/model [name] — view current or switch to another"
    ))
    
    def cmd_config(args: str, ctx: Dict) -> str:
        return "Configuration — use `hermes config` or edit config.yaml"
    
    reg.register(CommandDef(
        name="config", description="Show configuration",
        handler=cmd_config, category=CommandCategory.CONFIG,
        usage="/config — display current configuration"
    ))
    
    def cmd_mode(args: str, ctx: Dict) -> str:
        modes = ["auto", "hitl", "deny"]
        if args.strip() in modes:
            ctx['_set_approval_mode'] = args.strip()
            return f"Approval mode set to: {args.strip()}"
        return f"Current mode: {ctx.get('approval_mode', 'auto')}. Options: {', '.join(modes)}"
    
    reg.register(CommandDef(
        name="mode", description="Set approval mode (auto/hitl/deny)",
        handler=cmd_mode, category=CommandCategory.CONFIG,
        usage="/mode <auto|hitl|deny>"
    ))
    
    # ── Tools ──
    
    def cmd_tools(args: str, ctx: Dict) -> str:
        from tools.registry import get_registry
        r = get_registry()
        tools = r.list_all()
        cats = {}
        for t in tools:
            cats.setdefault(t.category, []).append(t.name)
        
        lines = [f"## Tools ({len(tools)} total)", ""]
        for cat, names in sorted(cats.items()):
            lines.append(f"**{cat}** ({len(names)}): {', '.join(names[:10])}")
        return "\n".join(lines)
    
    reg.register(CommandDef(
        name="tools", description="List available tools",
        handler=cmd_tools, category=CommandCategory.TOOLS,
        usage="/tools — shows all registered tools by category"
    ))
    
    def cmd_memory(args: str, ctx: Dict) -> str:
        return "Memory system — use /memory stats or /memory recall <query>"
    
    reg.register(CommandDef(
        name="memory", description="Memory system commands",
        handler=cmd_memory, category=CommandCategory.TOOLS,
        usage="/memory [stats|recall <query>|top]"
    ))
    
    # ── Workflow ──
    
    def cmd_plan(args: str, ctx: Dict) -> str:
        ctx['_enter_plan_mode'] = True
        ctx['_plan_topic'] = args.strip()
        return f"Entering plan mode{' for: ' + args.strip() if args.strip() else ''}..."
    
    reg.register(CommandDef(
        name="plan", description="Enter plan-only mode",
        handler=cmd_plan, category=CommandCategory.WORKFLOW,
        usage="/plan [topic] — generate plan without executing code"
    ))
    
    def cmd_review(args: str, ctx: Dict) -> str:
        ctx['_enter_review_mode'] = True
        return "Entering code review mode..."
    
    reg.register(CommandDef(
        name="review", description="Code review mode",
        handler=cmd_review, category=CommandCategory.WORKFLOW,
        usage="/review — review changes since last commit"
    ))
    
    def cmd_diff(args: str, ctx: Dict) -> str:
        return "Showing diff... (implement with git diff)"
    
    reg.register(CommandDef(
        name="diff", description="Show current diff",
        handler=cmd_diff, category=CommandCategory.WORKFLOW,
        usage="/diff — show git diff of current changes"
    ))
    
    # ── Info ──
    
    def cmd_help(args: str, ctx: Dict) -> str:
        return reg.format_help(args.strip() or None)
    
    reg.register(CommandDef(
        name="help", description="Show available commands",
        handler=cmd_help, category=CommandCategory.INFO,
        aliases=["h", "?"],
        usage="/help [command] — list all commands or get help on one"
    ))
    
    def cmd_status(args: str, ctx: Dict) -> str:
        lines = [
            "## Session Status",
            f"- Session: {ctx.get('session_id', 'N/A')}",
            f"- Model: {ctx.get('current_model', 'N/A')}",
            f"- Turns: {ctx.get('turn_count', 0)}",
            f"- Tools: {ctx.get('tool_count', 0)}",
            f"- Memory: {ctx.get('memory_count', 0)} items",
            f"- Approval: {ctx.get('approval_mode', 'auto')}",
        ]
        return "\n".join(lines)
    
    reg.register(CommandDef(
        name="status", description="Session status",
        handler=cmd_status, category=CommandCategory.INFO,
        usage="/status — show current session details"
    ))
    
    def cmd_usage(args: str, ctx: Dict) -> str:
        tokens = ctx.get('total_tokens', 0)
        cost = ctx.get('total_cost', 0)
        return f"Tokens used: {tokens:,} | Est. cost: ${cost:.4f}"
    
    reg.register(CommandDef(
        name="usage", description="Token usage stats",
        handler=cmd_usage, category=CommandCategory.INFO,
        usage="/usage — show token and cost estimates"
    ))
    
    def cmd_agents(args: str, ctx: Dict) -> str:
        return "Active agents: 1 (main)"
    
    reg.register(CommandDef(
        name="agents", description="Show active agents",
        handler=cmd_agents, category=CommandCategory.INFO,
        aliases=["tasks"],
        usage="/agents — list active sub-agents and tasks"
    ))
    
    # ── Debug ──
    
    def cmd_debug(args: str, ctx: Dict) -> str:
        import platform
        import sys
        lines = [
            "## Debug Info",
            f"- WW version: {ctx.get('version', 'N/A')}",
            f"- Python: {sys.version}",
            f"- Platform: {platform.platform()}",
            f"- Working dir: {os.getcwd()}",
        ]
        return "\n".join(lines)
    
    reg.register(CommandDef(
        name="debug", description="Debug information",
        handler=cmd_debug, category=CommandCategory.DEBUG,
        usage="/debug — show system diagnostic info"
    ))
    
    def cmd_save(args: str, ctx: Dict) -> str:
        ctx['_signal_save'] = True
        return "Saving conversation..."
    
    reg.register(CommandDef(
        name="save", description="Save conversation to file",
        handler=cmd_save, category=CommandCategory.SESSION,
        usage="/save — export current session transcript"
    ))
