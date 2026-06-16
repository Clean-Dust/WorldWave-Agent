"""ww/pm/planning.py — Codex-inspired planning & AGENTS.md system v0.1

Implements Gemini's WW-PM Subsystem 4:
- AGENTS.md / AGENTS.override.md recursive loading
- ExecPlans / PLANS.md generation and enforcement
- Task decomposition into mini-tickets

Architecture:
  AgentConfig — loads and merges AGENTS.md from project root downward
  ExecPlan    — structured execution plan with ticket decomposition
  PlanManager — coordinates planning lifecycle
"""

from __future__ import annotations
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


# ── AGENTS.md Loader ──────────────────────────────────────────────────

DEFAULT_AGENTS_MD = """# AGENTS.md — WorldWave Project Rules

## Style
- Follow existing code patterns in the project
- Use meaningful names over comments
- Keep functions focused and small

## Testing
- Run tests before submitting changes
- Add tests for new functionality
- Ensure all existing tests pass

## Architecture
- Prefer composition over inheritance
- Keep modules loosely coupled
- Follow separation of concerns
"""


class AgentConfig:
    """Load and merge AGENTS.md recursively from project root.

    Loading order (root->subdir):
    1. <project_root>/AGENTS.md — global rules
    2. <subdir>/AGENTS.override.md — per-directory overrides
    3. Merged with later files overriding earlier ones
    """

    def __init__(self, project_root: str = None):
        self._project_root = self._find_project_root(project_root)
        self._rules: Dict[str, str] = {}
        self._loaded_files: List[str] = []

    @property
    def project_root(self) -> str:
        return self._project_root

    @property
    def loaded_files(self) -> List[str]:
        return list(self._loaded_files)

    def load_global(self) -> str:
        """Load and return AGENTS.md content from project root."""
        path = os.path.join(self._project_root, "AGENTS.md")
        return self._load_file(path)

    def load_for_directory(self, directory: str) -> str:
        """Load rules for a specific directory (global + override)."""
        directory = os.path.abspath(directory)
        parts = []

        # Load global
        global_path = os.path.join(self._project_root, "AGENTS.md")
        if os.path.isfile(global_path):
            content = self._load_file(global_path)
            if content:
                parts.append(("global", content))

        # Load override chain from project root to target directory
        rel_path = os.path.relpath(directory, self._project_root)
        if rel_path != ".":
            path_parts = rel_path.split(os.sep)
            cumulative = self._project_root
            for part in path_parts:
                cumulative = os.path.join(cumulative, part)
                override_path = os.path.join(cumulative, "AGENTS.override.md")
                if os.path.isfile(override_path):
                    content = self._load_file(override_path)
                    if content:
                        parts.append((os.path.relpath(override_path, self._project_root), content))

        return "\n\n".join(content for _, content in parts) if parts else DEFAULT_AGENTS_MD

    def get_merged_rules(self) -> Dict[str, str]:
        """Get merged rules as structured sections."""
        return dict(self._rules)

    def _find_project_root(self, start: str = None) -> str:
        """Walk up to find project root (contains .git or AGENTS.md)."""
        path = start or os.getcwd()
        path = os.path.abspath(path)

        # Check current and parent directories
        for _ in range(10):  # Max depth
            if os.path.isdir(os.path.join(path, ".git")):
                return path
            if os.path.isfile(os.path.join(path, "AGENTS.md")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent

        return os.path.abspath(start or os.getcwd())

    def _load_file(self, path: str) -> str:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if path not in self._loaded_files:
                self._loaded_files.append(path)
            self._parse_rules(content)
            return content
        return ""

    def _parse_rules(self, content: str):
        """Extract section-based rules from markdown content."""
        current_section = "general"
        for line in content.split("\n"):
            if line.startswith("## "):
                current_section = line.strip("# ").strip().lower()
                self._rules[current_section] = ""
            elif current_section not in self._rules:
                self._rules[current_section] = line
            else:
                self._rules[current_section] += line + "\n"


# ── ExecPlan / PLANS.md ───────────────────────────────────────────────

class ExecTicket:
    """A single decomposable work unit (~5-10 min of work)."""

    def __init__(
        self,
        title: str,
        description: str = "",
        files: List[str] = None,
        depends_on: List[str] = None,
    ):
        self.id = f"ticket_{uuid.uuid4().hex[:6]}"
        self.title = title
        self.description = description
        self.files = files or []
        self.depends_on = depends_on or []
        self.status = "pending"  # pending | running | done | blocked | failed

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "files": self.files,
            "depends_on": self.depends_on,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ExecTicket":
        t = cls(data["title"], data.get("description", ""), data.get("files"), data.get("depends_on"))
        t.id = data.get("id", t.id)
        t.status = data.get("status", "pending")
        return t


class ExecPlan:
    """Structured execution plan with ticket decomposition."""

    def __init__(
        self,
        title: str,
        goal: str = "",
        tickets: List[ExecTicket] = None,
    ):
        self.id = f"plan_{uuid.uuid4().hex[:8]}"
        self.title = title
        self.goal = goal
        self.tickets = tickets or []
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at

    def add_ticket(self, ticket: ExecTicket):
        self.tickets.append(ticket)
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def next_ticket(self) -> Optional[ExecTicket]:
        """Get the next ticket whose dependencies are all done."""
        done_ids = {t.id for t in self.tickets if t.status == "done"}

        for ticket in self.tickets:
            if ticket.status != "pending":
                continue
            if all(dep in done_ids for dep in ticket.depends_on):
                return ticket

        # Fallback: first pending ticket
        for ticket in self.tickets:
            if ticket.status == "pending":
                return ticket

        return None

    def mark_done(self, ticket_id: str):
        for ticket in self.tickets:
            if ticket.id == ticket_id:
                ticket.status = "done"
                break
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_failed(self, ticket_id: str, reason: str = ""):
        for ticket in self.tickets:
            if ticket.id == ticket_id:
                ticket.status = "failed"
                break
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def is_complete(self) -> bool:
        return all(t.status == "done" for t in self.tickets) if self.tickets else False

    def to_plans_md(self) -> str:
        """Generate PLANS.md format markdown."""
        lines = [
            f"# Execution Plan: {self.title}",
            "",
            f"**Goal:** {self.goal}",
            f"**Created:** {self.created_at}",
            f"**Progress:** {sum(1 for t in self.tickets if t.status == 'done')}/{len(self.tickets)} tickets complete",
            "",
            "## Tickets",
            "",
        ]

        for ticket in self.tickets:
            icons = {"done": "[✓]", "running": "[▶]", "blocked": "[!]", "failed": "[✗]", "pending": "[ ]"}
            icon = icons.get(ticket.status, "[ ]")
            lines.append(f"### {icon} {ticket.title} (`{ticket.id}`)")
            if ticket.description:
                lines.append("")
                lines.append(ticket.description)
            if ticket.files:
                lines.append("")
                lines.append(f"Files: {', '.join(ticket.files)}")
            if ticket.depends_on:
                lines.append(f"Dependencies: {', '.join(ticket.depends_on)}")
            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "tickets": [t.to_dict() for t in self.tickets],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "complete": self.is_complete(),
            "progress": f"{sum(1 for t in self.tickets if t.status == 'done')}/{len(self.tickets)}",
        }


