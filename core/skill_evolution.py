"""
core/skill_evolution.py — Autonomous Skill Creation & Procedural Memory Evolution

Extracts reusable skills from successful task executions.
Hooks into the LEARN phase of the spiral loop to:
1. Detect repeatable patterns from task success
2. Extract structured procedure (trigger → steps → pitfalls)
3. Persist as Skill with version tracking
4. Auto-improve existing skills when better patterns emerge

This is the "closed learning loop" — the agent grows more efficient over time.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from core.config import ConfigManager
    from core.llm import LLMClient

log = logging.getLogger("ww.skill_evolution")

# ── Configuration defaults ──

MIN_TASK_STEPS = 3
MIN_SPIRALS = 2
MIN_SUCCESS_RATE = 0.8

# ── Prompt templates (kept as constants for easy iteration) ──

DISTILL_PROMPT_TEMPLATE = """\
You are a skill distillation engine. Given repeated successful task patterns, \
extract a reusable SKILL.md. Output YAML frontmatter + markdown body.

**Trigger keywords:** {trigger_keywords}
**Tool sequence pattern:** {tool_sequence}
**Success rate:** {success_count}/{total_count} ({success_rate:.0%})
**Average spirals:** {avg_spirals:.1f}
**Common errors:** {common_errors}

**Example tasks:**
{examples}

Output format:
```yaml
---
name: <kebab-case-name>
description: <one-line>
trigger: <when-to-use>
category: <general|devops|coding|research|data>
version: 1
---

## Steps
1. ...
2. ...

## Pitfalls
- ...

## Notes
Auto-generated from {success_count} successful executions.
```
Return ONLY the skill file content, no extra text."""

UPDATE_PROMPT_TEMPLATE = """\
Improve this skill based on new execution data. \
Add any new pitfalls, update steps if the tool sequence changed, \
bump version. Return the complete updated skill file.

**Current skill:**
{current_content}

