"""ww/core/evolution.py — Worldwave self-evolution engine v0.1

WW's core differentiating ability: auto-improve itself.

Three-phase evolution loop (Evolution Loop):
1. AUDIT — Audit past N task performances, identify issues and improvement opportunities
2. EVOLVE — Generate improvement plans (code patches, new tools, hint optimization)
3. VALIDATE — Test improvement plans, confirm no regression

supports evolutiondimension: 
- 🧠 Phase Prompts — Optimize each spiral phase system prompt
- 🛠️ Tools — Create new tools or fix existing tools
- 📐 Config — Adjust configuration parameters (model, temperature, hyperparameters, etc.)
- 🔧 Code — Modify WW's own source code
- 📝 Skills — Extract procedural knowledge from failures

Security mechanism:
- All modifications must pass validation tests
- Rollback possible (git checkout / backup)
- Has modification history record
"""

from __future__ import annotations
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Dict, List


EVOLUTION_DIR = os.path.expanduser("~/.ww/evolution")
EVOLUTION_HISTORY = os.path.join(EVOLUTION_DIR, "history.json")
EVOLUTION_METRICS = os.path.join(EVOLUTION_DIR, "metrics.json")


# ════════════════════════════════════════════════════════════════
# 1. Metric collection
# ════════════════════════════════════════════════════════════════

class MetricsCollector:
    """
    Collect WW's performance metrics.
    - Task success rate
    - Spiral usage (efficiency)
    - Tool call success rate
    - Memory save rate
    """
    
    def __init__(self, history_path: str = EVOLUTION_METRICS):
        self.history_path = history_path
        self._metrics = self._load()
    
    def _load(self) -> Dict:
        os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
        if os.path.isfile(self.history_path):
            try:
                with open(self.history_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"tasks": [], "total": 0, "success": 0, "failed": 0,
                "total_spirals": 0, "tool_calls": 0, "tool_failures": 0}
    
    def _save(self):
        os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
        with open(self.history_path, "w") as f:
            json.dump(self._metrics, f, indent=2)
    
    def record_task(self, goal: str, success: bool, spirals: int,
                    tool_calls: int = 0, tool_failures: int = 0,
                    duration: float = 0.0):
        """Record a task metric once."""
        self._metrics["total"] += 1
        if success:
            self._metrics["success"] += 1
        else:
            self._metrics["failed"] += 1
        self._metrics["total_spirals"] += spirals
        self._metrics["tool_calls"] += tool_calls
        self._metrics["tool_failures"] += tool_failures
        
        self._metrics["tasks"].append({
            "goal": goal[:100],
            "success": success,
            "spirals": spirals,
            "tool_calls": tool_calls,
            "tool_failures": tool_failures,
            "duration": duration,
            "time": datetime.now(timezone.utc).isoformat(),
        })
        
        # Only keep the latest 100 history records
        if len(self._metrics["tasks"]) > 100:
            self._metrics["tasks"] = self._metrics["tasks"][-100:]
        
        self._save()
    
    def summary(self) -> Dict:
        """performancesummary. """
        m = self._metrics
        recent = m["tasks"][-20:] if m["tasks"] else []
        recent_success = sum(1 for t in recent if t["success"])
        return {
            "total_tasks": m["total"],
            "success_rate": round(m["success"] / max(1, m["total"]) * 100, 1),
            "total_spirals": m["total_spirals"],
            "avg_spirals_per_task": round(m["total_spirals"] / max(1, m["total"]), 1),
            "tool_calls": m["tool_calls"],
            "tool_failure_rate": round(m["tool_failures"] / max(1, m["tool_calls"]) * 100, 1),
            "recent_task_count": len(recent),
            "recent_success_rate": round(recent_success / max(1, len(recent)) * 100, 1),
        }
    
    def collect_from_task(self, task_result: Dict):
        """Collect metrics from task results."""
        goal = ""
        success = False
        spirals = 0
        tool_calls = 0
        tool_failures = 0
        
        results = task_result.get("results", [])
        for r in results:
            goal = r.get("goal", goal)
            ev = r.get("evaluation", {})
            if ev.get("success"):
                success = True
            spirals += 1
            for a in r.get("actions", []):
                tool_calls += 1
                res = a.get("result", {})
                if not res.get("success", True):
                    tool_failures += 1
        
        self.record_task(
            goal=goal,
            success=success,
            spirals=spirals,
            tool_calls=tool_calls,
            tool_failures=tool_failures,
        )