class PlanManager:
    """Coordinates the planning lifecycle.

    Manages active plans, persists to PLANS.md, and tracks progress.
    """

    def __init__(self, project_root: str = None):
        self._project_root = project_root or os.getcwd()
        self._active_plan: Optional[ExecPlan] = None
        self._plans: Dict[str, ExecPlan] = {}

    @property
    def active_plan(self) -> Optional[ExecPlan]:
        return self._active_plan

    def create_plan(self, title: str, goal: str = "", tickets: List[ExecTicket] = None) -> ExecPlan:
        """Create a new execution plan."""
        plan = ExecPlan(title=title, goal=goal, tickets=tickets)
        self._plans[plan.id] = plan
        self._active_plan = plan
        return plan

    def load_plans_md(self, path: str = None) -> Optional[ExecPlan]:
        """Load execution plan from a PLANS.md file."""
        path = path or os.path.join(self._project_root, "PLANS.md")
        if not os.path.isfile(path):
            return None

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Basic parsing
        title_match = re.search(r"# Execution Plan: (.+)", content)
        goal_match = re.search(r"\*\*Goal:\*\* (.+)", content)

        title = title_match.group(1) if title_match else "Loaded Plan"
        goal = goal_match.group(1) if goal_match else ""

        tickets = []
        ticket_sections = re.split(r"^### ", content, flags=re.MULTILINE)[1:]

        for section in ticket_sections:
            lines = section.strip().split("\n")
            header = lines[0] if lines else ""
            # Extract title from "[ ] title (id)"
            title_match = re.match(r"\[.\] (.+?) \(`(.+?)`\)", header)
            if title_match:
                t = ExecTicket(title=title_match.group(1))
                t.id = title_match.group(2)

                # Parse description
                desc_lines = []
                in_desc = False
                for line in lines[1:]:
                    if line.startswith("Files:") or line.startswith("Dependencies:"):
                        break
                    if not line.startswith("---") and line.strip():
                        desc_lines.append(line)

                t.description = "\n".join(desc_lines).strip()

                # Parse files
                files_match = re.search(r"Files: (.+)", section)
                if files_match:
                    t.files = [f.strip() for f in files_match.group(1).split(",")]

                # Parse dependencies
                deps_match = re.search(r"Dependencies: (.+)", section)
                if deps_match:
                    t.depends_on = [d.strip() for d in deps_match.group(1).split(",")]

                # Detect status from icon
                if header.startswith("[✓]"):
                    t.status = "done"
                elif header.startswith("[▶]"):
                    t.status = "running"
                elif header.startswith("[!]"):
                    t.status = "blocked"
                elif header.startswith("[✗]"):
                    t.status = "failed"

                tickets.append(t)

        plan = ExecPlan(title=title, goal=goal, tickets=tickets)
        self._plans[plan.id] = plan
        self._active_plan = plan
        return plan

    def save_plans_md(self, plan: ExecPlan = None, path: str = None) -> Dict:
        """Save execution plan to PLANS.md."""
        plan = plan or self._active_plan
        if plan is None:
            return {"error": "No plan to save"}

        path = path or os.path.join(self._project_root, "PLANS.md")
        content = plan.to_plans_md()

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return {"success": True, "path": path, "tickets": len(plan.tickets)}

    def get_status(self) -> Dict:
        """Get current planning status."""
        active = self._active_plan
        if active is None:
            return {"active_plan": None, "total_plans": len(self._plans)}

        return {
            "active_plan": active.to_dict(),
            "total_plans": len(self._plans),
        }


