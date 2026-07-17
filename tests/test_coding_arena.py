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


def test_tasks_dir_has_at_least_20():
    assert TASKS.is_dir(), f"missing {TASKS}"
    ids = [p.name for p in TASKS.iterdir() if p.is_dir() and (p / "task.json").is_file()]
    assert len(ids) >= 20, f"want ≥20 tasks, got {len(ids)}: {ids}"


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
    adv = redir = samples = inspired = 0
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
        if meta.get("inspired_by"):
            inspired += 1
    assert adv >= 5, adv
    assert redir >= 3, redir
    assert samples >= 1, samples
    assert inspired >= 5, inspired


def test_load_tasks_smoke_and_full():
    arena = _arena_mod()

    root = arena.find_tasks_root()
    smoke = arena.load_tasks(root, smoke=True)
    assert 1 <= len(smoke) <= 3
    full = arena.load_tasks(root, smoke=False)
    assert len(full) >= 20

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


def test_pm_version_0_11():
    from coding import PM_VERSION, get_status

    assert PM_VERSION == "0.11.0"
    assert get_status()["version"] == "0.11.0"
