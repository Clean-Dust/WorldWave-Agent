"""HumanEval Evaluation Harness — benchmark WW on code generation.

HumanEval (Chen et al., 2021) is the standard benchmark for code generation:
  164 handwritten Python problems, each with:
    - A function signature + docstring
    - A canonical solution
    - A set of test cases

WW must complete the function body given the signature.

Usage:
  python -m tests.eval.humaneval --model deepseek-v4-pro --instances 164
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

log = logging.getLogger("ww.eval.humaneval")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class HumanEvalInstance:
    task_id: str           # e.g. "HumanEval/0"
    prompt: str            # Function signature + docstring
    canonical_solution: str
    test: str              # assert-based test cases
    entry_point: str       # Function name
    difficulty: str = ""   # easy/medium/hard (estimated)


@dataclass
class HumanEvalResult:
    task_id: str
    passed: bool
    generated_code: str = ""
    error: str = ""
    duration_seconds: float = 0.0


@dataclass
class HumanEvalReport:
    model: str
    total: int
    passed: int
    results: List[HumanEvalResult] = field(default_factory=list)
    timestamp: str = ""

    @property
    def pass_at_1(self) -> float:
        return self.passed / max(1, self.total)

    def to_markdown(self) -> str:
        lines = [
            f"# HumanEval Results — {self.model}",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Instances | {self.total} |",
            f"| Passed | {self.passed} |",
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


# ── HumanEval Harness ────────────────────────────────────────────

class HumanEvalHarness:
    """Evaluates WW on HumanEval code generation tasks."""

    def __init__(
        self,
        data_dir: str = "",
        model: str = "",
        timeout_per_instance: int = 30,
    ):
        self._data_dir = data_dir or os.path.join(
            tempfile.gettempdir(), "humaneval_data"
        )
        self._model = model or os.environ.get("WW_MODEL", "deepseek-v4-flash")
        self._timeout = timeout_per_instance
        os.makedirs(self._data_dir, exist_ok=True)

    def load_instances(self, limit: int = 0) -> List[HumanEvalInstance]:
        """Load HumanEval instances from local data or download."""
        human_eval_path = os.path.join(self._data_dir, "HumanEval.jsonl")
        if not os.path.exists(human_eval_path):
            log.warning(
                "HumanEval dataset not found at %s. "
                "Download with: pip install datasets && python -c "
                "\"from datasets import load_dataset; "
                "ds = load_dataset('openai_humaneval'); "
                "ds['test'].to_json('%s')\"",
                human_eval_path, human_eval_path,
            )
            return []

        instances = []
        with open(human_eval_path) as f:
            for i, line in enumerate(f):
                if limit and i >= limit:
                    break
                item = json.loads(line)
                instances.append(HumanEvalInstance(
                    task_id=item.get("task_id", f"HumanEval/{i}"),
                    prompt=item.get("prompt", ""),
                    canonical_solution=item.get("canonical_solution", ""),
                    test=item.get("test", ""),
                    entry_point=item.get("entry_point", ""),
                ))
        return instances

    def evaluate(
        self,
        instances: Optional[List[HumanEvalInstance]] = None,
    ) -> HumanEvalReport:
        """Run evaluation on HumanEval instances."""
        if instances is None:
            instances = self.load_instances()

        report = HumanEvalReport(
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
            log.info("[%d/%d] %s", i + 1, len(instances), inst.task_id)
            t0 = time.time()

            try:
                # Prompt WW to complete the function
                code = llm.chat(
                    messages=[{
                        "role": "user",
                        "content": (
                            "Complete the following Python function. "
                            "Return ONLY the function body, no explanation.\n\n"
                            + inst.prompt
                        ),
                    }],
                    json_mode=False,
                    temperature=0.0,
                    max_tokens=1024,
                )

                # Extract just the function body
                generated = self._extract_code(code, inst.prompt)

                # Test execution
                passed = self._run_tests(generated, inst.test, inst.entry_point)
                duration = time.time() - t0

            except Exception as e:
                passed = False
                generated = ""
                duration = time.time() - t0

            result = HumanEvalResult(
                task_id=inst.task_id,
                passed=passed,
                generated_code=generated,
                error="" if passed else "Test failed or error",
                duration_seconds=duration,
            )
            report.results.append(result)
            if passed:
                report.passed += 1

            log.info("  -> %s (%.1fs)", "PASS" if passed else "FAIL", duration)

        return report

    @staticmethod
    def _extract_code(generated: str, prompt: str) -> str:
        """Extract the function from the generated code."""
        # Try to find the function definition and everything after it
        code = generated.strip()

        # Remove markdown code fences if present
        if code.startswith("```"):
            lines = code.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)

        # If the response includes the prompt, extract only the new part
        prompt_signature = prompt.strip().split("\n")[0]
        if prompt_signature in code:
            idx = code.index(prompt_signature)
            code = code[idx:]

        return code

    @staticmethod
    def _run_tests(code: str, test: str, entry_point: str) -> bool:
        """Run the test cases in a subprocess."""
        full_code = code + "\n\n" + test + f"\n\ncheck({entry_point})\n"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(full_code)
            tmp_path = f.name

        try:
            result = subprocess.run(
                ["python3", tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── CLI ───────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="HumanEval evaluation for WW")
    ap.add_argument("--model", default="", help="Model name")
    ap.add_argument("--instances", type=int, default=0, help="Number of instances (0=all)")
    ap.add_argument("--output", default="", help="Output JSON path")
    ap.add_argument("--data-dir", default="", help="Dataset directory")
    args = ap.parse_args()

    harness = HumanEvalHarness(
        data_dir=args.data_dir,
        model=args.model,
    )

    instances = harness.load_instances(limit=args.instances)
    if not instances:
        print("No HumanEval instances found. Download the dataset first.")
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
