"""coding/orchestrator.py — Default coding path orchestrator.

coding_run_ticket steps:
  repo_map → grep/graph locate → edit_symbol|apply_patch → verify
  → on fail: circuit + one replan (no infinite loops)
  same fingerprint threshold → stop + structured handoff

Also provides steerable mid-task redirect:
  apply_redirect(message) / coding_redirect tool updates subgoal/plan observably.

PM 0.10: CodingMetrics, max_tool_rounds, max_same_fp, explain_failure → replan.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from coding.policy import get_causal_state

# Max replans after verify failure (one replan by default — no thrashing)
MAX_REPLANS = int(os.environ.get("WW_CODING_MAX_REPLANS", "1") or "1")
# Same fingerprint strikes before hard handoff
SAME_FP_THRESHOLD = int(os.environ.get("WW_CODING_SAME_FP_THRESHOLD", "3") or "3")
# Alias env for max_same_fp (PM 0.10)
MAX_SAME_FP = int(
    os.environ.get("WW_CODING_MAX_SAME_FP")
    or os.environ.get("WW_CODING_SAME_FP_THRESHOLD", "3")
    or "3"
)
# Bound tool rounds per ticket (map+locate+edit+verify+replan count as rounds)
MAX_TOOL_ROUNDS = int(os.environ.get("WW_CODING_MAX_TOOL_ROUNDS", "20") or "20")


# ── CodingMetrics ─────────────────────────────────────────────────────

@dataclass
class CodingMetrics:
    """Exportable coding-session metrics (JSON fields for prove / dashboards)."""

    rounds: int = 0
    tools: int = 0
    verifies: int = 0
    redirects: int = 0
    trips: int = 0
    autocompacts: int = 0
    replans: int = 0
    samples: int = 0
    ticket_id: Optional[str] = None
    goal: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def record_round(self, n: int = 1) -> None:
        self.rounds += n

    def record_tool(self, n: int = 1) -> None:
        self.tools += n

    def record_verify(self, n: int = 1) -> None:
        self.verifies += n

    def record_redirect(self, n: int = 1) -> None:
        self.redirects += n

    def record_trip(self, n: int = 1) -> None:
        self.trips += n

    def record_autocompact(self, n: int = 1) -> None:
        self.autocompacts += n

    def record_replan(self, n: int = 1) -> None:
        self.replans += n

    def record_sample(self, n: int = 1) -> None:
        self.samples += n

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Canonical JSON fields required by PM 0.10
        return {
            "rounds": d["rounds"],
            "tools": d["tools"],
            "verifies": d["verifies"],
            "redirects": d["redirects"],
            "trips": d["trips"],
            "autocompacts": d["autocompacts"],
            "replans": d["replans"],
            "samples": d["samples"],
            "ticket_id": d["ticket_id"],
            "goal": d["goal"],
            "started_at": d["started_at"],
            "finished_at": d["finished_at"],
            "extra": d["extra"],
        }

    def export(self, path: str = None) -> str:
        """Serialize metrics JSON; optionally write to *path*. Returns JSON string."""
        payload = self.to_dict()
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if path:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return text


_metrics = CodingMetrics()


def get_metrics() -> CodingMetrics:
    return _metrics


def reset_metrics() -> CodingMetrics:
    global _metrics
    _metrics = CodingMetrics()
    return _metrics


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def get_max_tool_rounds() -> int:
    return _env_int("WW_CODING_MAX_TOOL_ROUNDS", MAX_TOOL_ROUNDS)


def get_max_same_fp() -> int:
    """Configurable same-fingerprint threshold (WW_CODING_MAX_SAME_FP or SAME_FP)."""
    raw = os.environ.get("WW_CODING_MAX_SAME_FP")
    if raw is not None and str(raw).strip() != "":
        try:
            return int(raw)
        except ValueError:
            pass
    return _env_int("WW_CODING_SAME_FP_THRESHOLD", SAME_FP_THRESHOLD)


# ── Ticket / plan state ───────────────────────────────────────────────

_ticket_state: Dict[str, Any] = {
    "ticket_id": None,
    "goal": "",
    "subgoal": "",
    "plan": [],
    "files_touched": [],
    "steps": [],
    "status": "idle",
    "redirects": [],
    "version": 0,
    "updated_at": None,
    "handoff": None,
    "verify": None,
    "circuit": None,
    "metrics": None,
}


def get_ticket_state() -> Dict[str, Any]:
    """Return a shallow copy of the current ticket/plan state."""
    return dict(_ticket_state)


def reset_ticket_state() -> None:
    global _ticket_state
    _ticket_state = {
        "ticket_id": None,
        "goal": "",
        "subgoal": "",
        "plan": [],
        "files_touched": [],
        "steps": [],
        "status": "idle",
        "redirects": [],
        "version": 0,
        "updated_at": None,
        "handoff": None,
        "verify": None,
        "circuit": None,
        "metrics": None,
    }


def _touch() -> None:
    _ticket_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _ticket_state["version"] = int(_ticket_state.get("version") or 0) + 1


def _record_step(name: str, result: Dict) -> None:
    steps = _ticket_state.setdefault("steps", [])
    steps.append({
        "name": name,
        "ok": bool(result.get("success", result.get("ok", True))) and not result.get("error"),
        "summary": (result.get("summary") or result.get("message") or result.get("error") or "")[:300],
        "ts": time.time(),
    })
    # Keep bounded
    if len(steps) > 40:
        _ticket_state["steps"] = steps[-40:]


def _default_plan(goal: str) -> List[Dict]:
    return [
        {"id": "s1", "title": "repo_map — ranked signature overview", "status": "pending"},
        {"id": "s2", "title": "grep/graph — locate target symbols", "status": "pending"},
        {"id": "s3", "title": "edit_symbol|apply_patch — minimal fix", "status": "pending"},
        {"id": "s4", "title": "verify — execution-grounded tests", "status": "pending"},
        {"id": "s5", "title": "circuit/replan on fail; handoff if thrashing", "status": "pending"},
    ]


# ── Steerable redirect ────────────────────────────────────────────────

def apply_redirect(message: str, subgoal: str = None) -> Dict:
    """Update current subgoal/plan observably from a mid-task redirect message.

    Returns the new plan state so callers can assert the field changed.
    """
    message = (message or "").strip()
    if not message:
        return {"success": False, "error": "Empty redirect message", "plan_state": get_ticket_state()}

    old_subgoal = _ticket_state.get("subgoal") or ""
    old_version = int(_ticket_state.get("version") or 0)

    # Derive a concise subgoal if not provided
    new_subgoal = (subgoal or "").strip()
    if not new_subgoal:
        # First non-empty line, trimmed
        new_subgoal = message.splitlines()[0].strip()[:200]
        # Strip common prefixes
        new_subgoal = re.sub(
            r"^(please\s+)?(redirect|instead|now|change\s+(to|focus)|focus\s+on)[:\s]+",
            "",
            new_subgoal,
            flags=re.I,
        ).strip() or message[:200]

    plan = list(_ticket_state.get("plan") or _default_plan(_ticket_state.get("goal") or ""))
    # Insert a redirect marker at the front of remaining work
    redirect_item = {
        "id": f"r{_ticket_state.get('version', 0) + 1}",
        "title": f"REDIRECT: {new_subgoal}",
        "status": "active",
        "redirect_message": message[:500],
    }
    # Mark previous active items deferred
    for p in plan:
        if p.get("status") in ("pending", "active", "in_progress"):
            p["status"] = "deferred"
    plan.insert(0, redirect_item)

    _ticket_state["subgoal"] = new_subgoal
    _ticket_state["plan"] = plan
    _ticket_state["status"] = "redirected"
    redirects = _ticket_state.setdefault("redirects", [])
    redirects.append({
        "message": message[:500],
        "subgoal": new_subgoal,
        "prev_subgoal": old_subgoal,
        "ts": time.time(),
    })
    _touch()
    try:
        get_metrics().record_redirect()
    except Exception:
        pass

    # Mirror into harness plan state for coding_replan visibility
    try:
        from coding import harness as _harness
        if hasattr(_harness, "_plan_state"):
            ps = dict(_harness._plan_state or {})
            ps["subgoal"] = new_subgoal
            ps["goal"] = _ticket_state.get("goal") or ps.get("goal", "")
            ps["subgoals"] = plan
            ps["version"] = int(ps.get("version", 0)) + 1
            ps["redirected"] = True
            ps["redirect_message"] = message[:500]
            _harness._plan_state = ps
    except Exception:
        pass

    return {
        "success": True,
        "subgoal": new_subgoal,
        "prev_subgoal": old_subgoal,
        "plan": plan,
        "version": _ticket_state["version"],
        "prev_version": old_version,
        "changed": new_subgoal != old_subgoal or _ticket_state["version"] != old_version,
        "plan_state": get_ticket_state(),
        "message": f"Subgoal redirected → {new_subgoal}",
    }


# ── Orchestrated ticket run ───────────────────────────────────────────

def coding_run_ticket(
    goal: str,
    project_root: str = ".",
    symbol: str = None,
    file_path: str = None,
    new_body: str = None,
    patch: str = None,
    test_path: str = None,
    grep_pattern: str = None,
    token_budget: int = 4000,
    max_replans: int = None,
    max_tool_rounds: int = None,
    max_same_fp: int = None,
) -> Dict:
    """Run the default coding path for a ticket (deterministic, no LLM).

    Steps: repo_map → grep/graph → edit → verify → on fail circuit + one replan.
    Same fingerprint threshold → stop + structured handoff.

    PM 0.10: max_tool_rounds / max_same_fp configurable; explain_failure feeds replan;
    sample_repair when WW_CODING_SAMPLES>0; CodingMetrics exported on result.
    """
    max_replans = MAX_REPLANS if max_replans is None else max_replans
    max_tool_rounds = get_max_tool_rounds() if max_tool_rounds is None else int(max_tool_rounds)
    max_same_fp = get_max_same_fp() if max_same_fp is None else int(max_same_fp)
    project_root = os.path.abspath(project_root or ".")
    goal = (goal or "").strip() or "coding ticket"

    metrics = reset_metrics()
    metrics.goal = goal
    metrics.started_at = datetime.now(timezone.utc).isoformat()

    reset_ticket_state()
    _ticket_state["ticket_id"] = f"t-{int(time.time())}"
    _ticket_state["goal"] = goal
    _ticket_state["subgoal"] = goal
    _ticket_state["plan"] = _default_plan(goal)
    _ticket_state["status"] = "running"
    metrics.ticket_id = _ticket_state["ticket_id"]
    _touch()

    # Activate coding mode (role=coder, essence available)
    try:
        from coding.mode import build_coding_context
        mode_ctx = build_coding_context(goal=goal, project_root=project_root, force=True)
    except Exception as e:
        mode_ctx = {"active": False, "error": str(e)}

    # Optional coding model route (log only in deterministic path)
    model_route = None
    try:
        from coding.model_route import resolve_coding_model
        model_route = resolve_coding_model(prefer_coding=True)
    except Exception as e:
        model_route = {"error": str(e)}

    tool_rounds = 0
    results: Dict[str, Any] = {
        "goal": goal,
        "project_root": project_root,
        "mode": mode_ctx,
        "model_route": model_route,
        "steps": {},
        "success": False,
        "handoff": None,
        "replans": 0,
        "max_tool_rounds": max_tool_rounds,
        "max_same_fp": max_same_fp,
        "metrics": None,
    }

    def _bump_tool(name: str = "") -> bool:
        """Increment tool/round counters; return False if max_tool_rounds exceeded."""
        nonlocal tool_rounds
        tool_rounds += 1
        metrics.record_tool()
        metrics.record_round()
        if tool_rounds > max_tool_rounds:
            return False
        return True

    # ── Step 1: repo_map ──────────────────────────────────────────────
    if not _bump_tool("repo_map"):
        return _finish_max_rounds(results, metrics, tool_rounds, max_tool_rounds)
    try:
        from coding.perception import repo_map
        map_r = repo_map(project_root, token_budget=token_budget)
        results["steps"]["repo_map"] = {
            "success": True,
            "truncated": map_r.get("truncated"),
            "symbols_included": map_r.get("symbols_included"),
            "token_estimate": map_r.get("token_estimate"),
        }
        _record_step("repo_map", results["steps"]["repo_map"])
        _set_plan_status("s1", "done")
    except Exception as e:
        results["steps"]["repo_map"] = {"success": False, "error": str(e)}
        _record_step("repo_map", results["steps"]["repo_map"])

    # ── Step 2: grep / graph locate ───────────────────────────────────
    if not _bump_tool("locate"):
        return _finish_max_rounds(results, metrics, tool_rounds, max_tool_rounds)
    pattern = grep_pattern or symbol or _guess_symbol(goal)
    locate: Dict[str, Any] = {"pattern": pattern}
    try:
        from coding.perception import grep
        if pattern:
            g = grep(pattern, path=project_root, glob="*.py", max_matches=30)
            locate["grep"] = {
                "count": g.get("count", 0),
                "matches": (g.get("matches") or [])[:10],
                "engine": g.get("engine"),
            }
            # Auto-pick first hit if file_path not given
            if not file_path and g.get("matches"):
                file_path = g["matches"][0].get("file")
        else:
            locate["grep"] = {"count": 0, "skipped": True}
    except Exception as e:
        locate["grep"] = {"error": str(e)}

    try:
        from coding.code_graph import CodeGraphStore
        store = CodeGraphStore(project_root=project_root)
        build_r = store.build(project_root, force=False)
        locate["graph_build"] = {
            "success": True,
            "nodes": (build_r or {}).get("nodes") if isinstance(build_r, dict) else None,
            "stats": store.stats(),
        }
        if pattern or symbol:
            target = symbol or pattern
            locate["who_calls"] = store.who_calls(target)
            locate["blast_radius"] = store.blast_radius(target, max_depth=3)
        store.close()
    except Exception as e:
        locate["graph"] = {"error": str(e)}

    results["steps"]["locate"] = locate
    _record_step("locate", {"success": True, "summary": f"pattern={pattern}"})
    _set_plan_status("s2", "done")

    # ── Step 3: edit ──────────────────────────────────────────────────
    if not _bump_tool("edit"):
        return _finish_max_rounds(results, metrics, tool_rounds, max_tool_rounds)
    edit_result: Dict[str, Any] = {"skipped": True}
    if new_body and file_path and symbol:
        try:
            from coding.aci import DefensiveEditor
            editor = DefensiveEditor(lint_enabled=True)
            edit_result = editor.edit_symbol(file_path, symbol, new_body)
            edit_result["method"] = "edit_symbol"
            if edit_result.get("success"):
                _ticket_state.setdefault("files_touched", []).append(file_path)
        except Exception as e:
            edit_result = {"success": False, "error": str(e), "method": "edit_symbol"}
    elif patch:
        try:
            from coding.aci import DefensiveEditor
            editor = DefensiveEditor(lint_enabled=True)
            edit_result = editor.apply_patch(patch)
            edit_result["method"] = "apply_patch"
            if edit_result.get("success"):
                for p in edit_result.get("files") or edit_result.get("paths") or []:
                    _ticket_state.setdefault("files_touched", []).append(p)
        except Exception as e:
            edit_result = {"success": False, "error": str(e), "method": "apply_patch"}
    results["steps"]["edit"] = edit_result
    _record_step("edit", edit_result)
    if not edit_result.get("skipped"):
        _set_plan_status("s3", "done" if edit_result.get("success") else "failed")
    else:
        _set_plan_status("s3", "skipped")

    # ── Step 4–5: verify + circuit/replan ──────────────────────────────
    replan_count = 0
    final_verify: Optional[Dict] = None
    handoff = None

    while True:
        if not _bump_tool("verify"):
            return _finish_max_rounds(results, metrics, tool_rounds, max_tool_rounds)
        try:
            from coding.harness import coding_verify
            # Prefer explicit test_path; else look under project
            tp = test_path
            if not tp:
                cand = os.path.join(project_root, "tests")
                if os.path.isdir(cand):
                    tp = cand
            final_verify = coding_verify(test_path=tp)
        except Exception as e:
            final_verify = {"success": False, "error": str(e), "fingerprint": "verify-error"}

        metrics.record_verify()
        results["steps"]["verify"] = final_verify
        _ticket_state["verify"] = final_verify
        _record_step("verify", final_verify)

        if final_verify.get("success"):
            _set_plan_status("s4", "done")
            _set_plan_status("s5", "done")
            _ticket_state["status"] = "completed"
            results["success"] = True
            break

        _set_plan_status("s4", "failed")
        fp = final_verify.get("fingerprint") or "unknown"
        err_text = (
            final_verify.get("summary")
            or final_verify.get("output")
            or final_verify.get("error")
            or "verify failed"
        )

        # explain_failure → replan context (PM 0.10)
        explain = {}
        try:
            from coding.perception import explain_failure
            explain = explain_failure(err_text)
            results["steps"]["explain_failure"] = {
                "summary": explain.get("summary"),
                "bullets": (explain.get("bullets") or [])[:8],
                "success": True,
            }
        except Exception as e:
            explain = {"summary": "", "bullets": [], "error": str(e)}
            results["steps"]["explain_failure"] = {"success": False, "error": str(e)}

        # Optional multi-sample repair scaffolds when WW_CODING_SAMPLES>0
        sample_info = None
        try:
            k = int(os.environ.get("WW_CODING_SAMPLES", "0") or "0")
        except ValueError:
            k = 0
        if k > 0 and file_path:
            try:
                from coding.harness import coding_sample_repair
                sample_info = coding_sample_repair(
                    file_path,
                    error_text=err_text,
                    hint=explain.get("summary") or "",
                )
                results["steps"]["sample_repair"] = {
                    "enabled": sample_info.get("enabled"),
                    "k": sample_info.get("k"),
                    "n_samples": len(sample_info.get("samples") or []),
                }
                if sample_info.get("enabled"):
                    metrics.record_sample(len(sample_info.get("samples") or []) or k)
            except Exception as e:
                results["steps"]["sample_repair"] = {"enabled": False, "error": str(e)}

        # Circuit tracking
        circuit_info = {}
        try:
            from coding.circuit import get_breaker
            br = get_breaker()
            track_path = file_path or (symbol or "ticket") + ".py"
            circuit_info = br.after_edit(
                track_path,
                success=False,
                error_text=err_text,
                diff="",
            )
            _ticket_state["circuit"] = circuit_info
        except Exception as e:
            circuit_info = {"error": str(e)}

        results["steps"]["circuit"] = circuit_info
        _record_step("circuit", circuit_info)

        # Same fingerprint threshold → structured handoff, stop
        same = int(circuit_info.get("same_fingerprint_count") or 0)
        tripped = bool(circuit_info.get("tripped"))
        if tripped or same >= max_same_fp:
            if tripped or same >= max_same_fp:
                metrics.record_trip()
            handoff = _build_handoff(
                goal=goal,
                fingerprint=fp,
                verify=final_verify,
                circuit=circuit_info,
                reason="same_fingerprint_threshold" if same >= max_same_fp else "circuit_tripped",
                explain=explain,
            )
            results["handoff"] = handoff
            _ticket_state["handoff"] = handoff
            _ticket_state["status"] = "handoff"
            _set_plan_status("s5", "handoff")
            results["success"] = False
            break

        # One replan then retry verify only if we still have replan budget
        if replan_count >= max_replans:
            handoff = _build_handoff(
                goal=goal,
                fingerprint=fp,
                verify=final_verify,
                circuit=circuit_info,
                reason="max_replans_exhausted",
                explain=explain,
            )
            results["handoff"] = handoff
            _ticket_state["handoff"] = handoff
            _ticket_state["status"] = "handoff"
            _set_plan_status("s5", "handoff")
            results["success"] = False
            break

        # Replan with explain_failure bullets in context
        explain_notes = ""
        if explain.get("bullets"):
            explain_notes = " | explain: " + "; ".join(str(b) for b in explain["bullets"][:5])
        elif explain.get("summary"):
            explain_notes = " | explain: " + str(explain["summary"])[:200]
        try:
            from coding.harness import coding_replan
            rp = coding_replan(
                goal=goal,
                failure_fingerprints=[fp],
                notes=(
                    f"orchestrator replan #{replan_count + 1}"
                    f"{explain_notes}"
                )[:500],
                explain=explain,
            )
            results["steps"][f"replan_{replan_count + 1}"] = {
                "success": rp.get("success"),
                "subgoals": len(rp.get("subgoals") or []),
                "message": rp.get("message"),
                "explain_used": bool(explain.get("bullets") or explain.get("summary")),
            }
            _ticket_state["plan"] = rp.get("subgoals") or _ticket_state.get("plan")
            _ticket_state["subgoal"] = (rp.get("subgoals") or [{}])[0].get(
                "title", _ticket_state.get("subgoal")
            )
            _record_step("replan", results["steps"][f"replan_{replan_count + 1}"])
            metrics.record_replan()
        except Exception as e:
            results["steps"][f"replan_{replan_count + 1}"] = {"success": False, "error": str(e)}

        replan_count += 1
        results["replans"] = replan_count
        # Without a new edit, re-verify will fail the same way — orchestrator
        # does not invent code. Exit after recording the replan so the agent
        # (or E2E) can apply a new edit. No infinite loop.
        handoff = _build_handoff(
            goal=goal,
            fingerprint=fp,
            verify=final_verify,
            circuit=circuit_info,
            reason="replan_recorded_awaiting_new_edit",
            replan_count=replan_count,
            explain=explain,
        )
        results["handoff"] = handoff
        _ticket_state["handoff"] = handoff
        _ticket_state["status"] = "replanned"
        _set_plan_status("s5", "replanned")
        break

    return _finalize_ticket(results, metrics)


def _finish_max_rounds(
    results: Dict,
    metrics: CodingMetrics,
    tool_rounds: int,
    max_tool_rounds: int,
) -> Dict:
    handoff = {
        "type": "coding_handoff",
        "reason": "max_tool_rounds",
        "tool_rounds": tool_rounds,
        "max_tool_rounds": max_tool_rounds,
        "message": (
            f"Coding handoff (max_tool_rounds={max_tool_rounds}): "
            "stop thrashing; inspect plan and verify."
        ),
    }
    results["handoff"] = handoff
    results["success"] = False
    _ticket_state["handoff"] = handoff
    _ticket_state["status"] = "handoff"
    return _finalize_ticket(results, metrics)


def _finalize_ticket(results: Dict, metrics: CodingMetrics) -> Dict:
    metrics.finished_at = datetime.now(timezone.utc).isoformat()
    metrics_dict = metrics.to_dict()
    _ticket_state["metrics"] = metrics_dict
    _touch()
    results["plan_state"] = get_ticket_state()
    results["files_touched"] = list(_ticket_state.get("files_touched") or [])
    results["status"] = _ticket_state.get("status")
    results["metrics"] = metrics_dict
    # public_reply-safe summary (never dump raw tool JSON as user reply)
    results["user_summary"] = _user_summary(results)
    return results


def _set_plan_status(plan_id: str, status: str) -> None:
    for p in _ticket_state.get("plan") or []:
        if p.get("id") == plan_id:
            p["status"] = status
            break


def _guess_symbol(goal: str) -> Optional[str]:
    """Heuristic: extract a likely symbol name from the goal string."""
    if not goal:
        return None
    # backtick name
    m = re.search(r"`([A-Za-z_][\w.]*)`", goal)
    if m:
        return m.group(1)
    # def/class foo
    m = re.search(r"\b(?:def|class|function|method)\s+([A-Za-z_]\w*)", goal, re.I)
    if m:
        return m.group(1)
    # fix foo / edit bar
    m = re.search(r"\b(?:fix|edit|update|change|implement)\s+([A-Za-z_]\w*)", goal, re.I)
    if m:
        return m.group(1)
    return None


def _build_handoff(
    goal: str,
    fingerprint: str,
    verify: Dict,
    circuit: Dict,
    reason: str,
    replan_count: int = 0,
    explain: Dict = None,
) -> Dict:
    explain = explain or {}
    return {
        "type": "coding_handoff",
        "reason": reason,
        "goal": goal,
        "fingerprint": fingerprint,
        "verify_summary": (verify or {}).get("summary") or (verify or {}).get("error"),
        "circuit": {
            "tripped": (circuit or {}).get("tripped"),
            "same_fingerprint_count": (circuit or {}).get("same_fingerprint_count"),
        },
        "explain_summary": explain.get("summary") or "",
        "explain_bullets": (explain.get("bullets") or [])[:8],
        "replan_count": replan_count,
        "files_touched": list(_ticket_state.get("files_touched") or []),
        "plan": list(_ticket_state.get("plan") or []),
        "subgoal": _ticket_state.get("subgoal"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "message": (
            f"Coding handoff ({reason}): stop thrashing. "
            f"fingerprint={fingerprint}. "
            "Human or next agent should inspect verify output and plan."
        ),
    }


# Keys that must never appear as a raw tool-dump user reply
_RAW_DUMP_KEYS = (
    "tool_calls", "function_call", "raw_tool", "tool_result",
    "handler_output", "arguments_json",
)


def _user_summary(results: Dict) -> str:
    """Short natural-language summary safe for public_reply (no raw JSON dump)."""
    status = results.get("status") or ("ok" if results.get("success") else "failed")
    parts = [f"Coding ticket {status}."]
    if results.get("success"):
        parts.append("Verify is green.")
    elif results.get("handoff"):
        h = results["handoff"]
        parts.append(h.get("message") or f"Handoff: {h.get('reason')}")
    steps = results.get("steps") or {}
    if steps.get("edit") and not steps["edit"].get("skipped"):
        ok = steps["edit"].get("success")
        parts.append("Edit " + ("applied." if ok else "failed."))
    if steps.get("verify"):
        v = steps["verify"]
        if v.get("success"):
            parts.append(f"Tests: {v.get('passed', '?')} passed.")
        else:
            parts.append(v.get("summary") or "Tests failed.")
    text = " ".join(parts)
    # Never return something that looks like a tool JSON dump
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        text = f"Coding ticket {status}."
    for k in _RAW_DUMP_KEYS:
        if k in text and ("{" in text or f'"{k}"' in text):
            text = f"Coding ticket {status}."
            break
    return text[:800]


def summary_has_raw_tool_dump(summary: str) -> bool:
    """Return True if *summary* looks like a raw tool JSON dump (for proves)."""
    if not summary:
        return False
    s = summary.strip()
    if s.startswith("{") or s.startswith("["):
        return True
    for k in _RAW_DUMP_KEYS:
        if f'"{k}"' in s or f"'{k}'" in s:
            return True
    return False


# ── Tool registration ─────────────────────────────────────────────────

def get_orchestrator_tools() -> List[Dict]:
    return [
        {
            "name": "coding_run_ticket",
            "description": (
                "Run the default coding path for a ticket: "
                "repo_map → grep/graph → edit_symbol|apply_patch → verify → "
                "circuit + one replan on fail (no infinite loops). "
                "Returns a short user_summary plus structured steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Ticket goal"},
                    "project_root": {"type": "string", "default": "."},
                    "symbol": {"type": "string"},
                    "file_path": {"type": "string"},
                    "new_body": {"type": "string", "description": "Full def/class replacement for edit_symbol"},
                    "patch": {"type": "string", "description": "Unified diff for apply_patch"},
                    "test_path": {"type": "string"},
                    "grep_pattern": {"type": "string"},
                    "token_budget": {"type": "integer", "default": 4000},
                    "max_replans": {"type": "integer", "default": 1},
                },
                "required": ["goal"],
            },
            "handler": lambda goal, project_root=".", symbol=None, file_path=None,
                              new_body=None, patch=None, test_path=None,
                              grep_pattern=None, token_budget=4000, max_replans=None,
                              max_tool_rounds=None, max_same_fp=None: coding_run_ticket(
                goal=goal,
                project_root=project_root,
                symbol=symbol,
                file_path=file_path,
                new_body=new_body,
                patch=patch,
                test_path=test_path,
                grep_pattern=grep_pattern,
                token_budget=token_budget,
                max_replans=max_replans,
                max_tool_rounds=max_tool_rounds,
                max_same_fp=max_same_fp,
            ),
            "category": "code_planning",
            "permission": "requires_approval",
        },
        {
            "name": "coding_redirect",
            "description": (
                "Steer mid-task: update the current coding subgoal/plan observably. "
                "Use when the user redirects focus during a coding ticket."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Redirect instruction from user"},
                    "subgoal": {"type": "string", "description": "Optional explicit new subgoal"},
                },
                "required": ["message"],
            },
            "handler": lambda message, subgoal=None: apply_redirect(message, subgoal),
            "category": "code_planning",
            "permission": "safe",
        },
        {
            "name": "coding_ticket_status",
            "description": "Get current coding ticket/plan state (subgoal, plan, redirects, handoff).",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: {"success": True, "plan_state": get_ticket_state()},
            "category": "code_planning",
            "permission": "safe",
        },
    ]
