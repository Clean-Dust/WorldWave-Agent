"""ww/pm/circuit.py — Circuit Breaker & Test/Auto-repair Loop v0.1

Implements Gemini's WW-PM Subsystem 3.4.2:
- Repair attempt tracking per code region (error fingerprinting)
- Multi-line error pattern matching (Allure/JUnit XML aware)
- 3-strike circuit breaker with auto-git-rollback
- Structured error log generation for human handoff

Architecture:
  ErrorFingerprint — normalizes error output into stable signatures
  RepairTracker   — tracks attempts and detects repeated patterns
  CircuitBreaker  — 3-strike policy with git rollback
  TestRunner      — runs tests and parses structured results
"""

from __future__ import annotations
import hashlib
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ── Error Fingerprinting ──────────────────────────────────────────────

class ErrorFingerprint:
    """Normalize error output into stable, comparable signatures.

    Strips line numbers, memory addresses, timestamps, and file paths
    so the same logical error produces the same fingerprint.
    """

    @staticmethod
    def fingerprint(text: str) -> str:
        """Generate a stable hash from error text, normalizing variable data."""
        normalized = text.lower()

        # Remove absolute paths
        normalized = re.sub(r'/[\w/.-]+', '', normalized)

        # Remove line numbers (e.g., "line 42", ":42:")
        normalized = re.sub(r'line \d+', 'line N', normalized)
        normalized = re.sub(r':\d+:', ':N:', normalized)

        # Remove memory addresses (0x...)
        normalized = re.sub(r'0x[0-9a-f]+', '0x...', normalized)

        # Remove hex numbers (error codes)
        normalized = re.sub(r'0x[0-9a-f]{4,}', '0x...', normalized)

        # Remove timestamps
        normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '<ts>', normalized)

        # Remove UUIDs
        normalized = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<uuid>', normalized)

        # Remove file paths with extensions
        normalized = re.sub(r'\b\w+\.\w{1,4}\b', '<file>', normalized)

        # Remove variable numeric values
        normalized = re.sub(r'\b\d+\b', '<N>', normalized)

        # Collapse whitespace
        normalized = re.sub(r'\s+', ' ', normalized).strip()

        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    @staticmethod
    def extract_key_lines(text: str, max_lines: int = 15) -> List[str]:
        """Extract the most meaningful error lines (traceback, assertion, etc.)."""
        lines = text.split("\n")
        key_lines = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Include: Traceback lines, Error lines, AssertionError, FAIL lines
            if any(kw in stripped for kw in [
                "Traceback", "Error:", "Exception:", "AssertionError",
                "FAIL", "FAILED", "SyntaxError", "ImportError",
                "ModuleNotFoundError", "TypeError", "ValueError",
                "KeyError", "IndexError", "AttributeError",
                "Error: ", "error: ", "FAIL:", "warning: ",
            ]):
                key_lines.append(stripped[:200])
                if len(key_lines) >= max_lines:
                    break

        return key_lines or lines[:max_lines]


# ── Repair Attempt Tracking ───────────────────────────────────────────

class RepairRecord:
    """Record of a single repair attempt."""

    def __init__(
        self,
        filepath: str,
        error_fingerprint: str,
        error_snippet: str,
        diff: str = "",
        outcome: str = "unknown",
    ):
        self.id = uuid.uuid4().hex[:8]
        self.filepath = filepath
        self.error_fingerprint = error_fingerprint
        self.error_snippet = error_snippet
        self.diff = diff
        self.outcome = outcome
        self.timestamp = time.time()

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "filepath": self.filepath,
            "fingerprint": self.error_fingerprint,
            "error_snippet": self.error_snippet[:300],
            "diff": self.diff[:500],
            "outcome": self.outcome,
            "timestamp": datetime.fromtimestamp(self.timestamp).isoformat(),
        }


