"""ww/tools/skill_manager.py — Worldwave Skill system

WW's procedural memory.
Similar to Hermes skills, but more lightweight and with WW's spiral loop depth integration.

Skills is a YAML frontmatter + markdown file, stored at ~/worldwave/skills/.
each skill contains: trigger condition, steps, notes, validation method.
"""

from __future__ import annotations
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SKILLS_DIR = os.path.expanduser("~/worldwave/skills")


class Skill:
    """Single skill data structure."""
    
    def __init__(
        self,
        name: str,
        description: str = "",
        trigger: str = "",
        category: str = "general",
        steps: List[str] = None,
        pitfalls: List[str] = None,
        body: str = "",
        version: int = 1,
        created: str = "",
        updated: str = "",
    ):
        self.name = name
        self.description = description
        self.trigger = trigger
        self.category = category
        self.steps = steps or []
        self.pitfalls = pitfalls or []
        self.body = body
        self.version = version
        self.created = created or datetime.now(timezone.utc).isoformat()
        self.updated = updated or datetime.now(timezone.utc).isoformat()
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "category": self.category,
            "steps": self.steps,
            "pitfalls": self.pitfalls,
            "version": self.version,
            "created": self.created,
            "updated": self.updated,
        }
    
    def to_markdown(self) -> str:
        """Serialize into markdown file (YAML frontmatter + body)."""
        lines = ["---"]
        lines.append("name: " + self.name)
        lines.append("description: " + self.description)
        lines.append("trigger: " + self.trigger)
        lines.append("category: " + self.category)
        lines.append("version: " + str(self.version))
        lines.append("created: " + self.created)
        lines.append("updated: " + self.updated)
        if self.steps:
            lines.append("steps:")
            for s in self.steps:
                lines.append("  - " + s.replace("\n", " ").strip())
        if self.pitfalls:
            lines.append("pitfalls:")
            for p in self.pitfalls:
                lines.append("  - " + p.replace("\n", " ").strip())
        lines.append("---")
        if self.body:
            lines.append("")
            lines.append(self.body)
        return "\n".join(lines)
    
    def context_block(self) -> str:
        """Concise context block (for LLM to see)."""
        lines = []
        lines.append("## Skill: " + self.name + " (" + self.category + ")")
        lines.append(self.description)
        if self.steps:
            lines.append("")
            lines.append("Steps:")
            for i, s in enumerate(self.steps, 1):
                lines.append("  " + str(i) + ". " + s)
        if self.pitfalls:
            lines.append("")
            lines.append("Notes:")
            for p in self.pitfalls:
                lines.append("  ⚠ " + p)
        if self.body:
            lines.append("")
            lines.append(self.body[:1000])
        return "\n".join(lines)
    
    def relevance_score(self, goal: str) -> float:
        """Calculate skill and goal relevance (0-1)."""
        if not goal:
            return 0.0
        
        goal_lower = goal.lower()
        score = 0.0
        
        # description and trigger word match
        for keyword in re.split(r'[\s,, . ]+', self.description + " " + self.trigger):
            if keyword and len(keyword) > 1 and keyword.lower() in goal_lower:
                score += 0.3
        
        # step content match
        for step in self.steps:
            step_words = re.split(r'[\s,, . ]+', step.lower())
            for w in step_words:
                if len(w) > 2 and w in goal_lower:
                    score += 0.1
        
        # Body match
        for line in self.body.split("\n"):
            words = re.split(r'\s+', line.lower())
            for w in words:
                if len(w) > 3 and w in goal_lower:
                    score += 0.05
        
        return min(score, 1.0)
    
    @classmethod
    def from_markdown(cls, content: str) -> "Skill":
        """from  markdown deserialize. """
        if not content.startswith("---"):
            return cls(name="unknown", body=content)
        
        parts = content.split("---", 2)
        if len(parts) < 3:
            return cls(name="unknown", body=content)
        
        frontmatter = parts[1]
        body = parts[2].strip()
        
        # resolve frontmatter
        meta = {}
        for line in frontmatter.strip().split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                meta[key] = val
        
        # resolve list type field
        steps = []
        pitfalls = []
        in_steps = False
        in_pitfalls = False
        for line in frontmatter.strip().split("\n"):
            if line.strip().startswith("steps:"):
                in_steps = True
                in_pitfalls = False
                continue
            elif line.strip().startswith("pitfalls:"):
                in_pitfalls = True
                in_steps = False
                continue
            elif line.strip() and not line.strip().startswith("- "):
                in_steps = False
                in_pitfalls = False
            
            if in_steps and line.strip().startswith("- "):
                steps.append(line.strip()[2:])
            elif in_pitfalls and line.strip().startswith("- "):
                pitfalls.append(line.strip()[2:])
        
        return cls(
            name=meta.get("name", "unknown"),
            description=meta.get("description", ""),
            trigger=meta.get("trigger", ""),
            category=meta.get("category", "general"),
            steps=steps,
            pitfalls=pitfalls,
            body=body,
            version=int(meta.get("version", "1")),
            created=meta.get("created", ""),
            updated=meta.get("updated", ""),
        )
    
    def __repr__(self):
        return "<Skill:" + self.name + " v" + str(self.version) + ">" + self.category + ">"


