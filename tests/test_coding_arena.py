"""Tests for coding arena loader, hidden-test isolation, and smoke harness."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tests" / "fixtures" / "coding_arena" / "tasks"
sys.path.insert(0, str(ROOT))


def _arena_mod():
    import importlib.util

    path = ROOT / "scripts" / "coding_arena.py"
    name = "ww_coding_arena_mod"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec so @dataclass can resolve sys.modules[cls.__module__]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_tasks_dir_has_at_least_30():
    assert TASKS.is_dir(), f"missing {TASKS}"
    ids = [p.name for p in TASKS.iterdir() if p.is_dir() and (p / "task.json").is_file()]
    assert len(ids) >= 30, f"want ≥30 tasks, got {len(ids)}: {ids}"


def test_each_task_has_scaffold_and_hidden_tests():
    for child in sorted(TASKS.iterdir()):
        if not child.is_dir() or not (child / "task.json").is_file():
            continue
        assert (child / "scaffold").is_dir(), child
        assert (child / "hidden_tests").is_dir(), child
        assert list((child / "scaffold").rglob("*.py")), f"no py scaffold in {child}"
        assert list((child / "hidden_tests").rglob("test_*.py")), f"no hidden tests in {child}"
        meta = json.loads((child / "task.json").read_text(encoding="utf-8"))
        assert meta.get("goal")
        assert meta.get("gold_fix")


def test_adversarial_and_redirect_counts():
    adv = redir = samples = inspired = samples_hard = realrepo = 0
    for child in TASKS.iterdir():
        if not (child / "task.json").is_file():
            continue
        meta = json.loads((child / "task.json").read_text(encoding="utf-8"))
        if meta.get("adversarial"):
            adv += 1
        if meta.get("supports_redirect"):
            redir += 1
        if int(meta.get("samples") or 0) >= 2:
            samples += 1
            if meta.get("hard"):
                samples_hard += 1
        if meta.get("inspired_by"):
            inspired += 1
        tags = meta.get("tags") or []
        if meta.get("realrepo") or "realrepo" in tags:
            realrepo += 1
    assert adv >= 8, adv
    assert redir >= 3, redir
    assert samples >= 1, samples
    assert samples_hard >= 3, samples_hard
    assert inspired >= 5, inspired
    assert realrepo >= 3, realrepo


def test_load_tasks_smoke_and_full():
    arena = _arena_mod()

    root = arena.find_tasks_root()
    smoke = arena.load_tasks(root, smoke=True)
    assert 1 <= len(smoke) <= 3
    full = arena.load_tasks(root, smoke=False)
    assert len(full) >= 30

    for t in smoke:
        prompt = arena.build_agent_prompt(t)
        # Hidden test bodies must not appear in agent prompt
        hidden = (t.hidden_tests_dir / "test_hidden.py").read_text(encoding="utf-8")
        # Sample a distinctive assert line if present
        for line in hidden.splitlines():
            line = line.strip()
            if line.startswith("assert ") and len(line) > 20:
                assert line not in prompt, f"hidden assert leaked into prompt: {line}"
        assert "hidden_tests" not in prompt or "not available" in prompt.lower()


def test_hidden_tests_not_in_scaffold():
    for child in TASKS.iterdir():
        if not (child / "task.json").is_file():
            continue
        for p in (child / "scaffold").rglob("*"):
            assert "hidden_tests" not in p.parts
            if p.is_file() and p.suffix == ".py":
                # Agent-visible stub tests under scaffold/tests/ are allowed (TDD tasks).
                # Hidden suite must never live in scaffold.
                if "tests" in p.parts and p.name.startswith("test_"):
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
                assert "def test_" not in text or "test_" not in p.name


def test_materialize_excludes_hidden():
    arena = _arena_mod()

    tasks = arena.load_tasks(arena.find_tasks_root(), smoke=True)
    t = tasks[0]
    with tempfile.TemporaryDirectory(prefix="arena-mat-") as td:
        work = arena.materialize_workdir(t, Path(td))
        assert work.is_dir()
        names = [p.name for p in work.rglob("*")]
        assert "test_hidden.py" not in names
        assert not any("hidden_tests" in str(p) for p in work.rglob("*"))


def test_arena_smoke_subprocess():
    env = os.environ.copy()
    env["WW_ARENA_LLM"] = "0"
    env["WW_CODING_REQUIRE_TEST"] = "1"
    env["WW_CODING_MODEL"] = "arena-mock-model"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "coding_arena.py"), "--smoke", "--vs-baseline"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, out[-3000:]
    assert "WW pass rate" in out or "pass rate" in out.lower()
    latest = ROOT / "results" / "coding_arena" / "latest.json"
    assert latest.is_file(), "expected results/coding_arena/latest.json"
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data.get("summary", {}).get("n_tasks", 0) >= 1
    assert data.get("summary", {}).get("ww_public_reply_dump_count", 1) == 0
    # B9 model id present on WW rows
    for row in data.get("ww_results") or []:
        assert row.get("model_id"), row
        assert row.get("require_test") is True


def test_pm_version_0_13_endpoint():
    from coding import PM_VERSION, get_status

    assert PM_VERSION == "0.13.0-endpoint"
    assert get_status()["version"] == "0.13.0-endpoint"
    assert "index_facade" in get_status()["modules"]


def test_sb1_baseline_kind_and_summary_fields():
    arena = _arena_mod()
    assert arena.baseline_kind() == "strong_react"
    # summarize exposes F1 machine-readable fields
    from scripts import coding_arena as _  # noqa: F401 — may fail if not package
    ww = [
        arena.AgentRunResult(
            agent="ww", task_id="t1", pass_at_1=True, wall_time_s=1.0,
            require_test=True, gold_applied=False, mode="mock",
        ),
        arena.AgentRunResult(
            agent="ww", task_id="t2", pass_at_1=False, wall_time_s=1.0,
            require_test=True, gold_applied=False, mode="mock",
            failure_taxonomy="thrash",
        ),
    ]
    base = [
        arena.AgentRunResult(
            agent="baseline", task_id="t1", pass_at_1=False, wall_time_s=0.5,
            metrics={"baseline_kind": "strong_react"},
        ),
        arena.AgentRunResult(
            agent="baseline", task_id="t2", pass_at_1=False, wall_time_s=0.5,
            metrics={"baseline_kind": "strong_react"},
        ),
    ]
    s = arena.summarize(ww, base)
    assert s["baseline_kind"] == "strong_react"
    assert "ww_pass_rate" in s
    assert "baseline_pass_rate" in s
    assert "delta_pass_rate" in s
    assert "thrash_rate" in s
    assert "gold_applied_any" in s
    assert "f1_pass_ok" in s
    assert "f1_delta_ok" in s
    assert s["thrash_rate"] == 0.5


def test_llm_path_never_applies_gold():
    """Contract: WW_ARENA_LLM=1 must not call _apply_gold_fix (closed-book)."""
    arena = _arena_mod()
    gold_calls: list = []
    original_gold = arena._apply_gold_fix

    def _spy_gold(workdir, fix):
        gold_calls.append({"workdir": str(workdir), "fix_keys": list((fix or {}).keys())})
        return original_gold(workdir, fix)

    arena._apply_gold_fix = _spy_gold  # type: ignore

    def _hook(task, workdir, prompt, timeout_s):
        # Simulated closed-book agent: no gold, optional no-op edit
        assert "gold_fix" not in prompt
        # Ensure gold content not required
        return {
            "success": False,
            "gold_applied": False,
            "mode": "llm",
            "model_id": "contract-mock-llm",
            "model_route": {"model": "contract-mock-llm", "source": "test"},
            "metrics": {
                "rounds": 2,
                "tools": 2,
                "graph_calls": 2,
                "grep_calls": 1,
                "trips": 0,
                "replans": 0,
                "redirects": 0,
                "autocompacts": 0,
                "microcompacts": 0,
                "samples": 0,
                "max_same_fp": 0,
                "model_id": "contract-mock-llm",
                "extra": {"gold_applied": False},
            },
            "user_summary": "Closed-book contract hook finished without gold.",
            "state": {
                "located": True,
                "edited": False,
                "verify_ok": False,
                "timed_out": False,
                "thrash": False,
                "model_error": "",
                "rounds": 2,
                "files_touched": [],
            },
        }

    arena.set_closed_book_hook(_hook)
    try:
        tasks = arena.load_tasks(arena.find_tasks_root(), smoke=True)
        t = tasks[0]
        # Poison gold would make mock pass — LLM path must not use it
        assert t.gold_fix, "fixture should have gold for contrast"
        with tempfile.TemporaryDirectory(prefix="arena-llm-contract-") as td:
            old = os.environ.get("WW_ARENA_LLM")
            os.environ["WW_ARENA_LLM"] = "1"
            try:
                r = arena.run_ww_llm_agent(t, Path(td))
            finally:
                if old is None:
                    os.environ.pop("WW_ARENA_LLM", None)
                else:
                    os.environ["WW_ARENA_LLM"] = old
        assert gold_calls == [], f"gold must never be applied on LLM path: {gold_calls}"
        assert r.gold_applied is False
        assert r.mode == "llm"
        assert r.extra.get("gold_applied") is False
        # Must not silently label mock-as-llm with gold
        assert r.metrics.get("gold_applied") is not True
    finally:
        arena.set_closed_book_hook(None)
        arena._apply_gold_fix = original_gold  # type: ignore


def test_assistant_message_includes_reasoning_content():
    """DeepSeek thinking mode: closed-book loop must echo reasoning_content."""
    from core.transports.base import NormalizedResponse

    arena = _arena_mod()
    resp = NormalizedResponse(
        content="",
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "coding_grep",
                "arguments": {"pattern": "bug", "path": "src"},
            },
        }],
        reasoning_content="Search the scaffold for the failing symbol first.",
        finish_reason="tool_calls",
    )
    rc = arena._extract_reasoning_content(resp)
    assert rc == "Search the scaffold for the failing symbol first."

    # Simulate the closed-book builder path used after chat_with_tools
    name, args, tc_id = arena._normalize_tool_call(resp.tool_calls[0])
    openai_tcs = [{
        "id": tc_id or "call_1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": arena._tool_args_as_json_string(args),
        },
    }]
    msg = arena._build_assistant_message(
        content=resp.content,
        tool_calls_openai=openai_tcs,
        reasoning_content=rc,
    )
    assert msg["role"] == "assistant"
    assert msg["reasoning_content"] == rc
    assert msg["content"] is None  # tool-only turn
    assert len(msg["tool_calls"]) == 1
    # OpenAI protocol: arguments must be a JSON *string*, not a dict
    raw_args = msg["tool_calls"][0]["function"]["arguments"]
    assert isinstance(raw_args, str)
    parsed = json.loads(raw_args)
    assert parsed["pattern"] == "bug"


def test_tool_args_as_json_string_never_sends_dict():
    arena = _arena_mod()
    s = arena._tool_args_as_json_string({"a": 1})
    assert isinstance(s, str)
    assert json.loads(s) == {"a": 1}
    assert arena._tool_args_as_json_string('{"x": true}') == '{"x": true}'
    assert arena._tool_args_as_json_string(None) == "{}"
    assert arena._tool_args_as_json_string("") == "{}"


def test_build_assistant_message_omits_empty_reasoning():
    arena = _arena_mod()
    msg = arena._build_assistant_message(content="hello", reasoning_content="")
    assert "reasoning_content" not in msg
    assert msg["content"] == "hello"


def test_llm_path_honest_fail_without_api():
    """Without API key and without hook: mode=llm, gold_applied=false, not mock."""
    arena = _arena_mod()
    arena.set_closed_book_hook(None)
    # Clear keys for this process check
    cleared = {}
    for k in arena._API_KEY_ENVS:
        if k in os.environ:
            cleared[k] = os.environ.pop(k)
    # Also clear force flags
    force = os.environ.pop("WW_ARENA_LLM_FORCE", None)
    try:
        tasks = arena.load_tasks(arena.find_tasks_root(), smoke=True)
        t = tasks[0]
        with tempfile.TemporaryDirectory(prefix="arena-llm-noapi-") as td:
            os.environ["WW_ARENA_LLM"] = "1"
            r = arena.run_ww_llm_agent(t, Path(td))
        assert r.mode == "llm"
        assert r.gold_applied is False
        assert r.pass_at_1 is False
        assert r.failure_taxonomy == "model"
        assert "unavailable" in (r.error or "").lower() or "api" in (r.error or "").lower()
    finally:
        os.environ.pop("WW_ARENA_LLM", None)
        for k, v in cleared.items():
            os.environ[k] = v
        if force is not None:
            os.environ["WW_ARENA_LLM_FORCE"] = force


def test_index_facade_build_query():
    from coding.index_facade import IndexFacade

    root = ROOT / "coding"
    fac = IndexFacade(project_root=str(root))
    b = fac.build(force=False)
    assert b.get("success") is True or (b.get("graph") or {}).get("success")
    mq = fac.query("map", token_budget=800)
    assert mq.get("success")
    gq = fac.query("grep", pattern="IndexFacade", path=str(root), glob="*.py")
    assert gq.get("success")
    assert (gq.get("result") or {}).get("count", 0) >= 1
    graph = fac.query("graph", action="stats")
    assert graph.get("success")
    ctr = fac.metrics()
    assert ctr.get("graph_calls", 0) >= 1
    assert ctr.get("map_calls", 0) >= 1
    assert ctr.get("grep_calls", 0) >= 1
    fac.close()


def test_orchestrator_graph_calls_via_facade():
    """Default locate path must record graph_calls > 0."""
    import tempfile as tf

    from coding.orchestrator import coding_run_ticket, reset_metrics, get_metrics

    with tf.TemporaryDirectory(prefix="orch-facade-") as td:
        p = Path(td)
        (p / "pkg").mkdir()
        (p / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (p / "pkg" / "m.py").write_text(
            "def add(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        (p / "tests").mkdir()
        (p / "tests" / "test_m.py").write_text(
            "from pkg.m import add\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        reset_metrics()
        ticket = coding_run_ticket(
            goal="Fix `pkg/m.py::add` so add(a,b)==a+b",
            project_root=str(p),
            # no new_body — locate only; still must use graph
        )
        m = ticket.get("metrics") or get_metrics().to_dict()
        assert int(m.get("graph_calls") or 0) > 0, m
        locate = (ticket.get("steps") or {}).get("locate") or {}
        assert "graph_build" in locate or "graph" in locate
