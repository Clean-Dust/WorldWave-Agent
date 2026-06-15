"""LiveCodeBench Evaluation Harness — benchmark WW on fresh coding problems.

LiveCodeBench (Jain et al., 2024) collects problems from LeetCode, AtCoder,
and Codeforces that were posted within a recent time window. This ensures
the problems are NOT in any LLM training set.

Usage:
  python -m tests.eval.livecodebench --model deepseek-v4-pro --time-window 2024-06
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
from pathlib import Path
from typing import Any, List, Optional

log = logging.getLogger("ww.eval.livecodebench")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class LiveCodeInstance:
    problem_id: str
    platform: str         # leetcode, atcoder, codeforces
    title: str
    description: str      # Problem statement in markdown
    difficulty: str       # easy, medium, hard
    starter_code: str = ""
    public_tests: List[dict] = field(default_factory=list)
    private_tests: List[dict] = field(default_factory=list)
    time_limit_ms: int = 2000
    memory_limit_mb: int = 256
    date_posted: str = ""


@dataclass
class LiveCodeResult:
    problem_id: str
    passed: bool
    public_tests_passed: int = 0
    public_tests_total: int = 0
    private_tests_passed: int = 0
    private_tests_total: int = 0
    generated_code: str = ""
    error: str = ""
    duration_seconds: float = 0.0


@dataclass
class LiveCodeReport:
    model: str
    total: int
    passed: int
    results: List[LiveCodeResult] = field(default_factory=list)
    timestamp: str = ""

    @property
    def pass_at_1(self) -> float:
        return self.passed / max(1, self.total)

    def to_markdown(self) -> str:
        lines = [
            f"# LiveCodeBench Results — {self.model}",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Problems | {self.total} |",
            f"| Solved | {self.passed} |",
            f"| pass@1 | {self.pass_at_1:.1%} |",
            f"| Timestamp | {self.timestamp} |",
        ]
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "model": self.model,
            "total": self.total,
            "passed": self.passed,
            "pass_at_1": self.pass_at_1,
            "timestamp": self.timestamp,
        }


# ── LiveCodeBench Harness ────────────────────────────────────────

class LiveCodeBenchHarness:
    """Evaluates WW on LiveCodeBench problems."""

    def __init__(
        self,
        data_dir: str = "",
        model: str = "",
        time_window: str = "",
        timeout_per_problem: int = 60,
    ):
        self._data_dir = data_dir or os.path.join(
            tempfile.gettempdir(), "livecodebench_data"
        )
        self._model = model or os.environ.get("WW_MODEL", "deepseek-v4-flash")
        self._time_window = time_window
        self._timeout = timeout_per_problem
        os.makedirs(self._data_dir, exist_ok=True)

    def load_instances(self, limit: int = 0) -> List[LiveCodeInstance]:
        """Load LiveCodeBench instances.

        Downloads from the livecodebench PyPI package if available,
        otherwise uses a local JSON file.
        """
        lcb_path = os.path.join(self._data_dir, "livecodebench.json")
        if not os.path.exists(lcb_path):
            # Try to download using the livecodebench package
            try:
                import subprocess
                result = subprocess.run(
                    ["pip", "install", "livecodebench"],
                    capture_output=True, text=True, timeout=120,
                )
                from livecodebench import get_data
                data = get_data(time_window=self._time_window or "2024-06-01_2024-09-01")
                with open(lcb_path, "w") as f:
                    json.dump(data, f)
                log.info("Downloaded LiveCodeBench data")
            except Exception as e:
                log.warning("Cannot download LiveCodeBench: %s", e)
                return []

        if not os.path.exists(lcb_path):
            return []

        with open(lcb_path) as f:
            data = json.load(f)

        instances = []
        for item in data[:limit or len(data)]:
            instances.append(LiveCodeInstance(
                problem_id=item.get("problem_id", ""),
                platform=item.get("platform", "unknown"),
                title=item.get("title", ""),
                description=item.get("description", ""),
                difficulty=item.get("difficulty", "medium"),
                starter_code=item.get("starter_code", ""),
                public_tests=item.get("public_tests", []),
                private_tests=item.get("private_tests", []),
                time_limit_ms=item.get("time_limit_ms", 2000),
                memory_limit_mb=item.get("memory_limit_mb", 256),
                date_posted=item.get("date_posted", ""),
            ))
        return instances

    def evaluate(
        self,
        instances: Optional[List[LiveCodeInstance]] = None,
    ) -> LiveCodeReport:
        """Run evaluation on LiveCodeBench instances."""
        if instances is None:
            instances = self.load_instances()

        report = LiveCodeReport(
            model=self._model,
            total=len(instances),
            passed=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))))

        from core.llm import LLMClient

        llm = LLMClient(model=self._model)

        for i, inst in enumerate(instances):
            log.info("[%d/%d] %s (%s)", i + 1, len(instances), inst.title, inst.difficulty)
            t0 = time.time()

            try:
                prompt = self._build_prompt(inst)
                code = llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    json_mode=False,
                    temperature=0.1,
                    max_tokens=2048,
                )
                generated = self._extract_code(code)

                # Test with public + private tests
                pub_passed, pub_total = self._run_test_cases(
                    generated, inst.public_tests
                )
                priv_passed, priv_total = self._run_test_cases(
                    generated, inst.private_tests
                )
                all_passed = (pub_passed == pub_total) and (
                    priv_passed == priv_total
                )
                duration = time.time() - t0

            except Exception as e:
                all_passed = False
                pub_passed = pub_total = priv_passed = priv_total = 0
                generated = ""
                duration = time.time() - t0

            result = LiveCodeResult(
                problem_id=inst.problem_id,
                passed=all_passed,
                public_tests_passed=pub_passed,
                public_tests_total=pub_total,
                private_tests_passed=priv_passed,
                private_tests_total=priv_total,
                generated_code=generated,
                error="" if all_passed else "Tests failed",
                duration_seconds=duration,
            )
            report.results.append(result)
            if all_passed:
                report.passed += 1

            log.info(
                "  -> %s pub=%d/%d priv=%d/%d (%.1fs)",
                "PASS" if all_passed else "FAIL",
                pub_passed, pub_total, priv_passed, priv_total, duration,
            )

        return report

    @staticmethod
    def _build_prompt(inst: LiveCodeInstance) -> str:
        """Build the coding prompt for WW."""
        parts = [
            f"Solve this {inst.difficulty} coding problem from {inst.platform}.",
            f"Title: {inst.title}",
            "",
            inst.description,
        ]
        if inst.starter_code:
            parts.extend(["", "Starter code:", "```python", inst.starter_code, "```"])
        parts.extend([
            "",
            "Return ONLY the complete solution code, no explanation.",
            "The solution must handle all edge cases and pass the test suite.",
        ])
        return "\n".join(parts)

    @staticmethod
    def _extract_code(generated: str) -> str:
        """Extract code from LLM response."""
        code = generated.strip()
        if code.startswith("```"):
            lines = code.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)
        return code

    @staticmethod
    def _run_test_cases(
        code: str, tests: List[dict]
    ) -> tuple:
        """Run a set of test cases and return (passed, total)."""
        if not tests:
            return 0, 0

        passed = 0
        for tc in tests:
            input_data = tc.get("input", "")
            expected = tc.get("expected_output", "")

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            ) as f:
                f.write(code)
                tmp_path = f.name

            try:
                result = subprocess.run(
                    ["python3", tmp_path],
                    input=input_data,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = result.stdout.strip()
                if output == expected.strip():
                    passed += 1
            except (subprocess.TimeoutExpired, Exception):
                pass
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return passed, len(tests)


# ── CLI ───────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="LiveCodeBench evaluation for WW")
    ap.add_argument("--model", default="", help="Model name")
    ap.add_argument("--time-window", default="", help="Time window e.g. 2024-06")
    ap.add_argument("--instances", type=int, default=0, help="Number of problems (0=all)")
    ap.add_argument("--output", default="", help="Output JSON path")
    ap.add_argument("--data-dir", default="", help="Dataset directory")
    args = ap.parse_args()

    harness = LiveCodeBenchHarness(
        data_dir=args.data_dir,
        model=args.model,
        time_window=args.time_window,
    )

    instances = harness.load_instances(limit=args.instances)
    if not instances:
        print("No LiveCodeBench problems found.")
        return

    report = harness.evaluate(instances)
    print(report.to_markdown())

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(report.to_json(), f, indent=2)
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
