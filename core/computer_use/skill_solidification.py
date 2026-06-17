"""
ww/core/computer_use/skill_solidification.py — Cerebellar Skill Solidification v0.1

Biomimetic skill solidification: LLM-learned patterns → deterministic execution.

When the cortex (LLM) successfully performs a task pattern multiple times,
the cerebellum "compiles" that pattern into a non-LLM executable form.

This module:
1. Observes successful LLM action sequences
2. Identifies repeatable patterns
3. Extracts deterministic logic (templates, rules, scripts)
4. Stores solidified skills for direct execution without LLM

Solidification levels:
    L1: Parameterized template — fill-in-the-blanks command
    L2: Conditional rule — if-then decision tree
    L3: Compiled script — deterministic Python/Shell script
    L4: Reflex — instant action (no deliberation needed)

Pure Python, zero external dependencies.
"""

from __future__ import annotations
import hashlib
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ── Solidification levels ──

SOLIDIFICATION_LEVELS = {
    0: "none",
    1: "template",      # Parameterized command template
    2: "rule",          # Conditional if-then rule
    3: "script",        # Deterministic executable script
    4: "reflex",        # Instant reaction, no deliberation
}

MIN_OBSERVATIONS_FOR_SOLIDIFICATION = {
    1: 2,    # Template: 2 successful observations
    2: 3,    # Rule: 3 successful observations
    3: 5,    # Script: 5 successful observations
    4: 10,   # Reflex: 10 successful observations
}


@dataclass
class ActionPattern:
    """A repeatable action sequence detected by the cerebellum."""
    pattern_id: str
    domain: str                        # "shell", "file", "api", "browser"
    action_sequence: List[str]         # Ordered list of action steps
    params_template: Dict[str, Any]    # Parameter slots (to be filled)
    success_count: int
    total_attempts: int
    avg_latency: float
    solidified_level: int              # 0-4
    compiled_logic: Optional[str]      # Deterministic script (level 3+)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


@dataclass
class SolidifiedSkill:
    """A fully solidified skill, ready for non-LLM execution."""
    skill_id: str
    name: str
    domain: str
    level: int
    trigger_pattern: str               # What triggers this skill
    deterministic_action: Callable     # The actual callable
    params_schema: Dict[str, Any]      # Expected parameters
    confidence: float                  # 0-1
    use_count: int
    last_used: float


