"""coding/loop_bridge.py — Simulated loop user-message path for coding mode.

Functions the spiral loop would call when a mid-task user message arrives:
  - Detect redirect intent → apply_redirect (steerable)
  - Auto-autocompact when mock/real context is over threshold
  - Expand goal detection (shared with mode.is_coding_goal)

Used by live prove / unit tests without running a full LLM loop.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

# Redirect-ish patterns (EN + ZH)
_REDIRECT_RE = re.compile(
    r"(?i)\b("
    r"redirect|instead|now\s+focus|change\s+(to|focus)|focus\s+on|"
    r"switch\s+to|rather|please\s+work\s+on|ignore\s+that|"
    r"改成|改为|改为关注|转而|现在改|先做|不要.*改做|重新聚焦"
    r")\b"
)


def looks_like_redirect(message: str) -> bool:
    """Heuristic: user message is a mid-task steer/redirect."""
    s = (message or "").strip()
    if not s:
        return False
    if _REDIRECT_RE.search(s):
        return True
    # Short imperative re-aim often starts with "Instead" / "Now"
    if re.match(r"(?i)^(instead|now|rather|please\s+focus)\b", s):
        return True
    return False


def estimate_message_tokens(messages: List[Dict]) -> int:
    """Rough token estimate (~4 chars/token) for threshold checks."""
    total_chars = 0
    for m in messages or []:
        total_chars += len(str(m.get("content") or ""))
    return max(1, total_chars // 4) if total_chars else 0


def handle_coding_user_message(
    message: str,
    messages: List[Dict] = None,
    project_root: str = ".",
    goal: str = "",
    token_budget: int = None,
    force_redirect: bool = False,
    force_autocompact: bool = False,
) -> Dict[str, Any]:
    """Simulated loop path for a coding-session user message.

    Steps the real loop should mirror:
      1. If coding goal / active ticket → consider redirect
      2. If context over budget → autocompact (preserves edit_log)
      3. Return a structured result (never raw tool JSON as user summary)

    Returns:
      {
        success, redirect, autocompact, messages, metrics_delta,
        user_summary, coding_goal, ...
      }
    """
    from coding.mode import is_coding_goal
    from coding.orchestrator import apply_redirect, get_ticket_state, get_metrics

    message = (message or "").strip()
    messages = list(messages or [])
    project_root = project_root or "."
    if token_budget is None:
        token_budget = int(os.environ.get("WW_CODING_CONTEXT_BUDGET", "32000") or "32000")

    result: Dict[str, Any] = {
        "success": True,
        "message": message,
        "coding_goal": is_coding_goal(message) or is_coding_goal(goal),
        "redirect": None,
        "autocompact": None,
        "messages": messages,
        "metrics_delta": {"redirects": 0, "autocompacts": 0},
        "user_summary": "",
        "plan_state": get_ticket_state(),
    }

    # ── Redirect (steerable mid-task) ─────────────────────────────────
    do_redirect = force_redirect or looks_like_redirect(message)
    # Also redirect when there is an active ticket and user sends a re-aim
    ticket = get_ticket_state()
    active = ticket.get("status") in (
        "running", "redirected", "replanned", "handoff", "completed",
    ) and (ticket.get("goal") or ticket.get("subgoal"))
    if do_redirect or (active and looks_like_redirect(message)):
        redir = apply_redirect(message)
        result["redirect"] = {
            "success": redir.get("success"),
            "subgoal": redir.get("subgoal"),
            "prev_subgoal": redir.get("prev_subgoal"),
            "changed": redir.get("changed"),
            "version": redir.get("version"),
        }
        if redir.get("success"):
            result["metrics_delta"]["redirects"] = 1
            try:
                get_metrics().record_redirect()
            except Exception:
                pass
        result["plan_state"] = redir.get("plan_state") or get_ticket_state()

    # ── Auto autocompact near budget ──────────────────────────────────
    cur_tokens = estimate_message_tokens(messages)
    try:
        from coding.autocompact import should_autocompact, autocompact_messages
        need = force_autocompact or should_autocompact(
            current_tokens=cur_tokens,
            max_tokens=token_budget,
        )
        if need and messages:
            ac = autocompact_messages(
                messages,
                goal=goal or ticket.get("goal") or message[:120],
                project_root=project_root,
                max_tokens=token_budget,
                force=force_autocompact or need,
            )
            result["autocompact"] = {
                "triggered": ac.get("triggered"),
                "edit_log_preserved": ac.get("edit_log_preserved", True),
                "token_estimate": (ac.get("summary") or {}).get("token_estimate")
                if isinstance(ac.get("summary"), dict)
                else ac.get("current_tokens"),
                "summary_present": bool(ac.get("summary")),
            }
            if ac.get("triggered"):
                result["messages"] = ac.get("messages") or messages
                result["metrics_delta"]["autocompacts"] = 1
                try:
                    get_metrics().record_autocompact()
                except Exception:
                    pass
            else:
                result["messages"] = messages
        else:
            result["autocompact"] = {
                "triggered": False,
                "current_tokens": cur_tokens,
                "budget": token_budget,
            }
    except Exception as e:
        result["autocompact"] = {"triggered": False, "error": str(e)}

    # Reply-safe summary (never raw tool dump keys)
    parts = []
    if result.get("redirect") and result["redirect"].get("success"):
        parts.append(f"Redirected subgoal → {result['redirect'].get('subgoal')}")
    if result.get("autocompact") and result["autocompact"].get("triggered"):
        parts.append("Context autocompacted (edit_log preserved).")
    if not parts:
        if result["coding_goal"]:
            parts.append("Coding mode acknowledged user message.")
        else:
            parts.append("User message received.")
    result["user_summary"] = " ".join(parts)[:500]
    # Guard: never look like raw JSON
    if result["user_summary"].strip().startswith("{"):
        result["user_summary"] = "Coding loop updated plan state."
    return result


def apply_loop_coding_hooks(
    user_goal: str,
    messages: List[Dict] = None,
    project_root: str = ".",
    llm_client: Any = None,
    config: Dict = None,
) -> Dict[str, Any]:
    """Entry the spiral loop can call at start of a coding turn.

    - Builds coding context if goal matches
    - Resolves coding model route and applies to client
    - Handles redirect / autocompact for the user message path
    """
    from coding.mode import is_coding_goal, build_coding_context
    from coding.model_route import resolve_coding_model, apply_coding_model_to_client

    out: Dict[str, Any] = {
        "coding_mode": {"active": False},
        "model_route": None,
        "user_path": None,
    }
    if not is_coding_goal(user_goal):
        return out

    ctx = build_coding_context(goal=user_goal, project_root=project_root, force=True)
    out["coding_mode"] = ctx

    main_model = getattr(llm_client, "model", None) if llm_client else None
    main_provider = getattr(llm_client, "_provider", None) if llm_client else None
    route = resolve_coding_model(
        config=config,
        main_model=main_model,
        main_provider=main_provider,
        prefer_coding=True,
    )
    if llm_client is not None:
        apply_coding_model_to_client(llm_client, route)
    out["model_route"] = route

    out["user_path"] = handle_coding_user_message(
        message=user_goal,
        messages=messages or [],
        project_root=project_root,
        goal=user_goal,
    )
    return out
