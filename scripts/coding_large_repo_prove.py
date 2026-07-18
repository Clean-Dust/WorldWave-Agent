#!/usr/bin/env python3
"""WW Coding large-repo prove (PM 0.12) — graph/map/grep on medium corpus.

Offline-first:
  - Prefer gitignore cache dir ~/.cache/worldwave/coding_corpus
  - Else use in-repo coding/ + core/ self-bootstrap (always available)
  - Optional allowlist sparse clone when WW_CODING_CORPUS_CLONE=1
    (never vendors third-party into the main tree)

Writes JSON + Markdown under results/coding_large_repo/.
Time bounds; no OOM (bounded map token budget + file caps).
"""
from __future__ import annotations

import json
import os
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
CACHE_CANDIDATES = [
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "worldwave"
    / "coding_corpus",
    ROOT / "tests" / "fixtures" / "coding_corpus_cache",
]
SELF_ROOTS = [ROOT / "coding", ROOT / "core"]
# Known anchors present in self-bootstrap trees
ANCHORS = (
    "coding_run_ticket",
    "repo_map",
    "IndexFacade",
    "DefensiveEditor",
    "LLMClient",
)
TIME_BUDGET_S = float(os.environ.get("WW_LARGE_REPO_TIME_BUDGET", "90") or "90")
MAP_TOKEN_BUDGET = int(os.environ.get("WW_LARGE_REPO_MAP_TOKENS", "4000") or "4000")


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
            "passed": sum(1 for c in self.checks if c.ok),
            "total": len(self.checks),
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
            # Ignore empty cache shells
            if len(pys) >= 20:
                return cand, f"cache:{cand}", [cand]
    # Optional clone (off by default)
    if os.environ.get("WW_CODING_CORPUS_CLONE", "0").strip() in ("1", "true", "yes", "on"):
        cloned = _try_sparse_clone()
        if cloned is not None:
            return cloned, f"clone:{cloned}", [cloned]
    # Self-bootstrap always works offline
    roots = [p for p in SELF_ROOTS if p.is_dir()]
    return ROOT, "self_bootstrap:coding+core", roots


def _try_sparse_clone() -> Optional[Path]:
    """Allowlisted sparse clone into cache — never into main repo tree."""
    import subprocess

    dest = CACHE_CANDIDATES[0]
    dest.mkdir(parents=True, exist_ok=True)
    # Prefer a small public tree if network works; timeout hard.
    # Use a shallow clone of a tiny well-known path — cpython Lib/json only.
    marker = dest / ".ww_sparse_ok"
    if marker.is_file() and any(dest.rglob("*.py")):
        return dest
    url = os.environ.get(
        "WW_CODING_CORPUS_URL",
        "https://github.com/python/cpython.git",
    )
    try:
        # Clone to temp-ish subdir under cache only
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


def run() -> int:
    print("WW Coding LARGE REPO prove (PM 0.12)")
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

    # Work on a single root path for graph/map (prefer project when self-bootstrap)
    scan_root = str(project if source.startswith("cache") or source.startswith("clone") else ROOT)

    # ── graph_build via index facade ──────────────────────────────────
    t0 = time.time()
    try:
        from coding.index_facade import IndexFacade

        fac = IndexFacade(project_root=scan_root)
        # Bound: only build on coding+core for self-bootstrap to avoid huge trees
        if source.startswith("self"):
            # Build graph on coding/ only for speed, then also core lightly
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

    # ── repo_map token budget ─────────────────────────────────────────
    t0 = time.time()
    try:
        if fac is not None:
            mq = fac.query("map", token_budget=MAP_TOKEN_BUDGET, force_graph=True)
            map_r = mq.get("result") or {}
        else:
            from coding.perception import repo_map
            map_r = repo_map(scan_root, token_budget=MAP_TOKEN_BUDGET)
        tok = int(map_r.get("token_estimate") or 0)
        # truncated under budget or within slack
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

    # ── grep known anchors ────────────────────────────────────────────
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

    # ── facade counters present ───────────────────────────────────────
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

    # ── time bound ────────────────────────────────────────────────────
    elapsed = time.time() - t_all
    report.stats["elapsed_s"] = round(elapsed, 3)
    report.add(
        "time_bound",
        elapsed <= TIME_BUDGET_S,
        f"elapsed={elapsed:.2f}s budget={TIME_BUDGET_S}s",
    )

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
        "# Coding Large Repo Prove (PM 0.12)",
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
            f"| {c.get('name')} | {c.get('ok')} | {c.get('detail')} | {c.get('ms'):.0f} |"
        )
    lines += ["", "## Stats", "", "```json", json.dumps(payload.get("stats") or {}, indent=2), "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run())