class SkillManager:
    """
    WW procedural memory management.
    
    feature:
    - create/read/update/delete skills
    - auto from LLC sense phase generate learnings generate skills
    - based on goal find relevant skills
    """
    
    def __init__(self, skills_dir: str = SKILLS_DIR):
        self.skills_dir = skills_dir
        os.makedirs(self.skills_dir, exist_ok=True)
    
    def _path(self, name: str) -> str:
        if not name.endswith(".md"):
            name += ".md"
        return os.path.join(self.skills_dir, name)
    
    def list(self) -> List[Dict]:
        """list all skills (summary information)."""
        if not os.path.isdir(self.skills_dir):
            return []
        
        skills = []
        for f in sorted(os.listdir(self.skills_dir)):
            if not f.endswith(".md"):
                continue
            try:
                with open(os.path.join(self.skills_dir, f)) as fh:
                    skill = Skill.from_markdown(fh.read())
                skills.append(skill.to_dict())
            except Exception as e:
                skills.append({"name": f, "error": str(e)})
        return skills
    
    def load(self, name: str) -> Optional[Skill]:
        """loada skill completecontent. """
        path = self._path(name)
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                return Skill.from_markdown(f.read())
        except Exception:
            return None
    
    def save(self, skill: Skill) -> bool:
        """save a skill (create or update)."""
        try:
            skill.updated = datetime.now(timezone.utc).isoformat()
            path = self._path(skill.name)
            with open(path, "w") as f:
                f.write(skill.to_markdown())
            return True
        except Exception as e:
            print("Skill save error:", str(e))
            return False
    
    def delete(self, name: str) -> bool:
        """deletea skill. """
        path = self._path(name)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False
    
    def find_relevant(self, goal: str, max_results: int = 3) -> List[Skill]:
        """based on goal find the most relevant skill."""
        all_skills = []
        if not os.path.isdir(self.skills_dir):
            return []
        
        for f in os.listdir(self.skills_dir):
            if not f.endswith(".md"):
                continue
            try:
                with open(os.path.join(self.skills_dir, f)) as fh:
                    skill = Skill.from_markdown(fh.read())
                all_skills.append(skill)
            except Exception:
                continue
        
        scored = [(s.relevance_score(goal), s) for s in all_skills]
        scored.sort(key=lambda x: -x[0])
        return [s for score, s in scored if score > 0][:max_results]
    
    def autosave(self, name: str, description: str,
                 steps: List[str], pitfalls: List[str] = None,
                 body: str = "", category: str = "general") -> Skill:
        """auto from success task result create/update skill."""
        existing = self.load(name)
        if existing:
            existing.version += 1
            existing.description = description
            existing.steps = steps
            existing.pitfalls = pitfalls or []
            existing.body = body or existing.body
            existing.category = category
            self.save(existing)
            return existing
        else:
            skill = Skill(
                name=name,
                description=description,
                trigger=name.replace("-", " "),
                category=category,
                steps=steps,
                pitfalls=pitfalls or [],
                body=body,
            )
            self.save(skill)
            return skill
    
    def context_block(self, goal: str = "") -> str:
        """generatecontext block, inject relevant skills."""
        relevant = self.find_relevant(goal) if goal else []
        all_skills = self.list()
        
        lines = ["# Procedural Memory (Skills)\n"]
        
        if relevant:
            lines.append("## Related skill (load)\n")
            for s in relevant:
                lines.append(s.context_block())
                lines.append("")
        
        lines.append("## availableskilllist\n")
        if all_skills:
            for s in all_skills:
                lines.append("- " + s["name"] + ": " + s.get("description", "")[:80])
        else:
            lines.append("(No skill yet)\n")
        
        return "\n".join(lines)


def default_skill_manager() -> SkillManager:
    """createdefault  SkillManager. """
    return SkillManager()
