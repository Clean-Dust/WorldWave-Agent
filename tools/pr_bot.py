"""GitHub PR Bot tools — autonomous PR review via API polling."""

from __future__ import annotations
import os
from typing import Optional

from tools.registry import ToolRegistry, ToolDef


_pr_bot = None


def _get_bot():
    global _pr_bot
    if _pr_bot is None:
        from core.github_pr_bot import PRBot, create_pr_bot_from_env
        _pr_bot = create_pr_bot_from_env()
        if _pr_bot is None:
            token = os.environ.get("GITHUB_TOKEN", os.environ.get("WW_GITHUB_TOKEN", ""))
            if token:
                _pr_bot = PRBot(token=token)
    return _pr_bot


def register_tools(registry: ToolRegistry):

    def handle_pr_bot_poll(repo: str = "", **kwargs) -> dict:
        """Poll open PRs and review any new ones."""
        bot = _get_bot()
        if bot is None:
            return {"error": "No GitHub token configured. Set GITHUB_TOKEN or WW_GITHUB_TOKEN."}

        if repo:
            bot.add_repo(repo)

        if not bot._repos:
            return {"error": "No repos configured. Pass repo='owner/name' or set WW_PR_BOT_REPOS."}

        results = bot.poll_once()
        return {
            "reviewed": len(results),
            "results": [
                {
                    "repo": r.repo,
                    "pr": r.pr_number,
                    "passed": r.passed,
                    "summary": r.summary[:300],
                    "diff_summary": r.diff_summary,
                    "suggestions": r.suggestions,
                    "duration": r.duration,
                }
                for r in results
            ],
        }

    def handle_pr_bot_add_repo(repo: str, **kwargs) -> dict:
        """Add a repo to PR bot tracking."""
        bot = _get_bot()
        if bot is None:
            return {"error": "No GitHub token configured."}
        bot.add_repo(repo)
        return {"added": repo, "tracking": bot._repos}

    def handle_pr_bot_list(**kwargs) -> dict:
        """List tracked repos and recently seen PRs."""
        bot = _get_bot()
        if bot is None:
            return {"error": "No GitHub token configured."}
        return {
            "repos": bot._repos,
            "seen_prs": list(bot._seen.keys()),
            "poll_interval": bot._poll_interval,
        }

    def handle_pr_bot_review(owner: str, repo: str, pr_number: int, **kwargs) -> dict:
        """Manually trigger review for a specific PR."""
        bot = _get_bot()
        if bot is None:
            return {"error": "No GitHub token configured."}
        from core.github_pr_bot import PRInfo
        from datetime import datetime, timezone

        full_repo = f"{owner}/{repo}"
        # Fetch PR details
        data = bot._api_get(f"/repos/{full_repo}/pulls/{pr_number}")
        pr = PRInfo(
            number=pr_number,
            title=data.get("title", ""),
            body=data.get("body", ""),
            state=data.get("state", "open"),
            html_url=data.get("html_url", ""),
            head_sha=data["head"]["sha"],
            head_ref=data["head"]["ref"],
            head_repo_full=data["head"]["repo"]["full_name"],
            base_ref=data["base"]["ref"],
            base_repo_full=data["base"]["repo"]["full_name"],
            user_login=data["user"]["login"],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            draft=data.get("draft", False),
        )
        result = bot._review_pr(full_repo, pr)
        return {
            "repo": result.repo,
            "pr": result.pr_number,
            "passed": result.passed,
            "diff_summary": result.diff_summary,
            "suggestions": result.suggestions,
            "duration": result.duration,
        }

    registry.register(ToolDef(
        name="pr_bot_poll",
        description="Poll tracked GitHub repos for open PRs and auto-review new ones.",
        handler=handle_pr_bot_poll,
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Optional repo to add and poll (owner/name).", "default": ""},
            },
            "required": [],
        },
        category="git",
    ))

    registry.register(ToolDef(
        name="pr_bot_add_repo",
        description="Add a GitHub repo to PR bot tracking.",
        handler=handle_pr_bot_add_repo,
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo in owner/name format."},
            },
            "required": ["repo"],
        },
        category="git",
    ))

    registry.register(ToolDef(
        name="pr_bot_list",
        description="List PR bot tracked repos and recently seen PRs.",
        handler=handle_pr_bot_list,
        parameters={"type": "object", "properties": {}, "required": []},
        category="git",
    ))

    registry.register(ToolDef(
        name="pr_bot_review",
        description="Manually review a specific GitHub PR (clone, test, analyze).",
        handler=handle_pr_bot_review,
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner."},
                "repo": {"type": "string", "description": "Repo name."},
                "pr_number": {"type": "integer", "description": "PR number."},
            },
            "required": ["owner", "repo", "pr_number"],
        },
        category="git",
    ))
