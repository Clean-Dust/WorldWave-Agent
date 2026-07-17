"""coding/autocompact.py — Structured coding summary near context budget.

Produces a compact structured summary (goal, files touched, test status,
open issues) ≤ N tokens. Layers with microcompact for tool outputs.
Does NOT destroy audit / edit_log.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from coding.microcompact import fingerprint, compact_text

# Default token budget for the structured summary itself
DEFAULT_SUMMARY_TOKENS = int(os.environ.get("WW_CODING_AUTOCOMPACT_TOKENS", "800") or "800")
# Trigger when estimated context tokens exceed this ratio of max
DEFAULT_TRIGGER_RATIO = float(os.environ.get("WW_CODING_AUTOCOMPACT_RATIO", "0.85") or "0.85")
# Absolute token threshold fallback
DEFAULT_TOKEN_BUDGET = int(os.environ.get("WW_CODING_CONTEXT_BUDGET", "32000") or "32000")


def _est_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _read_edit_log_tail(project_root: str, max_lines: int = 20) -> List[Dict]:
    """Read last N edit_log entries without mutating the log."""
    path = os.path.join(project_root, ".ww", "edit_log.jsonl")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        out = []
        for line in lines[-max_lines:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"raw": line[:200]})
        return out
    except OSError:
        return []


def build_coding_summary(
    goal: str = "",
    files_touched: List[str] = None,
    test_status: Dict = None,
    open_issues: List[str] = None,
    plan: List[Dict] = None,
    subgoal: str = "",
    project_root: str = ".",
    max_tokens: int = DEFAULT_SUMMARY_TOKENS,
    extra: Dict = None,
) -> Dict[str, Any]:
    """Build a structured coding summary bounded to *max_tokens*.

    Preserves references to edit_log (does not delete or rewrite the log).
    """
    project_root = os.path.abspath(project_root or ".")
    files_touched = list(files_touched or [])
    open_issues = list(open_issues or [])
    plan = list(plan or [])
    test_status = dict(test_status or {})

    # Pull live orchestrator state if available
    try:
        from coding.orchestrator import get_ticket_state
        st = get_ticket_state()
        if not goal:
            goal = st.get("goal") or ""
        if not subgoal:
            subgoal = st.get("subgoal") or ""
        if not files_touched:
            files_touched = list(st.get("files_touched") or [])
        if not plan:
            plan = list(st.get("plan") or [])
        if not test_status and st.get("verify"):
            test_status = {
                "success": st["verify"].get("success"),
                "summary": st["verify"].get("summary"),
                "fingerprint": st["verify"].get("fingerprint"),
            }
        if st.get("handoff") and not open_issues:
            open_issues.append(st["handoff"].get("message") or st["handoff"].get("reason") or "handoff")
    except Exception:
        pass

    # Causal / verify
    try:
        from coding.policy import get_causal_state
        causal = get_causal_state().to_dict()
    except Exception:
        causal = {}

    edit_tail = _read_edit_log_tail(project_root, max_lines=15)
    edit_paths = []
    for e in edit_tail:
        p = e.get("path") or e.get("file") or e.get("filepath")
        if p and p not in edit_paths:
            edit_paths.append(p)
        if p and p not in files_touched:
            files_touched.append(p)

    # Build structured sections
    lines = [
        "# Coding AutoCompact Summary",
        f"ts: {datetime.now(timezone.utc).isoformat()}",
        f"goal: {(goal or '(none)')[:200]}",
        f"subgoal: {(subgoal or goal or '(none)')[:200]}",
        "",
        "## Files touched",
    ]
    if files_touched:
        for f in files_touched[:30]:
            lines.append(f"- {f}")
    else:
        lines.append("- (none recorded)")

    lines.append("")
    lines.append("## Test status")
    if test_status:
        lines.append(f"- success: {test_status.get('success')}")
        if test_status.get("summary"):
            lines.append(f"- summary: {str(test_status['summary'])[:240]}")
        if test_status.get("fingerprint"):
            lines.append(f"- fingerprint: {test_status['fingerprint']}")
        if test_status.get("passed") is not None:
            lines.append(f"- passed: {test_status.get('passed')} failed: {test_status.get('failed')}")
    else:
        lines.append("- (no verify yet)")
        if causal.get("last_verify_ok") is not None:
            lines.append(f"- causal.last_verify_ok: {causal.get('last_verify_ok')}")

    lines.append("")
    lines.append("## Plan (active)")
    active = [p for p in plan if p.get("status") in ("pending", "active", "in_progress", "deferred")]
    show = active or plan
    for p in show[:12]:
        lines.append(f"- [{p.get('status', '?')}] {p.get('title') or p.get('id') or p}")

    lines.append("")
    lines.append("## Open issues")
    if open_issues:
        for iss in open_issues[:15]:
            lines.append(f"- {str(iss)[:200]}")
    else:
        lines.append("- (none)")

    lines.append("")
    lines.append("## Audit")
    lines.append(f"- edit_log entries (tail): {len(edit_tail)} (file preserved at .ww/edit_log.jsonl)")
    lines.append(f"- pending_writes: {len(causal.get('pending_writes') or [])}")
    if extra:
        lines.append(f"- extra: {json.dumps(extra, default=str)[:300]}")

    text = "\n".join(lines)
    char_limit = max(200, max_tokens * 4)
    if _est_tokens(text) > max_tokens:
        c = compact_text(text, limit=char_limit)
        text = c["text"]
        truncated = True
        fp = c["fingerprint"]
    else:
        truncated = False
        fp = fingerprint(text)

    return {
        "summary": text,
        "token_estimate": _est_tokens(text),
        "max_tokens": max_tokens,
        "truncated": truncated,
        "fingerprint": fp,
        "goal": goal,
        "subgoal": subgoal,
        "files_touched": files_touched[:50],
        "test_status": test_status,
        "open_issues": open_issues[:20],
        "edit_log_preserved": True,
        "edit_log_tail_count": len(edit_tail),
        "ts": time.time(),
    }


def should_autocompact(
    current_tokens: int = None,
    max_tokens: int = DEFAULT_TOKEN_BUDGET,
    ratio: float = DEFAULT_TRIGGER_RATIO,
    messages: List[Dict] = None,
) -> bool:
    """Return True when context is near budget and autocompact should fire."""
    if current_tokens is None and messages is not None:
        total = 0
        for m in messages:
            total += _est_tokens(str(m.get("content") or ""))
        current_tokens = total
    if current_tokens is None:
        return False
    if max_tokens <= 0:
        return False
    return current_tokens >= int(max_tokens * ratio)


def autocompact_messages(
    messages: List[Dict],
    goal: str = "",
    project_root: str = ".",
    max_tokens: int = DEFAULT_TOKEN_BUDGET,
    summary_tokens: int = DEFAULT_SUMMARY_TOKENS,
    keep_last: int = 6,
    force: bool = False,
) -> Dict[str, Any]:
    """Layered compact: structured coding summary + drop/middle-compress messages.

    - Never deletes .ww/edit_log.jsonl
    - Keeps recent *keep_last* messages
    - Inserts a system summary block with goal / files / tests / issues
    - Applies microcompact to oversized individual messages
    """
    messages = list(messages or [])
    cur = sum(_est_tokens(str(m.get("content") or "")) for m in messages)
    triggered = force or should_autocompact(current_tokens=cur, max_tokens=max_tokens)
    if not triggered:
        return {
            "triggered": False,
            "messages": messages,
            "current_tokens": cur,
            "summary": None,
        }

    # Gather test status from causal if present
    test_status = {}
    try:
        from coding.policy import get_causal_state
        lv = get_causal_state().to_dict().get("last_verify") or {}
        if lv:
            test_status = {
                "success": lv.get("success"),
                "summary": lv.get("summary"),
                "fingerprint": lv.get("fingerprint"),
                "passed": lv.get("passed"),
                "failed": lv.get("failed"),
            }
    except Exception:
        pass

    summary = build_coding_summary(
        goal=goal,
        test_status=test_status,
        project_root=project_root,
        max_tokens=summary_tokens,
    )

    # Keep system messages + last N non-system; middle becomes summary
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other = [m for m in messages if m.get("role") != "system"]
    head = other[:1] if other else []  # first user turn
    tail = other[-keep_last:] if keep_last > 0 else []
    # Avoid duplicating head into tail
    if head and tail and head[0] is tail[0] and len(other) <= keep_last + 1:
        middle_replaced = []
    else:
        middle_replaced = [{
            "role": "system",
            "content": summary["summary"],
            "metadata": {
                "autocompact": True,
                "fingerprint": summary["fingerprint"],
                "edit_log_preserved": True,
            },
        }]

    # Microcompact oversized retained messages (layered)
    from coding.microcompact import compact_result
    field_limit = int(os.environ.get("WW_CODING_MICROCOMPACT_LIMIT", "6000") or "6000")

    def _mc(m: Dict) -> Dict:
        content = m.get("content")
        if isinstance(content, str) and len(content) > field_limit:
            c = compact_text(content, limit=field_limit)
            nm = dict(m)
            nm["content"] = c["text"]
            nm.setdefault("metadata", {})
            if isinstance(nm["metadata"], dict):
                nm["metadata"] = dict(nm["metadata"])
                nm["metadata"]["microcompact"] = True
                nm["metadata"]["fingerprint"] = c["fingerprint"]
            return nm
        if isinstance(content, (dict, list)):
            nm = dict(m)
            nm["content"] = compact_result(content, limit=field_limit)
            return nm
        return m

    new_msgs: List[Dict] = []
    seen_sys = set()
    for m in system_msgs:
        key = id(m)
        if key in seen_sys:
            continue
        seen_sys.add(key)
        # Skip prior autocompact blocks (replace with new)
        meta = m.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("autocompact"):
            continue
        content = m.get("content") or ""
        if "Coding AutoCompact Summary" in str(content):
            continue
        new_msgs.append(_mc(m))

    for m in middle_replaced:
        new_msgs.append(m)
    # head + tail without double-adding
    for m in head:
        if m not in tail:
            new_msgs.append(_mc(m))
    for m in tail:
        new_msgs.append(_mc(m))

    new_tokens = sum(_est_tokens(str(m.get("content") or "")) for m in new_msgs)
    return {
        "triggered": True,
        "messages": new_msgs,
        "current_tokens": cur,
        "new_tokens": new_tokens,
        "summary": summary,
        "edit_log_preserved": True,
        "fingerprint": summary["fingerprint"],
    }


def coding_autocompact(
    goal: str = "",
    project_root: str = ".",
    max_tokens: int = DEFAULT_SUMMARY_TOKENS,
    messages: List[Dict] = None,
    force: bool = True,
) -> Dict:
    """Tool-facing API: return structured summary (and optionally compact messages)."""
    if messages:
        return autocompact_messages(
            messages,
            goal=goal,
            project_root=project_root,
            summary_tokens=max_tokens,
            force=force,
        )
    summary = build_coding_summary(
        goal=goal,
        project_root=project_root,
        max_tokens=max_tokens,
    )
    return {
        "success": True,
        "triggered": True,
        "summary": summary["summary"],
        "structured": summary,
        "edit_log_preserved": True,
        "fingerprint": summary["fingerprint"],
        "token_estimate": summary["token_estimate"],
    }


def get_autocompact_tools() -> List[Dict]:
    return [
        {
            "name": "coding_autocompact",
            "description": (
                "Build a structured coding context summary (goal, files touched, "
                "test status, open issues) near the token budget. "
                "Does not destroy edit_log/audit. Layers with microcompact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "project_root": {"type": "string", "default": "."},
                    "max_tokens": {"type": "integer", "default": DEFAULT_SUMMARY_TOKENS},
                    "force": {"type": "boolean", "default": True},
                },
            },
            "handler": lambda goal="", project_root=".", max_tokens=DEFAULT_SUMMARY_TOKENS, force=True: coding_autocompact(
                goal=goal,
                project_root=project_root,
                max_tokens=max_tokens,
                force=force,
            ),
            "category": "code_planning",
            "permission": "safe",
        },
    ]
