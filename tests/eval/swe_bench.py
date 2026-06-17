"""
SWE-bench Evaluation Harness — benchmark WW against the standard.

SWE-bench (https://www.swebench.com/) is the standard benchmark for
AI coding agents. It tests real-world bug-fixing on Python repos.

This harness:
  1. Downloads SWE-bench dataset (or uses local copy)
  2. For each instance: clones repo, checks out base commit
  3. Runs WW to attempt the fix
  4. Evaluates using the SWE-bench test patch
  5. Reports pass@k, average time, etc.

Usage:
  python -m tests.eval.swe_bench --instances 10 --model deepseek-v4-pro

Config:
  SWE_BENCH_DATA_DIR = "/path/to/SWE-bench" or auto-download
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("ww.eval.swe_bench")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class SWEInstance:
    """A single SWE-bench instance."""
    instance_id: str
    repo: str              # e.g. "django/django"
    base_commit: str
    problem_statement: str
    hint_text: str = ""
    patch: str = ""        # The golden patch
    test_patch: str = ""   # The evaluation test
    version: str = ""
    fail_to_pass: List[str] = field(default_factory=list)
    pass_to_pass: List[str] = field(default_factory=list)
    created_at: str = ""


@dataclass
class SWEResult:
    """Result of one SWE-bench instance evaluation."""
    instance_id: str
    repo: str
    resolved: bool          # Did the agent correctly fix the bug?
    applied: bool           # Did the patch apply cleanly?
    tests_passed: int = 0
    tests_total: int = 0
    duration_seconds: float = 0.0
    error: str = ""
    agent_patch: str = ""
    log: str = ""

    @property
    def pass_rate(self) -> float:
        if self.tests_total == 0:
            return 0.0
        return self.tests_passed / self.tests_total


@dataclass
class SWEBenchReport:
    """Aggregate SWE-bench evaluation report."""
    model: str
    total: int
    resolved: int
    total_duration: float
    results: List[SWEResult] = field(default_factory=list)
    timestamp: str = ""

    @property
    def resolution_rate(self) -> float:
        return self.resolved / self.total if self.total > 0 else 0.0

    @property
    def avg_duration(self) -> float:
        return self.total_duration / self.total if self.total > 0 else 0.0

    def to_markdown(self) -> str:
        lines = [
            f"# SWE-bench Results — {self.model}",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Instances | {self.total} |",
            f"| Resolved | {self.resolved} |",
            f"| Resolution Rate | {self.resolution_rate:.1%} |",
            f"| Avg Duration | {self.avg_duration:.0f}s |",
            f"| Timestamp | {self.timestamp} |",
            "",
            "## Per-Instance Results",
            "",
        ]
        for r in self.results:
            status = "✅" if r.resolved else "❌"
            lines.append(f"| {status} | {r.instance_id} | {r.repo} | {r.duration_seconds:.0f}s | {'OK' if r.resolved else r.error[:50]} |")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "model": self.model,
            "total": self.total,
            "resolved": self.resolved,
            "resolution_rate": self.resolution_rate,
            "avg_duration": self.avg_duration,
            "timestamp": self.timestamp,
            "results": [
                {
                    "instance_id": r.instance_id,
                    "repo": r.repo,
                    "resolved": r.resolved,
                    "duration": r.duration_seconds,
                    "tests_passed": r.tests_passed,
                    "tests_total": r.tests_total,
                }
                for r in self.results
            ],
        }


# ── SWE-bench Harness ────────────────────────────────────────────

class SWEBenchHarness:
    """Evaluates WW on SWE-bench instances."""

    def __init__(
        self,
        data_dir: str = "",
        work_dir: str = "",
        model: str = "",
        max_instances: int = 0,
        timeout_per_instance: int = 300,
    ):
        self._data_dir = data_dir or os.environ.get(
            "SWE_BENCH_DATA_DIR",
            os.path.join(tempfile.gettempdir(), "swe_bench_data"),
        )
        self._work_dir = work_dir or os.path.join(tempfile.gettempdir(), "swe_bench_work")
        self._model = model or os.environ.get("WW_MODEL", "deepseek/deepseek-v4-flash")
        self._max_instances = max_instances
        self._timeout = timeout_per_instance

        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(self._work_dir, exist_ok=True)

    # ── Public API ───────────────────────────────────────────────

    def load_instances(self, limit: int = 0) -> List[SWEInstance]:
        """Load SWE-bench instances from local data or download."""
        # Try SWE-bench Lite first (300 curated instances)
        lite_path = os.path.join(self._data_dir, "swe-bench-lite.json")
        if os.path.exists(lite_path):
            with open(lite_path) as f:
                data = json.load(f)
        else:
            # Try the full dataset
            full_path = os.path.join(self._data_dir, "swe-bench.json")
            if os.path.exists(full_path):
                with open(full_path) as f:
                    data = json.load(f)
            else:
                log.warning("No SWE-bench data found. Download with: pip install swebench && python -m swebench.harness.run_evaluation --help")
                return []

        instances = []
        for item in data[:limit or len(data)]:
            instances.append(SWEInstance(
                instance_id=item.get("instance_id", ""),
                repo=item.get("repo", ""),
                base_commit=item.get("base_commit", ""),
                problem_statement=item.get("problem_statement", ""),
                hint_text=item.get("hint_text", ""),
                patch=item.get("patch", ""),
                test_patch=item.get("test_patch", ""),
                version=item.get("version", ""),
                fail_to_pass=item.get("FAIL_TO_PASS", []),
                pass_to_pass=item.get("PASS_TO_PASS", []),
                created_at=item.get("created_at", ""),
            ))
        return instances

    def evaluate(self, instances: Optional[List[SWEInstance]] = None,
                 fix_fn: Optional[Callable] = None) -> SWEBenchReport:
        """Run evaluation on a set of instances.

        Args:
            instances: SWE-bench instances (auto-loaded if None)
            fix_fn: Callable(repo_path, problem_statement) → patch_text.
                    If None, uses WW via subprocess.

        Returns:
            SWEBenchReport with aggregate results.
        """
        if instances is None:
            instances = self.load_instances(self._max_instances)

        results = []
        resolved_count = 0
        total_duration = 0.0

        for i, inst in enumerate(instances):
            log.info("[%d/%d] Evaluating %s", i + 1, len(instances), inst.instance_id)

            result = self._evaluate_one(inst, fix_fn)
            results.append(result)

            if result.resolved:
                resolved_count += 1
            total_duration += result.duration_seconds

        report = SWEBenchReport(
            model=self._model,
            total=len(instances),
            resolved=resolved_count,
            total_duration=total_duration,
            results=results,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        return report

    def evaluate_one(self, instance_id: str) -> SWEResult:
        """Evaluate a single instance by ID."""
        instances = self.load_instances()
        for inst in instances:
            if inst.instance_id == instance_id:
                return self._evaluate_one(inst)
        return SWEResult(instance_id=instance_id, repo="", resolved=False, applied=False, error="Instance not found")

    # ── Internal ─────────────────────────────────────────────────

    def _evaluate_one(self, inst: SWEInstance, fix_fn=None) -> SWEResult:
        start = time.time()
        result = SWEResult(
            instance_id=inst.instance_id,
            repo=inst.repo,
            resolved=False,
        )

        clone_dir = None
        try:
            # Clone repo at base commit
            clone_dir = os.path.join(self._work_dir, inst.instance_id.replace("/", "_"))
            self._clone_at_commit(inst.repo, inst.base_commit, clone_dir)

            # Apply test patch (so we can evaluate)
            if inst.test_patch:
                self._apply_patch(clone_dir, inst.test_patch)

            # Attempt fix
            if fix_fn:
                agent_patch = fix_fn(clone_dir, inst.problem_statement)
            else:
                agent_patch = self._run_ww_fix(clone_dir, inst.problem_statement)

            result.agent_patch = agent_patch

            # Evaluate: run the tests
            if agent_patch:
                self._apply_patch(clone_dir, agent_patch)
                passed, total, log_output = self._run_eval_tests(clone_dir, inst)
                result.tests_passed = passed
                result.tests_total = total
                result.log = log_output
                result.resolved = (passed == total) and total > 0

        except Exception as e:
            result.error = str(e)
            log.exception("Failed on %s", inst.instance_id)
        finally:
            if clone_dir and os.path.exists(clone_dir):
                import shutil
                shutil.rmtree(clone_dir, ignore_errors=True)

        result.duration_seconds = round(time.time() - start, 1)
        return result

    def _clone_at_commit(self, repo: str, commit: str, target: str):
        """Clone a repo at a specific commit."""
        url = f"https://github.com/{repo}.git"
        subprocess.run(
            ["git", "clone", "--depth=1", url, target],
            capture_output=True, timeout=120, check=True,
        )
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", commit],
            cwd=target, capture_output=True, timeout=60, check=True,
        )
        subprocess.run(
            ["git", "checkout", commit],
            cwd=target, capture_output=True, timeout=30, check=True,
        )

    def _apply_patch(self, repo_dir: str, patch_text: str):
        """Apply a git patch."""
        patch_file = os.path.join(repo_dir, "_temp.patch")
        with open(patch_file, "w") as f:
            f.write(patch_text)
        subprocess.run(
            ["git", "apply", "--verbose", patch_file],
            cwd=repo_dir, capture_output=True, timeout=30,
        )
        os.unlink(patch_file)

    def _run_ww_fix(self, repo_dir: str, problem: str) -> str:
        """Run WW to fix a problem. Returns patch text."""
        try:
            result = subprocess.run(
                ["ww", "run", problem],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            # Get the diff
            diff_result = subprocess.run(
                ["git", "diff"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return diff_result.stdout
        except subprocess.TimeoutExpired:
            return ""
        except Exception as e:
            log.error("WW fix failed: %s", e)
            return ""

    def _run_eval_tests(self, repo_dir: str, inst: SWEInstance) -> Tuple[int, int, str]:
        """Run evaluation tests. Returns (passed, total, log)."""
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", "-x", "--tb=short", "-q"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout + "\n" + result.stderr
            # Parse pytest summary
            import re
            match = re.search(r"(\d+) passed", output)
            passed = int(match.group(1)) if match else 0
            match = re.search(r"(\d+) failed", output)
            failed = int(match.group(1)) if match else 0
            return passed, passed + failed, output
        except Exception as e:
            return 0, 0, str(e)


# ── Entry point ──────────────────────────────────────────────────

def run_swe_bench(
    instances: int = 10,
    model: str = "",
    data_dir: str = "",
    output_path: str = "",
) -> SWEBenchReport:
    """Run SWE-bench evaluation and save results.

    Args:
        instances: Number of instances to evaluate
        model: Model to use
        data_dir: Path to SWE-bench data
        output_path: Where to save the JSON report

    Returns:
        SWEBenchReport
    """
    harness = SWEBenchHarness(
        data_dir=data_dir,
        model=model,
        max_instances=instances,
    )
    report = harness.evaluate()

    if output_path:
        with open(output_path, "w") as f:
            json.dump(report.to_json(), f, indent=2)

    return report


if __name__ == "__main__":
    import sys
    instances = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    report = run_swe_bench(instances=instances)
    print(report.to_markdown())