class RepairTracker:
    """Track repair attempts per file and detect repeated error patterns."""

    def __init__(self, max_attempts: int = 3):
        self._max_attempts = max_attempts
        self._records: Dict[str, List[RepairRecord]] = {}  # filepath -> records
        self._history: Dict[str, List[str]] = {}  # fingerprint -> list of record ids

    def record_attempt(
        self,
        filepath: str,
        error_text: str,
        diff: str = "",
    ) -> RepairRecord:
        """Record a repair attempt and check for repeated patterns."""
        fp = ErrorFingerprint.fingerprint(error_text)
        key_lines = ErrorFingerprint.extract_key_lines(error_text)

        record = RepairRecord(
            filepath=filepath,
            error_fingerprint=fp,
            error_snippet="\n".join(key_lines[:5]),
            diff=diff,
            outcome="failed",
        )

        if filepath not in self._records:
            self._records[filepath] = []
        self._records[filepath].append(record)

        if fp not in self._history:
            self._history[fp] = []
        self._history[fp].append(record.id)

        return record

    def strike_count(self, filepath: str) -> int:
        """Get the number of failed attempts for a file."""
        return len(self._records.get(filepath, []))

    def is_repeated_error(self, error_text: str) -> bool:
        """Check if this exact error has been seen before (same fingerprint)."""
        fp = ErrorFingerprint.fingerprint(error_text)
        return fp in self._history

    def is_circuit_tripped(self, filepath: str) -> bool:
        """Check if the circuit breaker should trip for this file."""
        return self.strike_count(filepath) >= self._max_attempts

    def get_same_error_attempts(self, error_text: str) -> int:
        """Get how many times this specific error has been encountered."""
        fp = ErrorFingerprint.fingerprint(error_text)
        return len(self._history.get(fp, []))

    def get_records(self, filepath: str = None) -> List[Dict]:
        """Get all repair records, optionally filtered by file."""
        if filepath:
            records = self._records.get(filepath, [])
        else:
            records = []
            for recs in self._records.values():
                records.extend(recs)
        return [r.to_dict() for r in records]

    def clear_file(self, filepath: str):
        """Clear records for a file (e.g., after successful repair)."""
        self._records.pop(filepath, None)

    def reset(self):
        """Reset all tracking."""
        self._records.clear()
        self._history.clear()


# ── Git Rollback ──────────────────────────────────────────────────────