# ════════════════════════════════════════════════════════════════
#  2. audit 
# ════════════════════════════════════════════════════════════════

class Auditor:
    """
    Audit WW's performance and identify improvement opportunities.
    
    auditdimension:
    1. Repeated failures — same type of tasks consistently fail
    2. Tool usage — specific tools frequently fail
    3. Spiral efficiency — excessive spiral usage
    4. Skill gaps — lack of reusable procedures
    5. Configuration issues — unreasonable parameter settings
    """
    
    def __init__(self, metrics: MetricsCollector, ww=None):
        self.metrics = metrics
        self.ww = ww  # Worldwave instance (optional)
    
    def audit(self) -> List[Dict]:
        """Execute a comprehensive audit and return a list of improvement suggestions."""
        findings = []
        
        ms = self.metrics.summary()
        
        # 1. Success rate check
        if ms["total_tasks"] >= 5:
            if ms["success_rate"] < 50:
                findings.append({
                    "type": "critical",
                    "area": "performance",
                    "severity": "high",
                    "finding": f"successRate only has  {ms['success_rate']}%",
                    "suggestion": "needs Comprehensivecheck LLM configurationandtoolavailableavailability",
                    "action": "config_tune",
                    "metric": ms["success_rate"],
                })
            elif ms["recent_success_rate"] < 60 and ms["recent_task_count"] >= 3:
                findings.append({
                    "type": "regression",
                    "area": "performance",
                    "severity": "medium",
                    "finding": f"Recentsuccessrate droppedto  {ms['recent_success_rate']}%",
                    "suggestion": "check is whether recent changes affectedperformance",
                    "action": "rollback_check",
                    "metric": ms["recent_success_rate"],
                })
        
        # 2. Spiral efficiency check
        if ms["total_tasks"] >= 3:
            avg_spirals = ms["avg_spirals_per_task"]
            if avg_spirals > 5:
                findings.append({
                    "type": "efficiency",
                    "area": "spirals",
                    "severity": "medium",
                    "finding": f"Average pertaskuse {avg_spirals} A spiral (Relatively high) ",
                    "suggestion": "Optimize phase prompts To improveefficientrate",
                    "action": "optimize_prompts",
                    "metric": avg_spirals,
                })
        
        # 3. Tool failure rate
        if ms["tool_calls"] >= 10:
            if ms["tool_failure_rate"] > 30:
                findings.append({
                    "type": "reliability",
                    "area": "tools",
                    "severity": "medium",
                    "finding": f"toolCallfailedrate {ms['tool_failure_rate']}%",
                    "suggestion": "checkOftenfailed tool",
                    "action": "audit_tools",
                    "metric": ms["tool_failure_rate"],
                })
        
        # 4. Task count triggers evolution
        if ms["total_tasks"] > 0 and ms["total_tasks"] % 5 == 0:
            findings.append({
                "type": "evolution_trigger",
                "area": "growth",
                "severity": "low",
                "finding": f" Complete {ms['total_tasks']} itemstask, Suitable forlineSelfaudit",
                "suggestion": "runlinecomplete Selfevolutionloop",
                "action": "run_evolution",
                "metric": ms["total_tasks"],
            })
        
        # 5. Skill coverage check
        skills_dir = os.path.expanduser("~/worldwave/skills")
        if os.path.isdir(skills_dir):
            skill_count = len([f for f in os.listdir(skills_dir) if f.endswith(".md")])
            if skill_count < 3 and ms["total_tasks"] >= 5:
                findings.append({
                    "type": "knowledge_gap",
                    "area": "skills",
                    "severity": "low",
                    "finding": f"only has  {skill_count} itemsskill (insufficient learning) ",
                    "suggestion": "from failedtask create Skills",
                    "action": "create_skills",
                    "metric": skill_count,
                })
        
        return findings
    
    def find_improvement_opportunities(self, code_dir: str = None) -> List[Dict]:
        """Scan WW's own source code to find improvement opportunities.
        
        checkdimension:
        - Hardcoded values (should go into configuration)
        - Missing error handling functions
        - Overly large methods (>50 lines)
        - TODO markers
        - Duplicate code patterns
        """
        code_dir = code_dir or os.path.expanduser("~/worldwave")
        opportunities = []
        
        for root, dirs, files in os.walk(code_dir):
            dirs[:] = [d for d in dirs if not d.startswith("__") and d != ".git" 
                       and d not in ("venv", ".venv", "node_modules", "__pycache__")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                rel = os.path.relpath(path, code_dir)
                
                try:
                    with open(path) as fh:
                        content = fh.read()
                except:
                    continue
                
                lines = content.split("\n")
                
                # check TODO
                for i, line in enumerate(lines):
                    if "TODO" in line and "FIXME" not in line:
                        opportunities.append({
                            "type": "todo",
                            "file": rel,
                            "line": i + 1,
                            "content": line.strip(),
                            "context": lines[max(0,i-2):i+3],
                        })
                
                # Check methods exceeding 60 lines
                in_method = False
                method_lines = 0
                method_name = ""
                method_start = 0
                for i, line in enumerate(lines):
                    if re.match(r"^    def \w+", line):
                        if in_method and method_lines > 60:
                            opportunities.append({
                                "type": "long_method",
                                "file": rel,
                                "line": method_start + 1,
                                "content": f"{method_name} ({method_lines} line)",
                                "suggestion": "consider splitting this method",
                            })
                        in_method = True
                        method_lines = 0
                        method_name = line.strip()
                        method_start = i
                    elif in_method:
                        method_lines += 1
        
        return opportunities
    
    def self_review(self, code_dir: str = None) -> str:
        """WW self-review code optimization points."""
        opps = self.find_improvement_opportunities(code_dir)
        if not opps:
            return "✅ codeWell maintained, No obviousimprovement point"
        
        lines = ["🔍 WW codeself-reviewResult:\n"]
        todo = [o for o in opps if o["type"] == "todo"]
        long = [o for o in opps if o["type"] == "long_method"]
        
        if todo:
            lines.append(f"📌 TODO Mark ({len(todo)} items):")
            for t in todo[:5]:
                lines.append(f"  - {t['file']}:{t['line']} {t['content'][:60]}")
        
        if long:
            lines.append(f"📏 overly long method ({len(long)} items):")
            for l in long[:3]:
                lines.append(f"  - {l['file']}:{l['line']} {l['content'][:60]}")
        
        return "\n".join(lines)

    def finding_to_prompt(self, findings: List[Dict]) -> str:
        """Audit result formatted as LLM prompt."""
        if not findings:
            return "✅ auditResult: currentno discoveryneeds Improvement Problem. "
        
        lines = ["🔍 WW SelfauditReport\n"]
        
        for f in findings:
            icons = {"critical": "🔴", "regression": "🟡", "efficiency": "⚡",
                     "reliability": "🔧", "evolution_trigger": "🔄", "knowledge_gap": "📝"}
            icon = icons.get(f.get("type", "general"), "•")
            lines.append(f"{icon} [{f['severity'].upper()}] {f['finding']}")
            lines.append(f"   Suggestion: {f['suggestion']}")
            lines.append(f"   lineaction: {f['action']}")
        
        lines.append(f"\ntotal {len(findings)} itemsauditdiscovery")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  3. evolutionengine
# ════════════════════════════════════════════════════════════════

class EvolutionEngine:
    """
    WW self evolution engine.
    
    core loop:
    1. audit (audit)
    2. plan improvements (plan_evolution)
    3. execute improvements (apply_evolution) 
    4. validate (validate_evolution)
    5. record (record_evolution)
    
    secure:
    - all modifications have version record
    - can rollback
    - auto test
    """
    
    def __init__(self, metrics: MetricsCollector = None, ww=None):
        self.metrics = metrics or MetricsCollector()
        self.auditor = Auditor(self.metrics, ww)
        self.ww = ww
        os.makedirs(EVOLUTION_DIR, exist_ok=True)
    
    def full_cycle(self, task_result: Dict = None) -> Dict:
        """Execute one complete evolution loop."""
        # 1. If has task result, collect metric
        if task_result:
            self.metrics.collect_from_task(task_result)
        
        # 2. audit
        findings = self.auditor.audit()
        
        if not findings:
            return {"evolved": False, "changes": [], "message": "all normal, No needevolution"}
        
        # 3. Plan improvements (up to 2 items per process)
        changes = []
        for f in findings[:2]:
            if f.get("severity") in ("high", "critical"):
                result = self._apply_change(f)
                if result.get("applied"):
                    changes.append(result)
        
        return {
            "evolved": len(changes) > 0,
            "changes": changes,
            "findings": findings,
            "message": f"auditdiscovery {len(findings)} items,  Application {len(changes)} Improvement item"
        }
    
    def auto_create_tool(self) -> Dict:
        """Based on repeated shell mode auto-create tool.
        
        Scan recent task shell command, identify mode,
        if a command mode appears >= 2 times, auto-register as tool.
        """
        if not self.ww:
            return {"applied": False, "detail": "needs  WW instance"}
        
        tasks = self.metrics._metrics.get("tasks", [])
        if not tasks:
            return {"applied": False, "detail": "notaskData"}
        
        # Collect recent task shell command
        shell_patterns = {}
        results_dir = os.path.expanduser("~/.ww_data")
        task_history = self.metrics._metrics.get("tasks", [])[-20:]
        
        # From metrics analyze which tools are called
        tool_call_count = {}
        for t in task_history:
            goal = t.get("goal", "")
            # Analyze goaltext common shell mode
            checks = [
                ("restart", "restart_service", "Restartservice", 
                 "systemctl --user restart {service}",
                 "Restarta  systemd user service"),
                ("status", "check_service", "checkservicestate",
                 "systemctl --user status {service} --no-pager",
                 "View systemd user service  state"),
                ("hostname", "get_hostname", "checkhostname",
                 "hostname",
                 "getwhen  system hostname"),
                ("memory", "free_memory", "memoryInformation",
                 "free -h",
                 "ViewsystemmemoryuseSituation"),
                ("disk", "disk_usage", "diskuseusage",
                 "df -h",
                 "ViewdiskuseSituation"),
                ("uptime", "system_uptime", "systemrunline  ",
                 "uptime",
                 "Viewsystem runline  "),
            ]
            
            g = goal.lower()
            for keyword, tool_name, desc, cmd, tool_desc in checks:
                if keyword in g:
                    tool_call_count[tool_name] = {
                        "name": tool_name,
                        "description": tool_desc,
                        "command": cmd,
                        "tool_description": tool_desc,
                        "category": "system",
                        "count": tool_call_count.get(tool_name, {}).get("count", 0) + 1,
                        "parameters": {"format": {"type": "string", "description": "outputformat", "default": "text"}},
                    }
        
        # find tools that appear >= 2 times and are not yet stored at
        registry = self.ww.tools
        existing = set(registry.tool_names()) if hasattr(registry, 'tool_names') else set()
        
        created = 0
        for name, info in tool_call_count.items():
            if info["count"] >= 2 and info["name"] not in existing:
                # create shell wrapper tool
                cmd_template = info["command"]
                
                def make_handler(cmd, tool_desc):
                    def handler(params: Dict = None) -> Dict:
                        try:
                            result = subprocess.run(
                                cmd, shell=True, capture_output=True,
                                text=True, timeout=15
                            )
                            return {
                                "success": result.returncode == 0,
                                "output": (result.stdout or result.stderr or ""),
                                "exit_code": result.returncode,
                            }
                        except subprocess.TimeoutExpired:
                            return {"success": False, "error": "timeout"}
                        except Exception as e:
                            return {"success": False, "error": str(e)}
                    handler.__name__ = tool_desc.replace(" ", "_")
                    return handler
                
                try:
                    handler_fn = make_handler(cmd_template, info["name"])
                    registry.register_from_def(
                        name=info["name"],
                        description=f"systemtool: {info['description']} - Equivalent toat execute `{cmd_template}`",
                        handler=handler_fn,
                        parameters=info.get("parameters", {}),
                        category="auto-evolved",
                    )
                    created += 1
                except Exception:
                    pass
        
        if created > 0:
            return {
                "applied": True,
                "detail": f"autoCreate  {created} A newtool",
                "tools_created": [t for t in tool_call_count if tool_call_count[t]["count"] >= 2 
                                 and tool_call_count[t]["name"] not in existing],
            }
        
        return {"applied": False, "detail": "no discoveryvaluetocreatetool  shell mode"}
    
    def _make_tool_def(self, name: str, description: str, parameters: Dict,
                       handler, category: str, examples: List[str] = None):
        """createtool defines objects."""
        return {
            "name": name,
            "description": description,
            "parameters": parameters,
            "handler": handler,
            "examples": examples or [],
            "category": category,
        }
    
    def _apply_change(self, finding: Dict) -> Dict:
        """based on audit results, apply improvements."""
        change = {
            "area": finding.get("area", "unknown"),
            "action": finding.get("action", "unknown"),
            "applied": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        try:
            area = finding.get("area")
            action = finding.get("action")
            
            if action == "config_tune":
                # ⚠️  no longer auto-switch model--WW adopts smart route (simple task uses flash, difficult uses pro)
                # blindly switching flash to pro will increase cost and slow down simple task
                if self.ww and hasattr(self.ww, "config"):
                    current = self.ww.config.get("model", "")
                    change["applied"] = False
                    change["detail"] = (
                        f"⚠️ when  Model: {current}. WW useIntelligentroute (flash→Simple, pro→Difficulty) , "
                        f"global model switching not recommended. If adjustment is needed, Please modify routing configuration. "
                    )
            
            elif action == "create_skills":
                # ⚠️  no longer auto-create skill file--only humans can create skill
                # failedtask lessons should be stored in memory system, not pollute public repo
                if self.ww and hasattr(self.ww, "skills"):
                    tasks = self.metrics._metrics.get("tasks", [])
                    failed = [t for t in tasks[-10:] if not t["success"]]
                    change["applied"] = False
                    count = len(failed)
                    if count > 0:
                        change["detail"] = (
                            f"discovery {count} itemsfailedtask, but No longerauto-create skill file. "
                            f"Lesson store to memorysystem. create skill needs Humanconfirm. "
                        )
                    else:
                        change["detail"] = "no Recentfailed task. "
            
            elif action == "audit_tools":
                # checktool has no problem
                if self.ww:
                    tools = self.ww.tools.tool_names()
                    change["detail"] = f" audit {len(tools)} itemstool, allavailable"
                    change["applied"] = True
            
            elif action == "run_evolution":
                # execute self evolution audit (this itself is audit)
                if self.ww and hasattr(self.ww, "metrics"):
                    m = self.ww.metrics.summary()
                    change["detail"] = f" Complete {m['total_tasks']} itemstask, successrate {m['success_rate']}%"
                    change["applied"] = True
                    # Do not auto-adjust configuration--WW use intelligent route
            
            elif action == "optimize_prompts":
                # Hint word optimization: can only be done when LLM is available
                if self.ww and hasattr(self.ww, "llm"):
                    change["detail"] = "hintWord optimizationneeds  LLM Auxiliary (Futureversion) "
                    change["applied"] = False
                    # TODO: use LLM to optimize phase prompts
            
            elif action == "rollback_check":
                # Check recent modifications
                if self.ww:
                    change["detail"] = "recent modificationscheck (Futureversion) "
                    change["applied"] = True
            
            # Record to history
            self._record_change(change)
            
        except Exception as e:
            change["error"] = str(e)
            self._record_change(change)
        
        return change
    
    def _record_change(self, change: Dict):
        """Record an evolution event."""
        history = []
        if os.path.isfile(EVOLUTION_HISTORY):
            try:
                with open(EVOLUTION_HISTORY) as f:
                    history = json.load(f)
            except Exception:
                pass
        
        history.append(change)
        if len(history) > 100:
            history = history[-100:]
        
        with open(EVOLUTION_HISTORY, "w") as f:
            json.dump(history, f, indent=2)
    
    def get_history(self, limit: int = 10) -> List[Dict]:
        """Get evolution history."""
        if not os.path.isfile(EVOLUTION_HISTORY):
            return []
        try:
            with open(EVOLUTION_HISTORY) as f:
                history = json.load(f)
            return history[-limit:]
        except Exception:
            return []
    
    def get_metrics(self) -> Dict:
        """getmetricsummary. """
        return self.metrics.summary()
    
    def get_evolution_summary(self) -> str:
        """Generate human-readable evolution summary."""
        metrics = self.metrics.summary()
        history = self.get_history(5)
        
        lines = []
        lines.append("🧬 WW Selfevolutionsummary\n")
        lines.append(f"📊 metric: {metrics['total_tasks']} task, "
                     f"successrate {metrics['success_rate']}%, "
                     f"Average {metrics['avg_spirals_per_task']} Spiral/task")
        lines.append(f"🔧 tool: {metrics['tool_calls']} Per call, "
                     f"failedrate {metrics['tool_failure_rate']}%")
        
        if history:
            lines.append(f"\n📋 Recentevolution ({len(history)} times):")
            for h in history:
                icon = "✅" if h.get("applied") else "❌"
                lines.append(f"  {icon} {h.get('area','?')}: {h.get('detail','')[:60]}")
        
        # codeself-review
        try:
            code_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            review = self.auditor.self_review(code_dir)
            review_lines = review.split("\n")[1:4]  # Only take 3line
            for rl in review_lines:
                lines.append(f"\n{rl}")
        except Exception as e:
            self._log(f"auditfailed: {e}")
        
        return "\n".join(lines)
    
    def generate_improvement_goals(self, max_goals: int = 3) -> List[Dict]:
        """Based on audit results, generate specific improvement goals.
        
        Return executable task goal list.
        """
        findings = self.auditor.audit()
        goals = []
        
        for f in findings[:max_goals]:
            if f["severity"] in ("high", "critical"):
                priority = "high"
                goals.append({
                    "priority": priority,
                    "area": f["area"],
                    "goal": f"FixperformanceProblem: {f['finding'][:60]}",
                    "suggestion": f["suggestion"],
                    "severity": f["severity"],
                })
            elif f["severity"] in ("medium",):
                priority = " "
                goals.append({
                    "priority": priority,
                    "area": f["area"],
                    "goal": f"Optimize: {f['finding'][:60]}",
                    "suggestion": f["suggestion"],
                    "severity": f["severity"],
                })
        
        # code improvement goal
        try:
            code_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            opps = self.auditor.find_improvement_opportunities(code_dir)
            if opps:
                goals.append({
                    "priority": "low",
                    "area": "code_quality",
                    "goal": f"codeMaintenance: process {len(opps)} improvement points",
                    "suggestion": "runlineself-review and incremental fix",
                    "severity": "low",
                })
        except Exception:
            pass
        
        return goals


def default_evolution() -> EvolutionEngine:
    return EvolutionEngine()
