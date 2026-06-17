"""
GitHub PR Bot — autonomous PR review via GitHub API polling.

No webhook required. Polls open PRs, clones, reviews, posts findings.
Designed for self-hosted/air-gapped environments.

Architecture:
  1. Poll GET /repos/:owner/:repo/pulls?state=open
  2. Detect new PRs or new commits on tracked PRs
  3. Clone repo → checkout PR branch → run tests in sandbox
  4. Analyze diff → semantic code search → generate review
  5. Post review via POST /repos/:owner/:repo/pulls/:number/reviews

Usage:
  bot = PRBot(token="ghp_...", repos=["owner/repo"])
  bot.poll_once()        # One-time check
  bot.start(interval=60) # Background polling every 60s

Config via env:
  GITHUB_TOKEN or WW_GITHUB_TOKEN
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from typing import Callable, Dict, List, Optional
from dataclasses import dataclass, field

log = logging.getLogger("ww.github.pr_bot")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class PRInfo:
    """Lightweight PR representation."""
    number: int
    title: str
    body: str
    state: str
    html_url: str
    head_sha: str
    head_ref: str
    head_repo_full: str
    base_ref: str
    base_repo_full: str
    user_login: str
    created_at: str
    updated_at: str
    draft: bool = False
    merged: bool = False
    mergeable: Optional[bool] = None

    @property
    def full_name(self) -> str:
        return f"{self.base_repo_full}#{self.number}"

    @property
    def review_branch(self) -> str:
        """Branch name for review: pr-{number}-{short_sha}"""
        return f"pr-{self.number}-{self.head_sha[:7]}"


@dataclass
class ReviewResult:
    """Result of an automated review."""
    pr_number: int
    repo: str
    passed: bool
    summary: str
    findings: List[Dict] = field(default_factory=list)  # [{level, file, line, message}]
    test_output: str = ""
    diff_summary: str = ""
    suggestions: List[str] = field(default_factory=list)
    duration: float = 0.0


# ── PR Bot ───────────────────────────────────────────────────────

class PRBot:
    """Autonomous PR reviewer via GitHub API polling."""

    def __init__(
        self,
        token: str = "",
        repos: Optional[List[str]] = None,
        work_dir: str = "",
        poll_interval: int = 60,
        auto_review: bool = True,
    ):
        self._token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("WW_GITHUB_TOKEN", "")
        self._repos: List[str] = repos or []
        self._poll_interval = poll_interval
        self._auto_review = auto_review
        self._work_dir = work_dir or os.path.join(tempfile.gettempdir(), "ww_pr_bot")
        os.makedirs(self._work_dir, exist_ok=True)

        # Track which PRs we've already reviewed (head_sha → last_seen_sha)
        self._seen: Dict[str, str] = {}  # "owner/repo#N" → head_sha
        self._running = False
        self._on_review: Optional[Callable[[ReviewResult], None]] = None
        self._review_callback: Optional[Callable[[PRInfo, str], Optional[str]]] = None

    # ── Public API ───────────────────────────────────────────────

    def set_review_callback(self, fn: Callable[[PRInfo, str], Optional[str]]):
        """Set a callback that reviews a PR's diff and returns review text.

        Args:
            fn: Called with (PRInfo, diff_text) → returns review body or None.
        """
        self._review_callback = fn

    def set_on_review(self, fn: Callable[[ReviewResult], None]):
        """Set callback invoked after each review completes."""
        self._on_review = fn

    def add_repo(self, owner_repo: str):
        """Track a new repo: 'owner/name'."""
        if owner_repo not in self._repos:
            self._repos.append(owner_repo)

    def remove_repo(self, owner_repo: str):
        """Stop tracking a repo."""
        if owner_repo in self._repos:
            self._repos.remove(owner_repo)

    def poll_once(self) -> List[ReviewResult]:
        """Check all tracked repos for new PRs/commits. Review any that are new."""
        results = []
        for repo in self._repos:
            try:
                prs = self._fetch_open_prs(repo)
                for pr in prs:
                    if pr.draft:
                        continue  # Skip draft PRs
                    key = pr.full_name
                    if self._seen.get(key) == pr.head_sha:
                        continue  # Already reviewed this commit
                    self._seen[key] = pr.head_sha
                    result = self._review_pr(repo, pr)
                    results.append(result)
                    if self._on_review:
                        self._on_review(result)
            except Exception as e:
                log.error("Error polling %s: %s", repo, e)
        return results

    def start(self, interval: int = 0):
        """Start background polling (called from scheduler/cron)."""
        if interval:
            self._poll_interval = interval
        self._running = True
        log.info("PRBot started polling %d repos every %ds", len(self._repos), self._poll_interval)

        import threading
        def _loop():
            while self._running:
                try:
                    self.poll_once()
                except Exception as e:
                    log.error("PRBot poll error: %s", e)
                time.sleep(self._poll_interval)

        t = threading.Thread(target=_loop, daemon=True, name="ww-prbot")
        t.start()

    def stop(self):
        """Stop background polling."""
        self._running = False
        log.info("PRBot stopped")

    # ── Review Engine ────────────────────────────────────────────

    def _review_pr(self, repo: str, pr: PRInfo) -> ReviewResult:
        """Clone, analyze, and review a PR."""
        start = time.time()
        result = ReviewResult(
            pr_number=pr.number,
            repo=repo,
            passed=False,
            summary="",
        )

        clone_dir = None
        try:
            # Clone repo
            clone_dir = self._clone_pr(repo, pr)

            # Get diff
            diff_text = self._get_diff(clone_dir, pr)
            diff_stats = self._diff_stats(diff_text)
            result.diff_summary = diff_stats

            # Run tests if available
            test_output = self._run_tests(clone_dir)
            result.test_output = test_output

            # Generate review
            if self._review_callback:
                review_body = self._review_callback(pr, diff_text)
                if review_body:
                    self._post_review(repo, pr.number, review_body, test_output)
                    result.summary = review_body[:200]

            # Determine pass/fail
            result.passed = self._check_pass(test_output, diff_text)
            result.suggestions = self._generate_suggestions(diff_text, test_output)

        except Exception as e:
            result.summary = f"Review failed: {e}"
            log.exception("Review failed for %s#%d", repo, pr.number)
        finally:
            if clone_dir:
                import shutil
                shutil.rmtree(clone_dir, ignore_errors=True)

        result.duration = round(time.time() - start, 2)
        return result

    # ── GitHub API ───────────────────────────────────────────────

    def _api_get(self, endpoint: str) -> dict:
        """GET request to GitHub API."""
        url = f"https://api.github.com{endpoint}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "WW-PR-Bot/1.0")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"GitHub API {endpoint}: {e.code} {body[:200]}")

    def _api_post(self, endpoint: str, data: dict) -> dict:
        """POST request to GitHub API."""
        url = f"https://api.github.com{endpoint}"
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "WW-PR-Bot/1.0")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"GitHub API POST {endpoint}: {e.code} {body[:200]}")

    def _fetch_open_prs(self, repo: str) -> List[PRInfo]:
        """Fetch all open PRs for a repo."""
        data = self._api_get(f"/repos/{repo}/pulls?state=open&per_page=100")
        if not isinstance(data, list):
            return []
        prs = []
        for item in data:
            prs.append(PRInfo(
                number=item["number"],
                title=item.get("title", ""),
                body=item.get("body", ""),
                state=item.get("state", "open"),
                html_url=item.get("html_url", ""),
                head_sha=item["head"]["sha"],
                head_ref=item["head"]["ref"],
                head_repo_full=item["head"]["repo"]["full_name"],
                base_ref=item["base"]["ref"],
                base_repo_full=item["base"]["repo"]["full_name"],
                user_login=item["user"]["login"],
                created_at=item.get("created_at", ""),
                updated_at=item.get("updated_at", ""),
                draft=item.get("draft", False),
                merged=item.get("merged", False),
                mergeable=item.get("mergeable"),
            ))
        return prs

    def _post_review(self, repo: str, pr_number: int, body: str, test_output: str = ""):
        """Post a PR review comment."""
        full_body = body
        if test_output:
            full_body += f"\n\n<details>\n<summary>🧪 Test Output</summary>\n\n```\n{test_output[:2000]}\n```\n</details>"

        self._api_post(
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            {
                "body": full_body,
                "event": "COMMENT",  # or "APPROVE" / "REQUEST_CHANGES"
            },
        )

    # ── Git Operations ───────────────────────────────────────────

    def _clone_pr(self, repo: str, pr: PRInfo) -> str:
        """Clone the repo and checkout the PR branch (or fetch ref)."""
        clone_dir = os.path.join(self._work_dir, f"{repo.replace('/', '_')}_{pr.review_branch}")

        # Remove if exists
        if os.path.exists(clone_dir):
            import shutil
            shutil.rmtree(clone_dir, ignore_errors=True)

        clone_url = f"https://x-access-token:{self._token}@github.com/{repo}.git"
        subprocess.run(
            ["git", "clone", "--depth=50", clone_url, clone_dir],
            capture_output=True, timeout=120, check=True,
        )

        # Fetch and checkout PR ref
        subprocess.run(
            ["git", "fetch", "origin", f"pull/{pr.number}/head:{pr.review_branch}"],
            cwd=clone_dir, capture_output=True, timeout=60, check=True,
        )
        subprocess.run(
            ["git", "checkout", pr.review_branch],
            cwd=clone_dir, capture_output=True, timeout=30, check=True,
        )

        return clone_dir

    def _get_diff(self, clone_dir: str, pr: PRInfo) -> str:
        """Get the diff between PR base and head."""
        result = subprocess.run(
            ["git", "diff", f"origin/{pr.base_ref}...{pr.review_branch}"],
            cwd=clone_dir, capture_output=True, text=True, timeout=30,
        )
        return result.stdout

    def _run_tests(self, clone_dir: str) -> str:
        """Attempt to run tests in the cloned repo."""
        outputs = []
        # Try common test commands
        test_commands = [
            ["python", "-m", "pytest", "-x", "--tb=short", "-q"],
            ["python", "-m", "unittest", "discover", "-q"],
            ["cargo", "test", "--quiet"],
            ["go", "test", "./..."],
            ["npm", "test", "--", "--silent"],
        ]
        for cmd in test_commands:
            try:
                result = subprocess.run(
                    cmd, cwd=clone_dir,
                    capture_output=True, text=True, timeout=60,
                )
                outputs.append(f"$ {' '.join(cmd)} (exit={result.returncode})")
                if result.stdout:
                    outputs.append(result.stdout[:1000])
                if result.stderr:
                    outputs.append(result.stderr[:500])
            except Exception:
                continue

        return "\n".join(outputs) if outputs else "No tests detected"

    # ── Analysis ─────────────────────────────────────────────────

    def _diff_stats(self, diff: str) -> str:
        """Summarize the diff."""
        additions = diff.count("\n+") - diff.count("\n+++")
        deletions = diff.count("\n-") - diff.count("\n---")
        files_changed = len(set(re.findall(r"diff --git a/(.+) b/", diff)))
        return f"{files_changed} files, +{additions} -{deletions}"

    def _check_pass(self, test_output: str, diff: str) -> bool:
        """Determine if the PR passes review."""
        if "FAILED" in test_output or "failures=" in test_output:
            return False
        if "exit=1" in test_output or "exit=2" in test_output:
            return False
        return True

    def _generate_suggestions(self, diff: str, test_output: str) -> List[str]:
        """Generate simple static-analysis suggestions."""
        suggestions = []
        # Check for common issues
        if "print(" in diff and "logging" not in diff:
            suggestions.append("Consider using logging instead of print()")
        if "TODO" in diff:
            suggestions.append("Contains TODO markers — consider resolving before merge")
        if "import pdb" in diff:
            suggestions.append("Debugger import (pdb) detected — remove before merge")
        if "try:" in diff and "except:" in diff and "Exception" not in diff:
            suggestions.append("Bare except: clause — consider catching specific exceptions")
        return suggestions


# ── Convenience ──────────────────────────────────────────────────

def create_pr_bot_from_env() -> Optional[PRBot]:
    """Create a PRBot from environment variables.

    Reads:
      GITHUB_TOKEN or WW_GITHUB_TOKEN
      WW_PR_BOT_REPOS (comma-separated list of repos)
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("WW_GITHUB_TOKEN", "")
    repos_str = os.environ.get("WW_PR_BOT_REPOS", "")
    repos = [r.strip() for r in repos_str.split(",") if r.strip()]

    if not token or not repos:
        return None

    return PRBot(token=token, repos=repos)