class SkillSolidifier:
    """Cerebellar skill solidification engine.

    Observes LLM action patterns and compiles them into deterministic
    skills that bypass the cortex entirely.
    """

    def __init__(
        self,
        min_confidence: float = 0.8,
        max_patterns: int = 200,
        persistence_path: str = "",
    ):
        self.min_confidence = min_confidence
        self.max_patterns = max_patterns

        # Observed action sequences (waiting to be solidified)
        self._observations: Dict[str, List[Dict]] = defaultdict(list)

        # Solidified patterns
        self._patterns: Dict[str, ActionPattern] = {}

        # Active skills (ready for non-LLM execution)
        self._skills: Dict[str, SolidifiedSkill] = {}

        # Recent execution history
        self._recent: deque = deque(maxlen=500)

        # Stats
        self._total_observed = 0
        self._total_solidified = 0
        self._total_executed = 0

        self.persistence_path = persistence_path
        if persistence_path:
            self._load()

    # ── Observation ──

    def observe(
        self,
        domain: str,
        action: str,
        params: Dict[str, Any],
        success: bool,
        output: str = "",
        latency: float = 0.0,
    ):
        """Observe an action execution for pattern detection.

        The cerebellum watches every action. Successful repetitions
        trigger solidification.
        """
        self._total_observed += 1

        # Build pattern signature
        signature = self._build_signature(domain, action, params)

        self._observations[signature].append({
            "domain": domain,
            "action": action,
            "params": params,
            "success": success,
            "output": output[:500],
            "latency": latency,
            "timestamp": time.time(),
        })

        # Trim old observations
        if len(self._observations[signature]) > 50:
            self._observations[signature] = self._observations[signature][-30:]

        self._recent.append({
            "signature": signature,
            "success": success,
            "timestamp": time.time(),
        })

        # Check if pattern is ready for solidification
        if success:
            self._check_solidification(signature)

    def _build_signature(self, domain: str, action: str,
                         params: Dict[str, Any]) -> str:
        """Build a stable pattern signature.

        Normalizes parameters to detect the same action with different values.
        """
        # Normalize: replace specific values with placeholders
        normalized = action.lower().strip()

        if params:
            # Replace parameter values with type markers
            for key in sorted(params.keys()):
                val = params[key]
                if isinstance(val, str):
                    # Replace file paths
                    if "/" in val or "\\" in val:
                        normalized += f" {key}=<path>"
                    # Replace URLs
                    elif val.startswith("http"):
                        normalized += f" {key}=<url>"
                    # Replace other strings
                    else:
                        normalized += f" {key}=<str>"
                elif isinstance(val, (int, float)):
                    normalized += f" {key}=<num>"
                elif isinstance(val, bool):
                    normalized += f" {key}=<bool>"
                elif isinstance(val, list):
                    normalized += f" {key}=<list:{len(val)}>"
                elif isinstance(val, dict):
                    normalized += f" {key}=<dict:{len(val)}>"

        # Hash for compact storage
        sig_hash = hashlib.md5(normalized.encode()).hexdigest()[:12]
        return f"{domain}:{sig_hash}"

    # ── Solidification ──

    def _check_solidification(self, signature: str):
        """Check if a pattern has been observed enough times to solidify."""
        if signature in self._patterns:
            return  # Already solidified

        obs = self._observations[signature]
        successes = [o for o in obs if o["success"]]
        if not successes:
            return

        success_count = len(successes)
        total = len(obs)
        success_rate = success_count / max(1, total)
        avg_latency = sum(o["latency"] for o in successes) / max(1, success_count)

        # Determine solidification level
        level = self._determine_level(success_count, success_rate)
        if level == 0:
            return

        # Extract pattern
        representative = successes[-1]  # Most recent success
        domain = representative["domain"]
        action = representative["action"]
        params = representative.get("params", {})

        # Extract parameter template
        param_template = self._extract_template(obs)

        # Try to compile deterministic logic for level 3+
        compiled = None
        if level >= 3:
            compiled = self._compile_to_script(domain, action, param_template, obs)

        pattern = ActionPattern(
            pattern_id=signature,
            domain=domain,
            action_sequence=[action],
            params_template=param_template,
            success_count=success_count,
            total_attempts=total,
            avg_latency=avg_latency,
            solidified_level=level,
            compiled_logic=compiled,
        )
        self._patterns[signature] = pattern
        self._total_solidified += 1

        # Create a Skill for levels 3+ (deterministic)
        if level >= 3 and compiled:
            self._create_skill(signature, pattern)

    def _determine_level(self, success_count: int, success_rate: float) -> int:
        """Determine solidification level based on success history."""
        if success_rate < self.min_confidence:
            return 0
        for level in [4, 3, 2, 1]:
            if success_count >= MIN_OBSERVATIONS_FOR_SOLIDIFICATION[level]:
                return level
        return 0

    def _extract_template(self, observations: List[Dict]) -> Dict[str, Any]:
        """Extract a parameterized template from observations."""
        if not observations:
            return {}

        # Use the most recent successful observation as template
        successes = [o for o in observations if o["success"]]
        if not successes:
            return {}

        template = successes[-1]["params"].copy()

        # Mark slots that vary across observations
        all_keys = set()
        for o in successes:
            all_keys.update(o.get("params", {}).keys())

        for key in all_keys:
            values = set()
            for o in successes:
                val = o.get("params", {}).get(key)
                if val is not None:
                    # Use string representation for comparison
                    try:
                        values.add(json.dumps(val, sort_keys=True))
                    except (TypeError, ValueError):
                        values.add(str(val))
            if len(values) > 1:
                # This parameter varies — mark as a slot
                template[key] = f"<slot:{key}>"

        return template

    def _compile_to_script(
        self,
        domain: str,
        action: str,
        template: Dict[str, Any],
        observations: List[Dict],
    ) -> Optional[str]:
        """Compile a pattern into a deterministic script.

        For shell commands: produce a parameterized shell command
        For file operations: produce a template
        For API calls: produce a curl-like template
        """
        if domain == "shell":
            # Build a parameterized shell command
            cmd = action
            for key, val in sorted(template.items()):
                if isinstance(val, str) and val.startswith("<slot:"):
                    cmd += f" ${{{key.upper()}}}"
                elif val is not None:
                    cmd += f" {val}"
            return cmd

        if domain == "file":
            return f"# File operation: {action}\n# Template: {json.dumps(template, indent=2)}"

        if domain == "api":
            method = template.get("method", "GET")
            url = template.get("url", "<url>")
            headers = template.get("headers", {})
            body = template.get("body", "")
            lines = [f"# API call: {method} {url}"]
            for k, v in headers.items():
                lines.append(f"# Header: {k}: {v}")
            if body:
                lines.append(f"# Body: {body}")
            return "\n".join(lines)

        return None

    # ── Skill creation ──

    def _create_skill(self, signature: str, pattern: ActionPattern):
        """Create a SolidifiedSkill from a solidified pattern.

        Level 3+ patterns get compiled into callable skills.
        """
        if pattern.solidified_level < 3 or not pattern.compiled_logic:
            return

        skill_id = f"cerebellar_{signature}"

        # Build the deterministic callable
        if pattern.domain == "shell":
            def _shell_executor(params: Dict[str, Any] = None) -> Dict[str, Any]:
                """Execute a solidified shell command."""
                cmd = pattern.compiled_logic
                if params:
                    for key, val in params.items():
                        cmd = cmd.replace(f"${{{key.upper()}}}", str(val))
                import subprocess
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=30,
                    )
                    return {
                        "success": result.returncode == 0,
                        "output": result.stdout[:1000],
                        "error": result.stderr[:500],
                        "exit_code": result.returncode,
                    }
                except Exception as e:
                    return {"success": False, "error": str(e)}
            executor = _shell_executor
        else:
            executor = lambda params=None: {"success": False, "error": "not executable"}

        skill = SolidifiedSkill(
            skill_id=skill_id,
            name=f"Cerebellar: {pattern.domain}/{pattern.action_sequence[0][:40]}",
            domain=pattern.domain,
            level=pattern.solidified_level,
            trigger_pattern=pattern.action_sequence[0] if pattern.action_sequence else "",
            deterministic_action=executor,
            params_schema=pattern.params_template,
            confidence=pattern.success_count / max(1, pattern.total_attempts),
            use_count=0,
            last_used=0.0,
        )
        self._skills[skill_id] = skill

    # ── Execution ──

    def try_execute(
        self,
        domain: str,
        action: str,
        params: Dict[str, Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Try to execute an action via solidified skill (bypassing LLM).

        Returns None if no matching skill exists (caller should fall back to LLM).
        """
        signature = self._build_signature(domain, action, params or {})
        if signature not in self._skills:
            return None

        skill = self._skills[signature]
        if skill.confidence < self.min_confidence:
            return None

        skill.use_count += 1
        skill.last_used = time.time()
        self._total_executed += 1

        try:
            result = skill.deterministic_action(params)
            return result
        except Exception as e:
            return {"success": False, "error": f"skill execution failed: {e}"}

    def has_skill(self, domain: str, action: str,
                  params: Dict[str, Any] = None) -> bool:
        """Check if a solidified skill exists for this action."""
        signature = self._build_signature(domain, action, params or {})
        skill = self._skills.get(signature)
        return skill is not None and skill.confidence >= self.min_confidence

    # ── Serialization ──

    def to_dict(self) -> Dict:
        return {
            "patterns": {
                sid: {
                    "pattern_id": p.pattern_id,
                    "domain": p.domain,
                    "action_sequence": p.action_sequence,
                    "params_template": p.params_template,
                    "success_count": p.success_count,
                    "total_attempts": p.total_attempts,
                    "solidified_level": p.solidified_level,
                    "compiled_logic": p.compiled_logic,
                    "first_seen": p.first_seen,
                    "last_seen": p.last_seen,
                }
                for sid, p in self._patterns.items()
            },
            "total_observed": self._total_observed,
            "total_solidified": self._total_solidified,
            "total_executed": self._total_executed,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "SkillSolidifier":
        s = cls()
        for sid, pd in d.get("patterns", {}).items():
            s._patterns[sid] = ActionPattern(
                pattern_id=pd["pattern_id"],
                domain=pd["domain"],
                action_sequence=pd.get("action_sequence", []),
                params_template=pd.get("params_template", {}),
                success_count=pd.get("success_count", 0),
                total_attempts=pd.get("total_attempts", 0),
                avg_latency=0.0,
                solidified_level=pd.get("solidified_level", 0),
                compiled_logic=pd.get("compiled_logic"),
                first_seen=pd.get("first_seen", time.time()),
                last_seen=pd.get("last_seen", time.time()),
            )
            # Re-create skills for level 3+ patterns
            if s._patterns[sid].solidified_level >= 3:
                s._create_skill(sid, s._patterns[sid])
        s._total_observed = d.get("total_observed", 0)
        s._total_solidified = d.get("total_solidified", 0)
        s._total_executed = d.get("total_executed", 0)
        return s

    def save(self, path: str = ""):
        p = path or self.persistence_path
        if p:
            with open(p, "w") as f:
                json.dump(self.to_dict(), f, indent=2)

    def _load(self):
        try:
            with open(self.persistence_path) as f:
                d = json.load(f)
            loaded = SkillSolidifier.from_dict(d)
            self._patterns = loaded._patterns
            self._skills = loaded._skills
            self._total_observed = loaded._total_observed
            self._total_solidified = loaded._total_solidified
            self._total_executed = loaded._total_executed
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # ── Stats ──

    def stats(self) -> Dict:
        return {
            "patterns_observed": len(self._observations),
            "patterns_solidified": len(self._patterns),
            "skills_active": len(self._skills),
            "total_observed": self._total_observed,
            "total_solidified": self._total_solidified,
            "total_executed": self._total_executed,
            "by_level": {
                str(level): sum(
                    1 for p in self._patterns.values()
                    if p.solidified_level == level
                )
                for level in range(5)
            },
            "recent_observations": len(self._recent),
        }


# ── Factory ──

def create_skill_solidifier(**kwargs) -> SkillSolidifier:
    return SkillSolidifier(**kwargs)