class GitRollback:
    """Git-based automatic rollback to stable state."""

    def __init__(self, repo_path: str = None):
        self._repo_path = repo_path or self._find_repo()
        self._saved_stash: Optional[str] = None

    def _find_repo(self) -> str:
        """Walk up to find git root."""
        path = os.getcwd()
        for _ in range(10):
            if os.path.isdir(os.path.join(path, ".git")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        return os.getcwd()

    def stash_current(self) -> Dict:
        """Stash current working changes."""
        result = self._run_git("stash", "push", "-m", f"ww-circuit-breaker-{int(time.time())}")
        if result["success"]:
            self._saved_stash = result.get("output", "").strip()
        return result

    def rollback_file(self, filepath: str) -> Dict:
        """Rollback a single file to the last committed state."""
        abs_path = os.path.abspath(filepath) if not os.path.isabs(filepath) else filepath
        rel_path = os.path.relpath(abs_path, self._repo_path)
        return self._run_git("checkout", "--", rel_path)

    def rollback_all(self) -> Dict:
        """Hard reset to last commit (all files)."""
        return self._run_git("reset", "--hard", "HEAD")

    def get_diff(self, filepath: str = None) -> Dict:
        """Get current diff for a file or all changes."""
        if filepath:
            return self._run_git("diff", filepath)
        return self._run_git("diff")

    def get_last_committed_content(self, filepath: str) -> Optional[str]:
        """Get the last committed version of a file."""
        rel_path = os.path.relpath(
            os.path.abspath(filepath), self._repo_path
        )
        result = self._run_git("show", f"HEAD:{rel_path}")
        if result["success"]:
            return result["output"]
        return None

    def _run_git(self, *args: str) -> Dict:
        """Run a git command."""
        try:
            result = subprocess.run(
                ["git"] + list(args),
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self._repo_path,
            )
            return {
                "success": result.returncode == 0,
                "output": result.stdout,
                "error": result.stderr,
                "exit_code": result.returncode,
            }
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return {"success": False, "error": str(e)}


# ── Test Runner ───────────────────────────────────────────────────────

class TestRunner:
    """Run tests and parse structured results."""

    SUPPORTED_FRAMEWORKS = {
        "pytest": {
            "command": ["python3", "-m", "pytest"],
            "success_pattern": r"(\d+) passed",
            "fail_pattern": r"(\d+) failed",
            "error_pattern": r"(ERROR|FAILED|FAIL)",
            "xml_flag": "--junit-xml",
        },
        "node": {
            "command": ["npm", "test"],
            "success_pattern": r"(passing|ok)",
            "fail_pattern": r"(failing|FAIL)",
            "error_pattern": r"(Error|FAIL)",
        },
    }

    def __init__(self, framework: str = "pytest"):
        self._framework = framework
        self._config = self.SUPPORTED_FRAMEWORKS.get(framework)
        self._last_output = ""
        self._last_xml_path: Optional[str] = None

    def run(self, test_path: str = None, extra_args: List[str] = None) -> Dict:
        """Run tests and return structured results.

        Without an explicit *test_path*, only runs ``./tests`` when that
        directory exists in *cwd*. Never falls back to collecting the entire
        repository (avoids multi-minute hangs in sandboxes / arena).
        """
        if self._config is None:
            return {"error": f"Unsupported framework: {self._framework}"}

        cwd = os.getcwd()
        resolved = test_path
        if not resolved:
            cand = os.path.join(cwd, "tests")
            if os.path.isdir(cand):
                resolved = cand
            else:
                return {
                    "success": False,
                    "error": "No test_path provided and no ./tests directory in cwd",
                    "passed": 0,
                    "failed": 0,
                    "total": 0,
                    "exit_code": -1,
                    "output": "",
                    "framework": self._framework,
                    "fingerprint": ErrorFingerprint.fingerprint("no-test-path"),
                }

        cmd = list(self._config["command"])
        cmd.append(resolved)

        # Add junit-xml if supported
        if "xml_flag" in self._config:
            xml_path = f"/tmp/ww-test-results-{uuid.uuid4().hex[:8]}.xml"
            cmd.extend([self._config["xml_flag"], xml_path])
            self._last_xml_path = xml_path

        if extra_args:
            cmd.extend(extra_args)

        # Bound wall time; arena/sandbox can tighten via WW_CODING_TEST_TIMEOUT
        try:
            timeout_s = int(os.environ.get("WW_CODING_TEST_TIMEOUT", "120") or "120")
        except ValueError:
            timeout_s = 120
        timeout_s = max(5, min(timeout_s, 120))

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s, cwd=cwd
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Test timed out ({timeout_s}s)",
                "framework": self._framework,
            }
        except FileNotFoundError:
            return {"error": f"Test framework not found: {self._config['command'][0]}"}

        self._last_output = result.stdout + "\n" + result.stderr
        return self._parse_results(result)

    def _parse_results(self, result: subprocess.CompletedProcess) -> Dict:
        """Parse test output for structured results."""
        output = result.stdout + "\n" + result.stderr
        passed = 0
        failed = 0
        errors = []

        # Try to parse structured output
        if self._framework == "pytest":
            # Parse pytest summary line: "3 passed, 1 failed in 0.10s"
            passed_match = re.search(r"(\d+) passed", output)
            failed_match = re.search(r"(\d+) failed", output)
            error_match = re.search(r"(\d+) error", output)

            passed = int(passed_match.group(1)) if passed_match else 0
            failed = int(failed_match.group(1)) if failed_match else 0
            errors_count = int(error_match.group(1)) if error_match else 0

            # Extract failure details
            for line in output.split("\n"):
                if "FAILED" in line or "ERROR" in line:
                    errors.append(line.strip())

            # Try parsing junit-xml
            xml_diags = self._parse_junit_xml()
            if xml_diags:
                errors.extend(xml_diags)

        total = passed + failed

        return {
            "success": result.returncode == 0,
            "passed": passed,
            "failed": failed,
            "total": total,
            "output": output[-2000:],  # last 2000 chars
            "errors": errors[:20],
            "exit_code": result.returncode,
            "framework": self._framework,
        }

    def _parse_junit_xml(self) -> List[str]:
        """Parse JUnit XML test results for structured diagnostics using xml.etree."""
        if not self._last_xml_path or not os.path.isfile(self._last_xml_path):
            return []

        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(self._last_xml_path)
            root = tree.getroot()

            failures = []
            for testcase in root.iter("testcase"):
                name = testcase.get("name", "?")
                classname = testcase.get("classname", "")
                full_name = f"{classname}.{name}" if classname else name

                failure = testcase.find("failure")
                if failure is not None:
                    msg = failure.get("message", "")[:200]
                    failures.append(f"  FAIL {full_name}: {msg}")

                error = testcase.find("error")
                if error is not None:
                    msg = error.get("message", "")[:200]
                    failures.append(f"  ERROR {full_name}: {msg}")

            return failures
        except (IOError, ET.ParseError, ValueError) as e:
            return [f"XML parse error: {e}"]

    @property
    def last_output(self) -> str:
        return self._last_output


