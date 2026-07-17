"""coding/mode.py — Coding-mode auto-detect and context injection.

When a user goal looks like a coding task:
  1. Inject a short CODING_AGENT.md essence into system/context
  2. Auto-load AGENTS.md when present
  3. Default capability role = coder (architect cannot edit)
  4. Hint coding_tool_search when many tools are registered
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Max chars of CODING_AGENT essence injected into system prompt
ESSENCE_MAX_CHARS = int(os.environ.get("WW_CODING_ESSENCE_MAX", "1800") or "1800")
# Tool-count threshold above which we hint coding_tool_search
TOOL_SEARCH_HINT_THRESHOLD = int(os.environ.get("WW_CODING_TOOL_HINT_THRESHOLD", "40") or "40")

# Keywords / patterns that signal a coding goal (EN + ZH, PM 0.10 expanded)
_CODING_KEYWORDS = {
    # English core
    "fix", "bug", "bugfix", "implement", "refactor", "edit", "patch", "test",
    "pytest", "unittest", "compile", "syntax", "function", "class", "module",
    "import", "typeerror", "attributeerror", "nameerror", "importerror", "traceback",
    "pr", "pull request", "commit", "diff", "lint", "mypy", "ruff",
    "codebase", "repo", "repository", "source code", "write code",
    "add test", "unit test", "integration test", "failing test", "write tests",
    "write test", "add tests", "coding_", "edit_symbol", "apply_patch", "repo_map",
    "debug", "stacktrace", "stack trace", "regression", "hotfix", "code review",
    # Chinese
    "修复", "重构", "实现", "代码", "测试", "函数", "模块", "报错",
    "修 bug", "修bug", "写测试", "写单测", "单元测试", "集成测试",
    "实现功能", "重构代码", "修复缺陷", "调试", "堆栈",
}

# Strong single-signal phrases (EN + ZH) — one hit is enough with light context
_STRONG_CODING_PHRASES = (
    "bugfix", "write tests", "write test", "add tests", "failing test",
    "unit test", "implement ", "refactor ", "fix the bug", "fix bug",
    "写测试", "写单测", "修复缺陷", "重构代码", "实现功能", "修 bug", "修bug",
)

_CODING_FILE_EXT = re.compile(
    r"\b[\w./-]+\.(py|js|ts|tsx|jsx|go|rs|java|c|cpp|h|hpp|rb|php|cs|kt|swift|sh|md)\b",
    re.I,
)
_CODING_PATHISH = re.compile(r"(^|[\s`])(src/|tests?/|coding/|core/|tools/|pkg/)")
_CODING_SYMBOL = re.compile(r"\b(def|class|async def)\s+\w+")


def is_coding_goal(text: str) -> bool:
    """Return True if *text* looks like a software-engineering / coding task."""
    if not text or not str(text).strip():
        return False
    s = str(text).strip()
    lower = s.lower()

    # Explicit force flags
    force = os.environ.get("WW_CODING_MODE", "").strip().lower()
    if force in ("1", "true", "yes", "on", "always"):
        return True
    if force in ("0", "false", "no", "off", "never"):
        return False

    # File path / extension signals
    if _CODING_FILE_EXT.search(s) or _CODING_PATHISH.search(s) or _CODING_SYMBOL.search(s):
        return True

    # Strong phrases (bugfix / implement / refactor / write tests EN+ZH)
    for phrase in _STRONG_CODING_PHRASES:
        if phrase in lower or phrase in s:
            return True

    # Keyword hits (need at least one strong signal, or two weak ones)
    hits = sum(1 for kw in _CODING_KEYWORDS if kw in lower or kw in s)
    if hits >= 2:
        return True
    if hits >= 1 and any(
        w in lower
        for w in (
            "file", "line", "error", "fail", "broken", "stack", "exception",
            "function", "method", "module", "package", "api", "endpoint",
            "code", "test", "bug", "代码", "测试", "函数", "模块",
        )
    ):
        return True
    # Single strong English verbs alone still count when goal is short
    if hits >= 1 and any(
        re.search(rf"\b{re.escape(w)}\b", lower)
        for w in ("bugfix", "implement", "refactor", "hotfix")
    ):
        return True

    # Tool-name references
    if "coding_" in lower or "edit_symbol" in lower:
        return True

    return False


def load_coding_agent_essence(max_chars: int = ESSENCE_MAX_CHARS) -> str:
    """Load a short essence of CODING_AGENT.md for system injection."""
    candidates = [
        Path(__file__).resolve().parent / "CODING_AGENT.md",
        Path.cwd() / "coding" / "CODING_AGENT.md",
    ]
    text = ""
    for p in candidates:
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                break
        except OSError:
            continue
    if not text:
        text = (
            "# CODING_AGENT essence\n"
            "Map → grep → graph → outline → edit_symbol → verify → circuit/replan.\n"
            "Prefer coding_edit_symbol; run coding_verify; architect cannot edit.\n"
        )
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + "\n\n…[CODING_AGENT essence truncated]"
    return text.strip()


def load_agents_md(project_root: str = None) -> str:
    """Auto-load AGENTS.md when present (project root)."""
    try:
        from coding.planning import AgentConfig
        ac = AgentConfig(project_root)
        content = ac.load_global()
        if content:
            return content.strip()
    except Exception:
        pass
    # Direct fallback
    roots = []
    if project_root:
        roots.append(Path(project_root))
    roots.append(Path.cwd())
    for root in roots:
        for name in ("AGENTS.md", "AGENTS.override.md"):
            p = root / name
            try:
                if p.is_file():
                    return p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
    return ""


def ensure_coder_role() -> Dict:
    """Default capability role = coder for coding mode. Returns status dict."""
    role_env = os.environ.get("WW_CODING_ROLE", "").strip().lower()
    # Explicit architect stays architect (caller must switch); default is coder
    target = role_env if role_env in ("architect", "coder", "reviewer") else "coder"
    if not role_env:
        os.environ["WW_CODING_ROLE"] = "coder"
        target = "coder"
    try:
        from coding.sandbox import get_manager
        mgr = get_manager()
        if mgr.mutex.role != target:
            mgr.switch_role(target)
        return {
            "role": mgr.mutex.role,
            "can_edit": "edit" in mgr.mutex._capabilities,
            "ensured": True,
        }
    except Exception as e:
        return {"role": target, "can_edit": target == "coder", "ensured": False, "error": str(e)}


def tool_search_hint(tool_count: int = None) -> str:
    """Hint to use coding_tool_search when many tools are registered."""
    if tool_count is None:
        try:
            from coding import get_tool_count
            tool_count = get_tool_count()
        except Exception:
            tool_count = 0
    if tool_count >= TOOL_SEARCH_HINT_THRESHOLD:
        return (
            f"There are ~{tool_count} coding/tools available. "
            "Use `coding_tool_search` with a short description to find the right tool "
            "instead of guessing names."
        )
    return ""


def build_coding_context(
    goal: str = "",
    project_root: str = None,
    force: bool = False,
    tool_count: int = None,
) -> Dict:
    """Detect coding goal and build injectable context blocks.

    Returns:
      {
        active: bool,
        role: {...},
        essence: str,
        agents_md: str,
        tool_hint: str,
        system_block: str,   # ready to append to system prompt
      }
    """
    active = force or is_coding_goal(goal)
    if not active:
        return {
            "active": False,
            "role": {},
            "essence": "",
            "agents_md": "",
            "tool_hint": "",
            "system_block": "",
        }

    role_info = ensure_coder_role()
    essence = load_coding_agent_essence()
    agents = load_agents_md(project_root)
    hint = tool_search_hint(tool_count)

    parts: List[str] = [
        "## Coding Mode (auto)",
        f"Capability role: **{role_info.get('role', 'coder')}** "
        f"(architect cannot edit; default is coder).",
        "",
        "### CODING_AGENT essence",
        essence,
    ]
    if agents:
        # Bound AGENTS.md so we don't blow the prompt
        agents_snip = agents if len(agents) <= 4000 else agents[:3960] + "\n…[AGENTS.md truncated]"
        parts.extend(["", "### Project AGENTS.md", agents_snip])
    if hint:
        parts.extend(["", "### Tool discovery", hint])

    system_block = "\n".join(parts)
    return {
        "active": True,
        "role": role_info,
        "essence": essence,
        "agents_md": agents,
        "tool_hint": hint,
        "system_block": system_block,
    }


def inject_coding_mode(
    messages: List[Dict],
    goal: str = "",
    project_root: str = None,
    force: bool = False,
) -> Tuple[List[Dict], Dict]:
    """Inject coding-mode system block into a message list.

    Returns (new_messages, context_dict).
    """
    # Infer goal from last user message if not provided
    if not goal:
        for m in reversed(messages or []):
            if m.get("role") == "user":
                goal = str(m.get("content") or "")
                break

    ctx = build_coding_context(goal=goal, project_root=project_root, force=force)
    if not ctx["active"] or not ctx["system_block"]:
        return list(messages or []), ctx

    msgs = list(messages or [])
    block = ctx["system_block"]
    has_system = any(m.get("role") == "system" for m in msgs)
    if not has_system:
        msgs.insert(0, {"role": "system", "content": block})
    else:
        for i, m in enumerate(msgs):
            if m.get("role") == "system":
                existing = m.get("content") or ""
                # Avoid double-injection
                if "Coding Mode (auto)" in existing or "CODING_AGENT essence" in existing:
                    break
                msgs[i] = {"role": "system", "content": existing + "\n\n" + block}
                break
    return msgs, ctx


def architect_cannot_edit_proof(tool_name: str = "coding_edit_symbol") -> Dict:
    """Prove architect role is denied edit tools (for tests / prove harness)."""
    from coding.sandbox import CapabilityMutex
    from coding.policy import architect_cannot_edit

    arch = CapabilityMutex("architect")
    coder = CapabilityMutex("coder")
    arch_check = arch.check_tool(tool_name)
    coder_check = coder.check_tool(tool_name)
    policy = architect_cannot_edit("architect", tool_name)
    return {
        "architect_allowed": arch_check.get("allowed"),
        "coder_allowed": coder_check.get("allowed"),
        "policy_allowed": policy.get("allowed"),
        "ok": (
            arch_check.get("allowed") is False
            and coder_check.get("allowed") is True
            and policy.get("allowed") is False
        ),
        "architect_reason": arch_check.get("reason") or policy.get("reason"),
    }
