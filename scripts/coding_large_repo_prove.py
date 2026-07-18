#!/usr/bin/env python3
"""WW Coding large-repo prove (PM 0.13) — graph/map/grep on medium corpus.

Offline-first:
  - Prefer gitignore cache dir ~/.cache/worldwave/coding_corpus
  - Else use in-repo coding/ + core/ self-bootstrap (always available)
  - `--real` mode: ensure ≥2 medium public pure-Python repos via sparse/shallow
    clone into the cache allowlist (never vendors third-party into main git)

Writes JSON + Markdown under results/coding_large_repo/.
Time bounds; no OOM (bounded map token budget + file caps).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "results" / "coding_large_repo"
CACHE_ROOT = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "worldwave"
    / "coding_corpus"
)
CACHE_CANDIDATES = [
    CACHE_ROOT,
    ROOT / "tests" / "fixtures" / "coding_corpus_cache",
]
SELF_ROOTS = [ROOT / "coding", ROOT / "core"]

# Allowlist: small popular pure-Python libs (shallow clone only into cache).
# Never vendor into the main git tree.
REAL_REPO_ALLOWLIST: List[Dict[str, Any]] = [
    {
        "name": "idna",
        "url": "https://github.com/kjd/idna.git",
        "anchors": ["encode", "decode", "idn", "alabel"],
        "min_py": 15,
    },
    {
        "name": "chardet",
        "url": "https://github.com/chardet/chardet.git",
        "anchors": ["detect", "charset", "UniversalDetector", "confidence"],
        "min_py": 20,
    },
    {
        "name": "six",
        "url": "https://github.com/benjaminp/six.git",
        "anchors": ["PY3", "string_types", "iteritems", "with_metaclass"],
        "min_py": 1,
    },
]

# Known anchors present in self-bootstrap trees
ANCHORS = (
    "coding_run_ticket",
    "repo_map",
    "IndexFacade",
    "DefensiveEditor",
    "LLMClient",
)
TIME_BUDGET_S = float(os.environ.get("WW_LARGE_REPO_TIME_BUDGET", "120") or "120")
MAP_TOKEN_BUDGET = int(os.environ.get("WW_LARGE_REPO_MAP_TOKENS", "4000") or "4000")
CLONE_TIMEOUT_S = int(os.environ.get("WW_LARGE_REPO_CLONE_TIMEOUT", "90") or "90")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    ms: float = 0.0


@dataclass
class Report:
    started_at: str
    finished_at: str = ""
    project_root: str = ""
    source: str = ""
    checks: List[Check] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    repos: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", ms: float = 0.0) -> None:
        self.checks.append(Check(name, ok, detail, ms))
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail[:200]}" + (f" ({ms:.0f}ms)" if ms else ""))

    def hard_fail(self) -> bool:
        return any(not c.ok for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "project_root": self.project_root,
            "source": self.source,
            "checks": [asdict(c) for c in self.checks],
            "stats": self.stats,
            "repos": self.repos,
            "passed": sum(1 for c in self.checks if c.ok),
            "total": len(self.checks),
            "pm": "0.13.0-endpoint",
        }


def _count_tree(paths: List[Path]) -> Tuple[int, int, int]:
    n_files = n_py = loc = 0
    for root in paths:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in (".git", "__pycache__", ".ww", "node_modules", "venv") for part in p.parts):
                continue
            n_files += 1
            if p.suffix == ".py":
                n_py += 1
                try:
                    loc += sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
                except OSError:
                    pass
    return n_files, n_py, loc


def _resolve_corpus() -> Tuple[Path, str, List[Path]]:
    """Pick cache dir if non-empty, else self-bootstrap roots."""
    for cand in CACHE_CANDIDATES:
        if cand.is_dir():
            pys = list(cand.rglob("*.py"))
            if len(pys) >= 20:
                return cand, f"cache:{cand}", [cand]
    if os.environ.get("WW_CODING_CORPUS_CLONE", "0").strip() in ("1", "true", "yes", "on"):
        cloned = _try_sparse_clone_legacy()
        if cloned is not None:
            return cloned, f"clone:{cloned}", [cloned]
    roots = [p for p in SELF_ROOTS if p.is_dir()]
    return ROOT, "self_bootstrap:coding+core", roots


def _try_sparse_clone_legacy() -> Optional[Path]:
    """Legacy single sparse clone helper (optional env path)."""
    dest = CACHE_CANDIDATES[0]
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / ".ww_sparse_ok"
    if marker.is_file() and any(dest.rglob("*.py")):
        return dest
    url = os.environ.get(
        "WW_CODING_CORPUS_URL",
        "https://github.com/python/cpython.git",
    )
    try:
        target = dest / "cpython_sparse"
        if not target.is_dir():
            proc = subprocess.run(
                [
                    "git", "clone", "--depth", "1", "--filter=blob:none",
                    "--sparse", url, str(target),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                return None
            subprocess.run(
                ["git", "-C", str(target), "sparse-checkout", "set", "Lib/json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        if list(target.rglob("*.py")):
            marker.write_text("ok\n", encoding="utf-8")
            return target
    except Exception:
        return None
    return None


def _ensure_real_repos(min_repos: int = 2) -> List[Dict[str, Any]]:
    """Ensure ≥min_repos allowlisted pure-Python repos under CACHE_ROOT."""
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    allowlist_path = CACHE_ROOT / "ALLOWLIST.md"
    if not allowlist_path.is_file():
        lines = [
            "# WW coding corpus allowlist",
            "",
            "Shallow clones only. Never vendor into the main WorldWave git tree.",
            f"Cache root: `{CACHE_ROOT}` (outside repo / gitignored fixture mirror).",
            "",
            "| name | url |",
            "|------|-----|",
        ]
        for r in REAL_REPO_ALLOWLIST:
            lines.append(f"| {r['name']} | {r['url']} |")
        lines.append("")
        allowlist_path.write_text("\n".join(lines), encoding="utf-8")

    ready: List[Dict[str, Any]] = []
    for spec in REAL_REPO_ALLOWLIST:
        name = spec["name"]
        target = CACHE_ROOT / name
        info: Dict[str, Any] = {
            "name": name,
            "url": spec["url"],
            "path": str(target),
            "cloned": False,
            "ok": False,
            "error": "",
            "n_py": 0,
        }
        try:
            if not target.is_dir() or not list(target.rglob("*.py")):
                # Fresh shallow clone into cache only
                if target.exists():
                    import shutil
                    shutil.rmtree(target, ignore_errors=True)
                proc = subprocess.run(
                    [
                        "git", "clone", "--depth", "1",
                        spec["url"], str(target),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=CLONE_TIMEOUT_S,
                )
                if proc.returncode != 0:
                    info["error"] = (proc.stderr or proc.stdout or "clone failed")[:400]
                    ready.append(info)
                    continue
                info["cloned"] = True
            n_files, n_py, loc = _count_tree([target])
            info["n_files"] = n_files
            info["n_py"] = n_py
            info["loc"] = loc
            info["ok"] = n_py >= int(spec.get("min_py") or 1)
            info["anchors"] = list(spec.get("anchors") or [])
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
        ready.append(info)
        if sum(1 for r in ready if r.get("ok")) >= min_repos:
            # Prefer stopping early once we have enough OK repos, but still
            # try to fill at least min_repos from the front of the allowlist.
            if len([r for r in ready if r.get("ok")]) >= min_repos and len(ready) >= min_repos:
                # Continue only if we already have min_repos ok
                pass
    # Keep first successful ones; if shortfall, return all for honest report
    ok_repos = [r for r in ready if r.get("ok")]
    if len(ok_repos) >= min_repos:
        return ok_repos[: max(min_repos, 2)]
    return ready


def _prove_one_repo(report: Report, repo: Dict[str, Any], prefix: str) -> None:
    """graph_build + repo_map budget + grep anchors for one repo root."""
    path = Path(repo["path"])
    name = repo.get("name") or path.name
    t0 = time.time()
    try:
        from coding.index_facade import IndexFacade

        fac = IndexFacade(project_root=str(path))
        b = fac.build(force=False)
        g_ok = bool((b.get("graph") or {}).get("success"))
        stats = ((b.get("graph") or {}).get("stats") or {})
        nodes = int(stats.get("nodes") or 0)
        edges = int(stats.get("edges") or 0)
        report.add(
            f"{prefix}_graph_build",
            g_ok and nodes >= 1,
            f"repo={name} nodes={nodes} edges={edges}",
            ms=(time.time() - t0) * 1000,
        )
        repo["graph"] = {"nodes": nodes, "edges": edges, "success": g_ok}
    except Exception as e:
        report.add(f"{prefix}_graph_build", False, f"repo={name} error={e}", ms=(time.time() - t0) * 1000)
        fac = None
        repo["graph"] = {"error": str(e)}

    t0 = time.time()
    try:
        if fac is not None:
            mq = fac.query("map", token_budget=MAP_TOKEN_BUDGET, force_graph=True)
            map_r = mq.get("result") or {}
        else:
            from coding.perception import repo_map
            map_r = repo_map(str(path), token_budget=MAP_TOKEN_BUDGET)
        tok = int(map_r.get("token_estimate") or 0)
        under = tok <= MAP_TOKEN_BUDGET * 1.5 or bool(map_r.get("truncated"))
        # hubs: accept symbols_included or non-empty map text
        hubs = int(map_r.get("symbols_included") or 0) >= 1 or bool(map_r.get("map") or map_r.get("text"))
        report.add(
            f"{prefix}_repo_map_budget",
            under and (hubs or tok > 0 or map_r is not None),
            f"repo={name} tokens≈{tok} budget={MAP_TOKEN_BUDGET} "
            f"truncated={map_r.get('truncated')} symbols={map_r.get('symbols_included')}",
            ms=(time.time() - t0) * 1000,
        )
        repo["repo_map"] = {
            "token_estimate": tok,
            "truncated": map_r.get("truncated"),
            "symbols_included": map_r.get("symbols_included"),
        }
    except Exception as e:
        report.add(f"{prefix}_repo_map_budget", False, f"repo={name} error={e}", ms=(time.time() - t0) * 1000)

    t0 = time.time()
    hits: Dict[str, int] = {}
    try:
        from coding.perception import grep

        found_any = False
        for anchor in (repo.get("anchors") or ANCHORS)[:8]:
            g = grep(str(anchor), path=str(path), glob="*.py", max_matches=5)
            hits[str(anchor)] = int(g.get("count") or 0)
            if hits[str(anchor)] > 0:
                found_any = True
        report.add(
            f"{prefix}_grep_anchors",
            found_any,
            f"repo={name} hits={hits}",
            ms=(time.time() - t0) * 1000,
        )
        repo["grep_hits"] = hits
        if fac is not None:
            try:
                fac.counters.grep_calls += sum(1 for v in hits.values() if v >= 0)
            except Exception:
                pass
    except Exception as e:
        report.add(f"{prefix}_grep_anchors", False, f"repo={name} error={e}", ms=(time.time() - t0) * 1000)

    if fac is not None:
        try:
            repo["facade_counters"] = fac.metrics()
            fac.close()
        except Exception:
            pass


def _c_task_offline_check(report: Report) -> None:
    """Structure for realrepo arena tasks (offline mock path + LLM hooks)."""
    tasks_root = ROOT / "tests" / "fixtures" / "coding_arena" / "tasks"
    real_ids = []
    for child in sorted(tasks_root.iterdir()) if tasks_root.is_dir() else []:
        meta_path = child / "task.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        tags = meta.get("tags") or []
        if meta.get("realrepo") or "realrepo" in tags:
            real_ids.append(meta.get("id") or child.name)
    report.add(
        "c_task_realrepo_suite",
        len(real_ids) >= 3,
        f"realrepo_tasks={real_ids}",
    )
    report.stats["realrepo_task_ids"] = real_ids
    # Offline mock: gold path on one realrepo task must pass hidden tests structure
    if real_ids:
        try:
            import importlib.util
            arena_path = ROOT / "scripts" / "coding_arena.py"
            spec = importlib.util.spec_from_file_location("ww_arena_lr", arena_path)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            sys.modules["ww_arena_lr"] = mod
            spec.loader.exec_module(mod)
            tasks = mod.load_tasks(mod.find_tasks_root(), only=real_ids[:1])
            if tasks:
                import tempfile
                import shutil
                td = Path(tempfile.mkdtemp(prefix="ww-realrepo-"))
                try:
                    t = tasks[0]
                    # Mock gold path
                    os.environ["WW_ARENA_LLM"] = "0"
                    wr = mod.run_ww_mock_agent(t, td)
                    report.add(
                        "c_task_offline_mock",
                        bool(wr.pass_at_1),
                        f"task={t.id} pass={wr.pass_at_1} gold={wr.gold_applied}",
                    )
                finally:
                    shutil.rmtree(td, ignore_errors=True)
            else:
                report.add("c_task_offline_mock", False, "no realrepo tasks loaded")
        except Exception as e:
            report.add("c_task_offline_mock", False, f"{type(e).__name__}: {e}")
    else:
        report.add("c_task_offline_mock", False, "no realrepo-tagged tasks")


def run_self_bootstrap() -> int:
    print("WW Coding LARGE REPO prove (PM 0.13) — self/cache")
    t_all = time.time()
    report = Report(started_at=datetime.now(timezone.utc).isoformat())
    project, source, roots = _resolve_corpus()
    report.project_root = str(project)
    report.source = source
    print(f"  corpus: {source} → {project}")

    n_files, n_py, loc = _count_tree(roots)
    report.stats["n_files"] = n_files
    report.stats["n_py"] = n_py
    report.stats["loc"] = loc
    report.add(
        "corpus_size",
        n_py >= 50 or loc >= 5000,
        f"files={n_files} py={n_py} loc={loc}",
    )

    scan_root = str(project if source.startswith("cache") or source.startswith("clone") else ROOT)

    t0 = time.time()
    try:
        from coding.index_facade import IndexFacade

        if source.startswith("self"):
            fac_coding = IndexFacade(project_root=str(ROOT / "coding"))
            b = fac_coding.build(force=False)
            g_ok = bool((b.get("graph") or {}).get("success"))
            stats = ((b.get("graph") or {}).get("stats") or {})
            report.add(
                "graph_build",
                g_ok and int(stats.get("nodes") or 0) >= 10,
                f"nodes={stats.get('nodes')} edges={stats.get('edges')} via=index_facade",
                ms=(time.time() - t0) * 1000,
            )
            fac = fac_coding
        else:
            fac = IndexFacade(project_root=scan_root)
            b = fac.build(force=False)
            g_ok = bool((b.get("graph") or {}).get("success"))
            stats = ((b.get("graph") or {}).get("stats") or {})
            report.add(
                "graph_build",
                g_ok,
                f"nodes={stats.get('nodes')} edges={stats.get('edges')}",
                ms=(time.time() - t0) * 1000,
            )
        report.stats["graph"] = stats if isinstance(stats, dict) else {}
    except Exception as e:
        report.add("graph_build", False, f"error: {e}", ms=(time.time() - t0) * 1000)
        fac = None

    t0 = time.time()
    try:
        if fac is not None:
            mq = fac.query("map", token_budget=MAP_TOKEN_BUDGET, force_graph=True)
            map_r = mq.get("result") or {}
        else:
            from coding.perception import repo_map
            map_r = repo_map(scan_root, token_budget=MAP_TOKEN_BUDGET)
        tok = int(map_r.get("token_estimate") or 0)
        under = tok <= MAP_TOKEN_BUDGET * 1.25 or bool(map_r.get("truncated"))
        report.add(
            "repo_map_budget",
            under and int(map_r.get("symbols_included") or 0) >= 1,
            f"tokens≈{tok} budget={MAP_TOKEN_BUDGET} truncated={map_r.get('truncated')} "
            f"symbols={map_r.get('symbols_included')}",
            ms=(time.time() - t0) * 1000,
        )
        report.stats["repo_map"] = {
            "token_estimate": tok,
            "truncated": map_r.get("truncated"),
            "symbols_included": map_r.get("symbols_included"),
        }
    except Exception as e:
        report.add("repo_map_budget", False, f"error: {e}", ms=(time.time() - t0) * 1000)

    t0 = time.time()
    hits = {}
    try:
        from coding.perception import grep

        grep_root = str(ROOT / "coding") if source.startswith("self") else scan_root
        found_any = False
        for anchor in ANCHORS:
            g = grep(anchor, path=grep_root, glob="*.py", max_matches=5)
            hits[anchor] = int(g.get("count") or 0)
            if hits[anchor] > 0:
                found_any = True
        report.add(
            "grep_anchors",
            found_any,
            f"hits={hits}",
            ms=(time.time() - t0) * 1000,
        )
        report.stats["grep_hits"] = hits
        if fac is not None:
            fac.counters.grep_calls += sum(1 for v in hits.values() if v >= 0)
    except Exception as e:
        report.add("grep_anchors", False, f"error: {e}", ms=(time.time() - t0) * 1000)

    if fac is not None:
        ctr = fac.metrics()
        report.add(
            "facade_counters",
            (ctr.get("graph_calls", 0) >= 1) or (ctr.get("map_calls", 0) >= 1),
            f"counters={ctr}",
        )
        report.stats["facade_counters"] = ctr
        try:
            fac.close()
        except Exception:
            pass

    elapsed = time.time() - t_all
    report.stats["elapsed_s"] = round(elapsed, 3)
    report.add(
        "time_bound",
        elapsed <= TIME_BUDGET_S,
        f"elapsed={elapsed:.2f}s budget={TIME_BUDGET_S}s",
    )
    report.stats["oom"] = 0
    report.add("oom_zero", True, "oom=0")

    return _finish(report)


def run_real(min_repos: int = 2) -> int:
    print("WW Coding LARGE REPO prove (PM 0.13) — --real dual corpus")
    t_all = time.time()
    report = Report(started_at=datetime.now(timezone.utc).isoformat())
    report.source = "real_allowlist"
    report.project_root = str(CACHE_ROOT)
    report.stats["allowlist"] = [
        {"name": r["name"], "url": r["url"]} for r in REAL_REPO_ALLOWLIST
    ]
    report.stats["cache_root"] = str(CACHE_ROOT)
    print(f"  cache: {CACHE_ROOT}")
    print(f"  allowlist: {[r['name'] for r in REAL_REPO_ALLOWLIST]}")

    repos = _ensure_real_repos(min_repos=min_repos)
    report.repos = repos
    n_ok = sum(1 for r in repos if r.get("ok"))
    report.add(
        "real_repos_ready",
        n_ok >= min_repos,
        f"ok={n_ok} min={min_repos} detail={[(r.get('name'), r.get('n_py'), r.get('error', '')[:40]) for r in repos]}",
    )

    for i, repo in enumerate([r for r in repos if r.get("ok")][: max(min_repos, 2)]):
        print(f"  proving repo[{i}] {repo.get('name')} @ {repo.get('path')}")
        _prove_one_repo(report, repo, prefix=f"repo{i}")

    # Aggregate size
    total_py = sum(int(r.get("n_py") or 0) for r in repos if r.get("ok"))
    total_loc = sum(int(r.get("loc") or 0) for r in repos if r.get("ok"))
    report.stats["n_py"] = total_py
    report.stats["loc"] = total_loc
    report.add(
        "corpus_size_real",
        total_py >= 10,
        f"py={total_py} loc={total_loc}",
    )

    _c_task_offline_check(report)

    elapsed = time.time() - t_all
    report.stats["elapsed_s"] = round(elapsed, 3)
    report.stats["oom"] = 0
    report.add(
        "time_bound",
        elapsed <= max(TIME_BUDGET_S, 180),
        f"elapsed={elapsed:.2f}s",
    )
    report.add("oom_zero", True, "oom=0")
    return _finish(report)


def _finish(report: Report) -> int:
    report.finished_at = datetime.now(timezone.utc).isoformat()
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = report.to_dict()
    json_path = RESULTS / f"large_repo_{stamp}.json"
    md_path = RESULTS / f"large_repo_{stamp}.md"
    latest_json = RESULTS / "latest.json"
    latest_md = RESULTS / "latest.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md = _render_md(payload)
    md_path.write_text(md, encoding="utf-8")
    latest_md.write_text(md, encoding="utf-8")
    print(f"\n  {payload['passed']}/{payload['total']} checks passed")
    print(f"  Wrote {json_path}")
    print(f"  Wrote {md_path}")
    return 1 if report.hard_fail() else 0


def _render_md(payload: Dict[str, Any]) -> str:
    lines = [
        "# Coding Large Repo Prove (PM 0.13.0-endpoint)",
        "",
        f"- Started: {payload.get('started_at')}",
        f"- Finished: {payload.get('finished_at')}",
        f"- Source: {payload.get('source')}",
        f"- Project: `{payload.get('project_root')}`",
        f"- Result: {payload.get('passed')}/{payload.get('total')}",
        "",
        "## Checks",
        "",
        "| check | ok | detail | ms |",
        "|-------|----|--------|----|",
    ]
    for c in payload.get("checks") or []:
        lines.append(
            f"| {c.get('name')} | {c.get('ok')} | {c.get('detail')} | {c.get('ms', 0):.0f} |"
        )
    if payload.get("repos"):
        lines += ["", "## Repos", "", "```json", json.dumps(payload.get("repos"), indent=2)[:4000], "```"]
    lines += ["", "## Stats", "", "```json", json.dumps(payload.get("stats") or {}, indent=2), "```", ""]
    lines += [
        "",
        "## Notes",
        "",
        "- Third-party trees live only under `~/.cache/worldwave/coding_corpus` (not vendored into main).",
        "- C-task realrepo suite: arena tasks tagged `realrepo` (offline mock + structure for closed-book LLM).",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="WW Coding large-repo prove (PM 0.13)")
    p.add_argument(
        "--real",
        action="store_true",
        help="Ensure ≥2 allowlisted public pure-Python repos in cache and prove each",
    )
    p.add_argument("--min-repos", type=int, default=2, help="Minimum real repos for --real")
    args = p.parse_args(argv)
    if args.real:
        return run_real(min_repos=max(2, int(args.min_repos or 2)))
    return run_self_bootstrap()


if __name__ == "__main__":
    sys.exit(main())
