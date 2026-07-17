#!/usr/bin/env python3
"""Generate a scale fixture of N Python modules for coding-engine gates.

Usage:
  python scripts/coding_scale_fixture.py --out tests/fixtures/coding_scale --count 200

Gates (also exercised by coding_prove --e2e scale section / --scale):
  - graph_build completes
  - repo_map truncates under token budget
  - grep finds a known symbol
  - wall time bound configurable (WW_CODING_SCALE_TIMEOUT, default 120s)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "tests" / "fixtures" / "coding_scale"
KNOWN_SYMBOL = "scale_known_anchor"


def generate(out_dir: Path, count: int = 200, seed_modules: int = None) -> dict:
    """Write ≥count .py files under out_dir/pkg/ with a known anchor symbol."""
    if count < 1:
        raise ValueError("count must be >= 1")
    seed_modules = seed_modules or count
    pkg = out_dir / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('"""Scale fixture package."""\n', encoding="utf-8")

    # Anchor module with known symbol (grep target)
    anchor = pkg / "anchor.py"
    anchor.write_text(
        f'''"""Known-symbol anchor for scale grep/graph gates."""


def {KNOWN_SYMBOL}(x: int) -> int:
    """Stable symbol for coding_grep / who_calls."""
    return x + 1


def helper_mid(x: int) -> int:
    return {KNOWN_SYMBOL}(x) * 2
''',
        encoding="utf-8",
    )

    written = [anchor]
    for i in range(count):
        # Spread across subpackages for realistic walk
        sub = pkg / f"mod_{i // 50:03d}"
        sub.mkdir(parents=True, exist_ok=True)
        init = sub / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
            written.append(init)
        f = sub / f"unit_{i:04d}.py"
        # Some units call the anchor so the graph is non-trivial
        if i % 17 == 0:
            body = f'''"""Scale unit {i}."""
from pkg.anchor import {KNOWN_SYMBOL}


def fn_{i}(x: int) -> int:
    return {KNOWN_SYMBOL}(x) + {i}


class C{i}:
    def run(self, x: int) -> int:
        return fn_{i}(x)
'''
        else:
            body = f'''"""Scale unit {i}."""


def fn_{i}(x: int) -> int:
    return x + {i}


class C{i}:
    def run(self, x: int) -> int:
        return fn_{i}(x)
'''
        f.write_text(body, encoding="utf-8")
        written.append(f)

    # Minimal tests dir so verify can be pointed here if needed
    tests = out_dir / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_anchor.py").write_text(
        f'''from pkg.anchor import {KNOWN_SYMBOL}


def test_anchor():
    assert {KNOWN_SYMBOL}(1) == 2
''',
        encoding="utf-8",
    )

    py_files = list(out_dir.rglob("*.py"))
    return {
        "out_dir": str(out_dir),
        "count_requested": count,
        "py_files": len(py_files),
        "known_symbol": KNOWN_SYMBOL,
        "anchor": str(anchor.relative_to(out_dir)),
    }


def run_gates(
    out_dir: Path,
    token_budget: int = 2000,
    timeout_s: float = None,
) -> dict:
    """graph_build + repo_map truncate + grep known symbol; time-bounded."""
    timeout_s = timeout_s or float(os.environ.get("WW_CODING_SCALE_TIMEOUT", "120") or "120")
    t0 = time.time()
    report = {"ok": True, "checks": [], "elapsed_s": 0.0}

    def add(name: str, ok: bool, detail: str = ""):
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["ok"] = False

    # graph_build
    try:
        from coding.code_graph import CodeGraphStore
        ww = out_dir / ".ww"
        if ww.exists():
            import shutil
            shutil.rmtree(ww, ignore_errors=True)
        store = CodeGraphStore(project_root=str(out_dir))
        store.build(str(out_dir), force=True)
        stats = store.stats()
        store.close()
        add(
            "graph_build",
            stats.get("nodes", 0) > 0 and stats.get("files", 0) >= 50,
            f"nodes={stats.get('nodes')} files={stats.get('files')} edges={stats.get('edges')}",
        )
    except Exception as e:
        add("graph_build", False, str(e))

    if time.time() - t0 > timeout_s:
        add("time_bound", False, f"exceeded {timeout_s}s after graph")
        report["elapsed_s"] = time.time() - t0
        return report

    # repo_map truncates
    try:
        from coding.perception import repo_map
        m = repo_map(str(out_dir), token_budget=token_budget)
        add(
            "repo_map_truncates",
            bool(m.get("truncated")) or m.get("symbols_included", 0) < m.get("symbols_total", 0),
            f"truncated={m.get('truncated')} included={m.get('symbols_included')}/{m.get('symbols_total')} tokens={m.get('token_estimate')}",
        )
    except Exception as e:
        add("repo_map_truncates", False, str(e))

    # grep known symbol
    try:
        from coding.perception import grep
        g = grep(KNOWN_SYMBOL, path=str(out_dir), glob="*.py", max_matches=20)
        add(
            "grep_known_symbol",
            g.get("count", 0) >= 1,
            f"count={g.get('count')} engine={g.get('engine')}",
        )
    except Exception as e:
        add("grep_known_symbol", False, str(e))

    elapsed = time.time() - t0
    report["elapsed_s"] = elapsed
    add("time_bound", elapsed <= timeout_s, f"elapsed={elapsed:.2f}s limit={timeout_s}s")
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="WW coding scale fixture generator")
    p.add_argument("--out", type=str, default=str(DEFAULT_OUT), help="Output directory")
    p.add_argument("--count", type=int, default=200, help="Number of unit modules (>=200)")
    p.add_argument("--gates", action="store_true", help="Run map/grep/graph gates after generate")
    p.add_argument("--token-budget", type=int, default=2000)
    p.add_argument("--timeout", type=float, default=None)
    args = p.parse_args(argv)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    meta = generate(out, count=max(args.count, 1))
    print(f"Generated {meta['py_files']} .py files under {meta['out_dir']}")
    print(f"  known_symbol={meta['known_symbol']}")

    if args.gates:
        print("Running scale gates…")
        r = run_gates(out, token_budget=args.token_budget, timeout_s=args.timeout)
        for c in r["checks"]:
            status = "PASS" if c["ok"] else "FAIL"
            print(f"  [{status}] {c['name']}: {c['detail'][:140]}")
        print(f"elapsed={r['elapsed_s']:.2f}s")
        return 0 if r["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