# ── Circuit Breaker ───────────────────────────────────────────────────

class CircuitBreakerError(Exception):
    """Raised when the circuit breaker trips."""
    pass


class CircuitBreaker:
    """Smart circuit breaker with error pattern detection and git rollback.

    Tracks repair attempts per file. After 3 strikes (same or different errors
    on the same file), auto-rolls back via git and generates an analysis report.
    """

    def __init__(
        self,
        max_strikes: int = 3,
        enable_rollback: bool = True,
        repo_path: str = None,
    ):
        self._max_strikes = max_strikes
        self._enable_rollback = enable_rollback
        self._tracker = RepairTracker(max_attempts=max_strikes)
        self._git = GitRollback(repo_path)
        self._test_runner = TestRunner("pytest")
        self._tripped: Dict[str, Dict] = {}  # filepath -> trip report

    def before_edit(self, filepath: str) -> Dict:
        """Call before making an edit. Returns warnings if close to limit."""
        strikes = self._tracker.strike_count(filepath)
        remaining = self._max_strikes - strikes
        return {
            "filepath": filepath,
            "strikes": strikes,
            "remaining_attempts": remaining,
            "tripped": self._is_tripped(filepath),
        }

    def after_edit(
        self,
        filepath: str,
        success: bool,
        error_text: str = "",
        diff: str = "",
        run_tests: bool = False,
    ) -> Dict:
        """Call after an edit attempt. Handles success/failure + circuit tripping."""
        if success:
            self._tracker.clear_file(filepath)
            return {"status": "success", "filepath": filepath}

        # Record the failure
        record = self._tracker.record_attempt(filepath, error_text, diff)
        strikes = self._tracker.strike_count(filepath)

        result = {
            "status": "failed",
            "filepath": filepath,
            "strikes": strikes,
            "max_strikes": self._max_strikes,
            "record": record.to_dict(),
            "is_repeated": self._tracker.is_repeated_error(error_text),
        }

        # Same fingerprint thrice also trips (even if under file strike budget)
        same_fp = self._tracker.get_same_error_attempts(error_text)
        result["same_fingerprint_count"] = same_fp

        # Check circuit breaker: per-file strikes OR same fingerprint x3
        if strikes >= self._max_strikes or same_fp >= self._max_strikes:
            trip_report = self._trip(filepath, error_text, diff)
            result["tripped"] = True
            result["trip_report"] = trip_report
            if same_fp >= self._max_strikes:
                result["trip_reason"] = "same_fingerprint"
                trip_report["trip_reason"] = "same_fingerprint"
                trip_report["recommendation"] = (
                    f"Circuit breaker tripped: same error fingerprint seen "
                    f"{same_fp} times on {filepath}. Stop thrashing; handoff report generated."
                )

            # Auto-rollback
            if self._enable_rollback:
                rb_result = self._git.rollback_file(filepath)
                result["rollback"] = rb_result

        return result

    def run_tests(self, test_path: str = None) -> Dict:
        """Run tests and return results with structured diagnostics."""
        result = self._test_runner.run(test_path)
        return result

    def _trip(self, filepath: str, error_text: str, diff: str) -> Dict:
        """Trip the circuit breaker for a file."""
        key_lines = ErrorFingerprint.extract_key_lines(error_text)
        records = self._tracker.get_records(filepath)

        report = {
            "circuit_tripped": True,
            "filepath": filepath,
            "total_attempts": len(records),
            "last_error_snippet": "\n".join(key_lines[:10]),
            "error_fingerprints": list(set(
                r["fingerprint"] for r in records
            )),
            "records": records,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": (
                f"Circuit breaker tripped for {filepath} after "
                f"{len(records)} attempts. Changes auto-rolled back. "
                f"Consider reviewing the logic manually or delegating "
                f"to a higher-reasoning model."
            ),
        }

        self._tripped[filepath] = report
        return report

    def _is_tripped(self, filepath: str) -> bool:
        return filepath in self._tripped

    def get_trip_reports(self) -> Dict:
        """Get all circuit breaker trip reports."""
        return {"tripped_files": list(self._tripped.keys()), "reports": self._tripped}

    def reset_file(self, filepath: str):
        """Reset tracking for a file."""
        self._tracker.clear_file(filepath)
        self._tripped.pop(filepath, None)

    def reset_all(self):
        """Reset all tracking."""
        self._tracker.reset()
        self._tripped.clear()

    def get_status(self) -> Dict:
        """Get circuit breaker status summary."""
        tracked_files = {}
        for fp, records in self._tracker._records.items():
            tracked_files[fp] = {
                "strikes": len(records),
                "tripped": fp in self._tripped,
            }
        return {
            "tracked_files": tracked_files,
            "tripped_files": list(self._tripped.keys()),
            "max_strikes": self._max_strikes,
            "rollback_enabled": self._enable_rollback,
        }


