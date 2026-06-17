"""ww/pm/allure.py — Allure test report parser for structured diagnostics v0.1

Implements Gemini's requirement: parse Allure structured test reports
including failure messages, step logs, and crash screenshots.

Allure produces JSON result files in allure-results/ directory.
This parser reads them and returns structured diagnostics.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Dict, List


class AllureParser:
    """Parse Allure test results from allure-results/ directory."""

    def __init__(self, results_dir: str = "allure-results"):
        self._results_dir = results_dir

    def parse(self, results_dir: str = None) -> Dict:
        """Parse all Allure result files in the directory."""
        directory = results_dir or self._results_dir
        if not os.path.isdir(directory):
            return {"error": f"Allure results directory not found: {directory}"}

        tests = []
        total = 0
        passed = 0
        failed = 0
        broken = 0
        screenshots = []

        for f in sorted(Path(directory).glob("*-result.json")):
            total += 1
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, IOError):
                continue

            name = data.get("name", f.stem)
            status = data.get("status", "unknown")
            full_name = data.get("fullName", name)

            test = {
                "name": name,
                "full_name": full_name,
                "status": status,
                "start": data.get("start"),
                "stop": data.get("stop"),
                "labels": [l.get("value", "") for l in data.get("labels", [])],
                "parameters": data.get("parameters", []),
            }

            if status == "passed":
                passed += 1
            elif status == "failed":
                failed += 1
                # Extract failure message
                status_details = data.get("statusDetails", {})
                test["message"] = status_details.get("message", "")
                test["trace"] = (status_details.get("trace", "") or "")[:500]
            elif status == "broken":
                broken += 1

            # Extract attachments (screenshots, logs)
            attachments = []
            for att in data.get("attachments", []):
                att_name = att.get("name", "")
                att_type = att.get("type", "")
                source = att.get("source", "")
                att_path = os.path.join(directory, source) if source else ""
                attachments.append({
                    "name": att_name,
                    "type": att_type,
                    "source": att_path,
                    "exists": os.path.isfile(att_path) if att_path else False,
                })
                if att_type and "image" in att_type.lower():
                    screenshots.append(att_path)

            if attachments:
                test["attachments"] = attachments

            tests.append(test)

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "broken": broken,
            "tests": tests,
            "screenshots": screenshots,
            "results_dir": directory,
        }

    def get_failed_tests(self, results_dir: str = None) -> Dict:
        """Get only failed/broken tests with diagnostic info."""
        result = self.parse(results_dir)
        failed_tests = [t for t in result.get("tests", [])
                        if t.get("status") in ("failed", "broken")]
        return {
            "total_failed": len(failed_tests),
            "total_passed": result.get("passed", 0),
            "tests": failed_tests,
            "screenshots": result.get("screenshots", []),
        }


def create_allure_tools() -> List[Dict]:
    parser = AllureParser()
    return [
        {
            "name": "coding_allure_parse",
            "description": "Parse Allure test results from allure-results/ directory. Returns structured diagnostics including failure messages, stack traces, and attachment paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "results_dir": {
                        "type": "string",
                        "description": "Path to allure-results directory",
                        "default": "allure-results",
                    }
                },
            },
            "handler": lambda results_dir="allure-results": parser.parse(results_dir),
            "category": "code_repair",
        },
        {
            "name": "coding_allure_failures",
            "description": "Get only failed/broken tests from Allure results with full diagnostic info (message, trace, screenshots).",
            "parameters": {
                "type": "object",
                "properties": {
                    "results_dir": {
                        "type": "string",
                        "description": "Path to allure-results directory",
                        "default": "allure-results",
                    }
                },
            },
            "handler": lambda results_dir="allure-results": parser.get_failed_tests(results_dir),
            "category": "code_repair",
        },
    ]


def get_allure_tools() -> List[Dict]:
    return create_allure_tools()