# ── Tool definitions ──────────────────────────────────────────────────

_config: AgentConfig = None
_manager: PlanManager = None


def get_config() -> AgentConfig:
    global _config
    if _config is None:
        _config = AgentConfig()
    return _config


def get_manager() -> PlanManager:
    global _manager
    if _manager is None:
        _manager = PlanManager()
    return _manager


def create_planning_tools(config: AgentConfig, manager: PlanManager) -> List[Dict]:
    return [
        {
            "name": "coding_load_agents_md",
            "description": "Load project rules from AGENTS.md. Searches from project root downward, merging overrides per directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Target directory to load rules for (optional, defaults to project root)",
                    }
                },
            },
            "handler": lambda directory=None: {
                "content": config.load_for_directory(directory or config.project_root),
                "project_root": config.project_root,
                "loaded_files": config.loaded_files,
            },
            "category": "code_planning",
        },
        {
            "name": "coding_create_plan",
            "description": "Create a structured execution plan with ticket decomposition. Must be called before complex multi-file tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Plan title",
                    },
                    "goal": {
                        "type": "string",
                        "description": "Overall goal of the execution",
                    },
                    "tickets": {
                        "type": "array",
                        "description": "List of tickets (work units, ~5-10 min each)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Ticket title"},
                                "description": {"type": "string", "description": "What to do"},
                                "files": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Files this ticket affects",
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Ticket IDs this depends on",
                                },
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["title", "tickets"],
            },
            "handler": lambda title, goal="", tickets=None: manager.create_plan(
                title, goal,
                [ExecTicket(**t) for t in (tickets or [])]
            ).to_dict(),
            "category": "code_planning",
        },
        {
            "name": "coding_next_ticket",
            "description": "Get the next pending ticket whose dependencies are satisfied.",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: manager.active_plan.next_ticket().to_dict() if manager.active_plan and manager.active_plan.next_ticket() else {"error": "No pending tickets"},
            "category": "code_planning",
        },
        {
            "name": "coding_mark_ticket_done",
            "description": "Mark a ticket as completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "Ticket ID to mark done",
                    }
                },
                "required": ["ticket_id"],
            },
            "handler": lambda ticket_id: (
                manager.active_plan.mark_done(ticket_id),
                manager.save_plans_md(),
                {"success": True, "ticket_id": ticket_id},
            )[2],
            "category": "code_planning",
        },
        {
            "name": "coding_save_plan",
            "description": "Save the current execution plan to PLANS.md.",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: manager.save_plans_md(),
            "category": "code_planning",
        },
        {
            "name": "coding_plan_status",
            "description": "Get the current execution plan status.",
            "parameters": {"type": "object", "properties": {}},
            "handler": manager.get_status,
            "category": "code_planning",
        },
    ]


def get_planning_tools() -> List[Dict]:
    return create_planning_tools(get_config(), get_manager())