# ── Tool definitions ──────────────────────────────────────────────────

_breaker: CircuitBreaker = None


def get_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker()
    return _breaker


def create_circuit_tools(breaker: CircuitBreaker) -> List[Dict]:
    return [
        {
            "name": "coding_circuit_before_edit",
            "description": "Check circuit breaker status before editing a file. Returns remaining attempts before auto-rollback triggers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File to check",
                    }
                },
                "required": ["filepath"],
            },
            "handler": breaker.before_edit,
            "category": "code_repair",
        },
        {
            "name": "coding_circuit_after_edit",
            "description": "Report the outcome of an edit attempt. Tracks failures and auto-trips circuit breaker after 3 strikes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Edited file path"},
                    "success": {
                        "type": "boolean",
                        "description": "Whether the edit was successful",
                    },
                    "error_text": {
                        "type": "string",
                        "description": "Error output if failed",
                    },
                    "diff": {
                        "type": "string",
                        "description": "Diff of changes made",
                    },
                    "run_tests": {
                        "type": "boolean",
                        "description": "Run tests after edit",
                        "default": False,
                    },
                },
                "required": ["filepath", "success"],
            },
            "handler": lambda filepath, success, error_text="", diff="", run_tests=False: breaker.after_edit(
                filepath, success, error_text, diff, run_tests
            ),
            "category": "code_repair",
        },
        {
            "name": "coding_circuit_run_tests",
            "description": "Run project tests and parse structured results. Supports pytest with JUnit XML diagnostics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_path": {
                        "type": "string",
                        "description": "Specific test path (optional)",
                    },
                    "extra_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra pytest arguments",
                    },
                },
            },
            "handler": lambda test_path=None, extra_args=None: breaker.run_tests(test_path),
            "category": "code_repair",
        },
        {
            "name": "coding_circuit_status",
            "description": "Get circuit breaker status: tracked files, strike counts, tripped files.",
            "parameters": {"type": "object", "properties": {}},
            "handler": breaker.get_status,
            "category": "code_repair",
        },
        {
            "name": "coding_circuit_reset",
            "description": "Reset circuit breaker tracking for a file or all files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File to reset (omit to reset all)",
                    }
                },
            },
            "handler": lambda filepath=None: (
                breaker.reset_all() if filepath is None else breaker.reset_file(filepath),
                {"status": "reset", "filepath": filepath or "all"},
            )[1],
            "category": "code_repair",
        },
        {
            "name": "coding_circuit_reports",
            "description": "Get all circuit breaker trip reports with error fingerprints and repair history.",
            "parameters": {"type": "object", "properties": {}},
            "handler": breaker.get_trip_reports,
            "category": "code_repair",
        },
        {
            "name": "coding_circuit_error_fingerprint",
            "description": "Generate a stable fingerprint from an error message. Useful for detecting repeated errors across different runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "error_text": {
                        "type": "string",
                        "description": "Error text to fingerprint",
                    }
                },
                "required": ["error_text"],
            },
            "handler": lambda error_text: {
                "fingerprint": ErrorFingerprint.fingerprint(error_text),
                "key_lines": ErrorFingerprint.extract_key_lines(error_text)[:5],
            },
            "category": "code_repair",
        },
    ]


def get_circuit_tools() -> List[Dict]:
    return create_circuit_tools(get_breaker())