**New data:** success_count={success_count}, \
total={total_count}, avg_spirals={avg_spirals:.1f}
**Recent tool sequence:** {tool_sequence}
**New errors:** {new_errors}"""


@dataclass
class TaskPattern:
    """A detected repeatable pattern from a task execution."""

    pattern_id: str
    trigger_keywords: List[str] = field(default_factory=list)
    tool_sequence: List[str] = field(default_factory=list)
    success_count: int = 0
    total_count: int = 0
    avg_spirals: float = 0.0
    avg_duration: float = 0.0
    common_errors: List[str] = field(default_factory=list)
    last_seen: float = 0.0
    skill_name: str = ""

    @property
    def success_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.success_count / self.total_count

    def to_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "trigger_keywords": self.trigger_keywords,
            "tool_sequence": self.tool_sequence,
            "success_count": self.success_count,
            "total_count": self.total_count,
            "avg_spirals": self.avg_spirals,
            "avg_duration": self.avg_duration,
            "common_errors": self.common_errors,
            "last_seen": self.last_seen,
            "skill_name": self.skill_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskPattern":
        return cls(
            pattern_id=d.get("pattern_id", ""),
            trigger_keywords=d.get("trigger_keywords", []),
            tool_sequence=d.get("tool_sequence", []),
            success_count=d.get("success_count", 0),
            total_count=d.get("total_count", 0),
            avg_spirals=d.get("avg_spirals", 0.0),
            avg_duration=d.get("avg_duration", 0.0),
            common_errors=d.get("common_errors", []),
            last_seen=d.get("last_seen", 0.0),
            skill_name=d.get("skill_name", ""),
        )

    def __repr__(self) -> str:
        return (
            f"TaskPattern(id={self.pattern_id}, "
            f"triggers={self.trigger_keywords[:3]}, "
            f"success={self.success_count}/{self.total_count} ({self.success_rate:.0%}), "
            f"skill={self.skill_name or '—'})"
        )


class SkillEvolutionEngine:
    """Autonomous skill extraction and evolution from task experience.

    Hooks into the LEARN phase. When enough successful executions of a
    similar task pattern accumulate, it crystallizes them into a Skill.

    Accepts ConfigManager + LLMClient via constructor so paths and LLM
    are resolved at init time — no late-set side effects.
    """

    def __init__(
        self,
        config: "ConfigManager",
        llm: Optional["LLMClient"] = None,
        enabled: bool = True,
        min_success: int = 3,
    ):
        self.config = config
        self._llm_client = llm
        self.enabled = enabled
        self.min_success = min_success

        self._patterns: Dict[str, TaskPattern] = {}
        self._recent_tasks: List[Dict] = []
        self._loaded = False

        # Resolve paths from config
        self._skills_dir = Path(config.expand_path("$WW_HOME/skills"))
        self._evolution_db = Path(config.expand_path("~/.ww/skill_evolution.json"))

    # ── Persistence ──────────────────────────────────────────

    def ensure_loaded(self):
        if self._loaded:
            return
        self._evolution_db.parent.mkdir(parents=True, exist_ok=True)
        if self._evolution_db.is_file():
            try:
                data = json.loads(self._evolution_db.read_text())
                patterns = data.get("patterns", {})
                self._patterns = {
                    k: TaskPattern.from_dict(v) for k, v in patterns.items()
                }
                self._recent_tasks = data.get("recent_tasks", [])[-50:]
            except Exception as e:
                log.warning("Skill evolution DB load failed: %s", e)
        self._loaded = True

    def _save(self):
        self._evolution_db.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "patterns": {k: v.to_dict() for k, v in self._patterns.items()},
            "recent_tasks": self._recent_tasks[-50:],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self._evolution_db.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # ── Phase: Observe ────────────────────────────────────────

    def observe_task(
        self,
        goal: str,
        tool_sequence: List[str],
        success: bool,
        spirals: int,
        duration: float,
        errors: Optional[List[str]] = None,
    ):
        """Record a completed task for pattern analysis."""
        if not self.enabled:
            return
        self.ensure_loaded()

        entry = {
            "goal": goal[:200],
            "tool_sequence": tool_sequence,
            "success": success,
            "spirals": spirals,
            "duration": duration,
            "errors": errors or [],
            "timestamp": time.time(),
        }
        self._recent_tasks.append(entry)

        # Trim to last 100
        if len(self._recent_tasks) > 100:
            self._recent_tasks = self._recent_tasks[-100:]

        # Extract trigger keywords from goal
        trigger_kw = self._extract_keywords(goal)

        # Find or create pattern
        pattern_key = self._find_similar_pattern(tool_sequence, trigger_kw)

        if pattern_key:
            pat = self._patterns[pattern_key]
            pat.total_count += 1
            if success:
                pat.success_count += 1
            pat.avg_spirals = (
                pat.avg_spirals * (pat.total_count - 1) + spirals
            ) / pat.total_count
            pat.avg_duration = (
                pat.avg_duration * (pat.total_count - 1) + duration
            ) / pat.total_count
            pat.last_seen = time.time()
            if errors:
                for e in errors:
                    if e not in pat.common_errors:
                        pat.common_errors.append(e)
        elif success and len(tool_sequence) >= MIN_TASK_STEPS:
            # New pattern
            pid = uuid.uuid4().hex[:8]
            pat = TaskPattern(
                pattern_id=pid,
                trigger_keywords=trigger_kw,
                tool_sequence=tool_sequence,
                success_count=1,
                total_count=1,
                avg_spirals=float(spirals),
                avg_duration=duration,
                common_errors=errors or [],
                last_seen=time.time(),
            )
            self._patterns[pid] = pat

        self._save()

        # Check if any pattern is ready for skill extraction
        if success and len(tool_sequence) >= MIN_TASK_STEPS:
            self._maybe_extract_skill(
                goal, pattern_key or list(self._patterns.keys())[-1]
            )

    # ── Phase: Analyze ────────────────────────────────────────

    @staticmethod
    def _extract_keywords(goal: str) -> List[str]:
        """Extract trigger keywords from a goal string."""
        goal_lower = goal.lower()
        keywords: List[str] = []

        action_verbs = [
            "build", "create", "refactor", "deploy", "test", "fix",
            "analyze", "migrate", "configure", "optimize", "review",
            "code review", "debug", "install", "update", "upgrade",
            "search", "find", "generate", "convert", "transform",
        ]
        for verb in action_verbs:
            if verb in goal_lower:
                keywords.append(verb)

        tech_terms = [
            "python", "react", "api", "database", "docker", "git",
            "aws", "linux", "nginx", "kubernetes", "typescript",
            "rust", "sql", "graphql", "rest", "websocket", "mqtt",
            "p2p", "blockchain", "ml", "ai", "llm", "transformer",
        ]
        for term in tech_terms:
            if term in goal_lower:
                keywords.append(term)

        return keywords[:5]

    def _find_similar_pattern(
        self, tool_sequence: List[str], keywords: List[str]
    ) -> Optional[str]:
        """Find an existing pattern that matches this execution."""
        best_key = None
        best_score = 0.0

        for pid, pat in self._patterns.items():
            if not pat.tool_sequence:
                continue
            seq_score = self._jaccard_similarity(
                set(tool_sequence), set(pat.tool_sequence)
            )
            kw_score = (
                self._jaccard_similarity(set(keywords), set(pat.trigger_keywords))
                if pat.trigger_keywords
                else 0.0
            )
            total = seq_score * 0.7 + kw_score * 0.3
            if total > 0.4 and total > best_score:
                best_score = total
                best_key = pid

        return best_key

    @staticmethod
    def _jaccard_similarity(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        intersection = a & b
        union = a | b
        return len(intersection) / len(union) if union else 0.0

    # ── Phase: Extract → Skill ─────────────────────────────────

    def _maybe_extract_skill(self, goal: str, pattern_id: str):
        """Check if a pattern is mature enough to become a Skill."""
        pat = self._patterns.get(pattern_id)
        if not pat:
            return
        if pat.skill_name:
            self._maybe_improve_skill(pat)
            return

        if pat.success_count < self.min_success:
            return
        if pat.success_rate < MIN_SUCCESS_RATE:
            return
        if pat.total_count < MIN_SPIRALS:
            return

        skill_data = self._llm_distill_skill(pat, goal)
        if not skill_data:
            skill_data = self._heuristic_skill(pat)

        if skill_data:
            self._write_skill(skill_data)
            pat.skill_name = skill_data["name"]
            self._save()
            log.info("New skill crystallized: %s", skill_data["name"])

    def _maybe_improve_skill(self, pat: TaskPattern):
        """Check if an existing skill should be updated with new patterns."""
        if pat.success_count <= self.min_success:
            return
        if pat.success_count % 5 != 0:
            return
        skill_path = self._skills_dir / (pat.skill_name + ".md")
        if not skill_path.is_file():
            return
        try:
            content = skill_path.read_text()
            current_steps = self._parse_skill_steps(content)
            if set(pat.tool_sequence[:8]) != set(current_steps[:8]):
                updated = self._llm_update_skill(pat, content)
                if updated:
                    skill_path.write_text(updated)
                    log.info("Skill improved: %s v%d", pat.skill_name, pat.total_count)
        except Exception as e:
            log.warning("Skill improvement failed: %s", e)

    def _llm_distill_skill(
        self, pat: TaskPattern, goal: str
    ) -> Optional[Dict]:
        """Use LLM to distill a high-quality skill from observed patterns."""
        if not self._llm_client:
            return None
        try:
            related = [
                t
                for t in self._recent_tasks[-20:]
                if any(kw in t["goal"].lower() for kw in pat.trigger_keywords)
            ]
            examples = "\n".join(
                f"- Goal: {t['goal'][:80]} | {'✓' if t['success'] else '✗'} | "
                f"{t['spirals']} spirals | Tools: {', '.join(t['tool_sequence'][:5])}"
                for t in related[:5]
            )

            prompt = DISTILL_PROMPT_TEMPLATE.format(
                trigger_keywords=", ".join(pat.trigger_keywords),
                tool_sequence=" → ".join(pat.tool_sequence[:8]),
                success_count=pat.success_count,
                total_count=pat.total_count,
                success_rate=pat.success_rate,
                avg_spirals=pat.avg_spirals,
                common_errors=", ".join(pat.common_errors[-3:]),
                examples=examples,
            )
            result = self._llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                json_mode=False,
                max_tokens=1500,
            )
            if not result:
                return None
            return self._parse_skill_from_llm(result)
        except Exception as e:
            log.warning("LLM skill distillation failed: %s", e)
            return None

    @staticmethod
    def _parse_skill_from_llm(text: str) -> Optional[Dict]:
        """Parse LLM output into skill metadata."""
        fm_match = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not fm_match:
            return None
        frontmatter = fm_match.group(1)
        body = text[fm_match.end():].strip()

        data: Dict[str, str] = {}
        for line in frontmatter.strip().split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                data[key.strip()] = val.strip()

        name = data.get("name", "auto-skill-" + str(int(time.time())))
        return {
            "name": name,
            "description": data.get("description", ""),
            "trigger": data.get("trigger", ""),
            "category": data.get("category", "general"),
            "version": int(data.get("version", 1)),
            "body": body,
            "raw": text,
        }

    def _heuristic_skill(self, pat: TaskPattern) -> Dict:
        """Generate a skill from pattern data without LLM."""
        name = f"auto-{'-'.join(pat.trigger_keywords[:3]) or 'pattern'}".lower()
        name = re.sub(r"[^a-z0-9-]", "-", name)[:40]

        steps = [
            f"{i+1}. Execute `{tool}` — {self._describe_tool(tool)}"
            for i, tool in enumerate(pat.tool_sequence[:8])
        ]
        pitfalls = (
            pat.common_errors[-5:]
            if pat.common_errors
            else ["No known pitfalls yet"]
        )

        body = (
            "## Steps\n"
            + "\n".join(steps)
            + "\n\n"
            "## Pitfalls\n"
            + "\n".join(f"- {p}" for p in pitfalls)
            + "\n\n"
            "## Notes\n"
            f"Auto-generated from {pat.success_count}/{pat.total_count} successful executions.\n"
        )

        raw = (
            f"---\n"
            f"name: {name}\n"
            f"description: Auto-extracted pattern\n"
            f"trigger: {', '.join(pat.trigger_keywords)}\n"
            f"category: general\n"
            f"version: 1\n"
            f"---\n\n"
            f"{body}"
        )

        return {
            "name": name,
            "description": f"Auto-extracted: {', '.join(pat.trigger_keywords[:3])}",
            "trigger": ", ".join(pat.trigger_keywords),
            "category": "general",
            "version": 1,
            "body": body,
            "raw": raw,
        }

    def _llm_update_skill(
        self, pat: TaskPattern, current_content: str
    ) -> Optional[str]:
        """Use LLM to improve an existing skill with new data."""
        if not self._llm_client:
            return None
        try:
            prompt = UPDATE_PROMPT_TEMPLATE.format(
                current_content=current_content,
                success_count=pat.success_count,
                total_count=pat.total_count,
                avg_spirals=pat.avg_spirals,
                tool_sequence=" → ".join(pat.tool_sequence[:8]),
                new_errors=", ".join(pat.common_errors[-3:]),
            )
            result = self._llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                json_mode=False,
                max_tokens=2000,
            )
            return result if result else None
        except Exception:
            return None

    def _write_skill(self, skill_data: Dict):
        """Persist a skill to disk (atomic write: temp then rename)."""
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        path = self._skills_dir / (skill_data["name"] + ".md")

        content = skill_data.get("raw", "")
        if not content:
            lines = ["---"]
            for k in ["name", "description", "trigger", "category", "version"]:
                lines.append(f"{k}: {skill_data.get(k, '')}")
            lines.append("---")
            lines.append("")
            lines.append(skill_data.get("body", ""))
            content = "\n".join(lines)

        # Atomic write
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        tmp_path.rename(path)

    @staticmethod
    def _parse_skill_steps(content: str) -> List[str]:
        """Extract tool names from skill steps."""
        tools = []
        for line in content.split("\n"):
            m = re.search(r"`(\w+)`", line)
            if m:
                tools.append(m.group(1))
        return tools

    @staticmethod
    def _describe_tool(tool_name: str) -> str:
        descriptions = {
            "search_files": "Search project files",
            "read_file": "Read file contents",
            "write_file": "Write new file",
            "patch": "Edit existing file",
            "terminal": "Run shell command",
            "web_search": "Search the web",
            "web_fetch": "Fetch web page",
            "memory_store": "Store to memory",
            "memory_recall": "Recall from memory",
            "git": "Git operations",
            "delegate_task": "Delegate subtask",
            "vision_analyze": "Analyze image",
            "code_exec": "Execute code",
            "test": "Run tests",
        }
        return descriptions.get(tool_name, tool_name.replace("_", " ").title())

    # ── Public API ────────────────────────────────────────────

    def stats(self) -> Dict:
        """Return evolution statistics."""
        self.ensure_loaded()
        return {
            "patterns_tracked": len(self._patterns),
            "skills_extracted": sum(
                1 for p in self._patterns.values() if p.skill_name
            ),
            "recent_tasks": len(self._recent_tasks),
            "top_patterns": [
                {
                    "trigger": ", ".join(p.trigger_keywords[:2]),
                    "success_rate": f"{p.success_rate:.0%}",
                    "count": p.total_count,
                    "skill": p.skill_name or "—",
                }
                for p in sorted(
                    self._patterns.values(),
                    key=lambda x: x.total_count,
                    reverse=True,
                )[:5]
            ],
        }

    def list_auto_skills(self) -> List[str]:
        """Return list of auto-generated skill names."""
        self.ensure_loaded()
        return [p.skill_name for p in self._patterns.values() if p.skill_name]

    def force_extract(self, goal: str) -> Optional[Dict]:
        """Force skill extraction for a given goal (debug/testing)."""
        self.ensure_loaded()
        recent = [t for t in self._recent_tasks[-10:]]
        if not recent:
            return {"error": "No recent tasks to analyze"}
        all_tools: List[str] = []
        for t in recent:
            all_tools.extend(t["tool_sequence"])
        unique_tools = list(dict.fromkeys(all_tools))[:8]
        pid = uuid.uuid4().hex[:8]
        pat = TaskPattern(
            pattern_id=pid,
            trigger_keywords=self._extract_keywords(goal),
            tool_sequence=unique_tools,
            success_count=len([t for t in recent if t["success"]]),
            total_count=len(recent),
            last_seen=time.time(),
        )
        self._patterns[pid] = pat
        skill = self._llm_distill_skill(pat, goal) or self._heuristic_skill(pat)
        if skill:
            self._write_skill(skill)
            pat.skill_name = skill["name"]
        self._save()
        return skill

    def __repr__(self) -> str:
        return (
            f"SkillEvolutionEngine(patterns={len(self._patterns)}, "
            f"skills={sum(1 for p in self._patterns.values() if p.skill_name)}, "
            f"tasks={len(self._recent_tasks)})"
        )
