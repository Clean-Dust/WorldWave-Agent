"""
ww/core/loop.py — Worldwave Spiral Cognitive Loop Engine v0.2

The LLM-driven spiral cognitive loop at WW's core.

Each spiral:
1. PERCEIVE  - LLM analyzes goal + environment, decides perception strategy
2. RECALL    - Recalls relevant experiences from memory v2
3. PLAN      - LLM creates executable plan (step-by-step with tool calls)
4. ACT       - Executes the plan steps
5. EVALUATE  - LLM evaluates results against the goal
6. LEARN     - Extracts lessons and stores in memory v2

Each phase auto-checkpoints for crash recovery.
"""
from __future__ import annotations
import sys
import os
import json
import re
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.state import StateManager, SpiralState
from core.llm import create_llm
from tools.registry import ToolRegistry, default_registry
from tools.skill_manager import SkillManager
from core.config import ConfigManager
from core.scheduler import Scheduler
from core.evolution import EvolutionEngine
from core.logger import get_logger
from core.subconscious import Subconscious
from core.checkpoint import CheckpointDB, build_context_snapshot
from core.global_workspace import GlobalWorkspace
from core.cascade import CascadeBus, wire_biomimetic_cascade
from core.circadian import CircadianRhythm, collect_system_metrics
from core.subconscious.basal_ganglia import BasalGanglia
from core.computer_use.predictive_model import PredictiveModel
from core.computer_use.skill_solidification import SkillSolidifier


class Worldwave:
    """
    Worldwave Subject — v0.2 LLM Driven version. 

    Usage: 
        ww = Worldwave()
        result = ww.run("Check system status")
    """

    def __init__(
        self,
        model: str = "deepseek/deepseek-v4-flash",
        persist_dir: str = "",
        memory_system=None,
        llm_config: Optional[Dict[str, Any]] = None,
        tools: Optional[ToolRegistry] = None,
    ):
        self.model = model
        self.memory = memory_system  # Built-in MemorySystem Instance (Optional)
        self.state = StateManager(persist_dir=persist_dir)
        self.llm = create_llm(llm_config or {"model": model})
        self.tools = tools or default_registry()
        self.skills = SkillManager()
        self.config = ConfigManager()
        self.scheduler = Scheduler()
        self.evolution = EvolutionEngine(ww=self)
        self.metrics = self.evolution.metrics

        self.running = False
        self.verbose = True
        self._pending_question = None
        self._wwlog = get_logger()
        self.checkpoint_db = CheckpointDB()
        self._tool_history: List[Dict] = []
        self._steps_total = 0
        self._steps_completed = 0

        # Subconscious v4: Meta-learning observer (Pure decision tree ensemble, Do not read conversation) 
        enabled = self.config.get("subconscious_enabled", True)
        self.subconscious = Subconscious(
            enabled=enabled,
            rewind_threshold=self.config.get("subconscious_threshold", 0.7),
        )

        # Context window manager (Dynamic compression) 
        from core.context import ConversationManager
        self.conversation = ConversationManager(
            llm=self.llm,
            default_max_messages=self.config.get("context_max_messages", 30),
            default_max_tokens=self.config.get("context_max_tokens", 32000),
        )

        # ── Biomimetic modules (v0.6) ──

        # Global Workspace: LLM context as scarce resource (7-item capacity)
        self.workspace = GlobalWorkspace(
            capacity=self.config.get("workspace_capacity", 7),
        )

        # Basal Ganglia: dual-pathway G/N action evaluation
        self.basal_ganglia = BasalGanglia(
            state_dim=32,
        )
        # Load persisted model if exists
        bg_path = os.path.join(persist_dir or os.path.expanduser("~/.ww"), "basal_ganglia.json")
        if os.path.exists(bg_path):
            self.basal_ganglia._load(bg_path)

        # Circadian Rhythm: adaptive heartbeat
        self.circadian = CircadianRhythm()

        # Cross-module Cascade Bus
        self.cascade = CascadeBus()
        wire_biomimetic_cascade(
            bus=self.cascade,
            basal_ganglia=self.basal_ganglia,
            global_workspace=self.workspace,
            circadian_rhythm=self.circadian,
        )

        # Cerebellum: internal predictive model + skill solidification
        self.predictive_model = PredictiveModel()
        pm_path = os.path.join(persist_dir or os.path.expanduser("~/.ww"), "predictive_model.json")
        if os.path.exists(pm_path):
            self.predictive_model._load(pm_path)

        self.skill_solidifier = SkillSolidifier()
        ss_path = os.path.join(persist_dir or os.path.expanduser("~/.ww"), "solidified_skills.json")
        if os.path.exists(ss_path):
            self.skill_solidifier._load(ss_path)


    def _store_memory(self, content: str, source: str = "ww_loop",
                      entities: List[str] = None) -> Optional[str]:
        """Store to built-in MemorySystem. """
        if self.memory is None:
            return None
        try:
            result = self.memory._do_store(
                content=content,
                source=source,
                tags=entities,
            )
            return result.get("atom_id")
        except Exception as e:
            logger.warning(f"_store_memory Failure: {e}")
            return None

    def _recall_memory(self, query: str, limit: int = 5) -> List[Dict]:
        """Recall (Built-in MemorySystem) . """
        if self.memory is None:
            return []
        try:
            results = self.memory.recall(query, top_k=limit)
            return [r.get("atom", r) for r in results.get("results", [])]
        except Exception as e:
            logger.warning(f"_recall_memory Failure: {e}")
            return []

    # ── Reflex Arc constants ──────────────────────────────────────
    REFLEX_THRESHOLD = 0.15      # Complexity score below this → fast path
    REFLEX_MAX_TOKENS = 2048     # Max tokens for reflex LLM call
    REFLEX_SAFETY_THRESHOLD = 0.6  # Basal Ganglia N-score above this → block

    def _estimate_complexity(self, goal: str) -> float:
        """Estimate task complexity from the goal string alone.

        Returns a score 0.0 (trivial) to 1.0 (highly complex).
        Used to decide whether to take the reflex arc shortcut.
        """
        goal_lower = goal.lower().strip()

        # ── Factor 1: Token count ──
        words = goal.split()
        if len(words) <= 5:
            token_score = 0.0
        elif len(words) <= 15:
            token_score = 0.2
        elif len(words) <= 40:
            token_score = 0.5
        else:
            token_score = 0.8

        # ── Factor 2: Action verb analysis ──
        single_step_verbs = {
            "change", "replace", "fix", "delete", "remove", "add", "create",
            "read", "show", "list", "find", "search", "check", "run", "execute",
            "set", "update", "modify", "rename", "move", "copy", "open",
            "write", "edit", "改", "刪除", "替換", "修改", "查看", "顯示",
        }
        multi_step_markers = {
            "plan", "design", "build", "implement", "refactor", "migrate",
            "deploy", "orchestrate", "coordinate", "analyze", "architecture",
            "first", "then", "after", "before", "finally", "next",
            "multiple", "several", "all", "every", "each",
            "設計", "架構", "重構", "部署", "首先", "然後", "之後",
            "多個", "全部", "每個", "分析",
        }

        goal_words = set(goal_lower.split())

        single_hits = goal_words & single_step_verbs
        multi_hits = goal_words & multi_step_markers

        if multi_hits:
            verb_score = 0.7 + min(0.3, len(multi_hits) * 0.1)
        elif single_hits and len(goal_words) <= 12:
            verb_score = 0.05
        elif single_hits:
            verb_score = 0.2
        else:
            verb_score = 0.5  # Ambiguous — lean cautious

        # ── Factor 3: Structural markers ──
        structural_score = 0.0
        # Bullet points / numbered lists → structured multi-step task
        if re.search(r'[\n\r]', goal) or re.search(r'^\d+[\.\)]', goal, re.MULTILINE):
            structural_score = 0.6
        # Code blocks / file paths → likely a specific edit
        if re.search(r'`[^`]+`', goal) or '/' in goal or '.py' in goal or '.js' in goal:
            structural_score = max(structural_score, 0.1)
        # URLs → research/summarization task
        if 'http' in goal_lower:
            structural_score = max(structural_score, 0.3)
        # Question marks → simple Q&A
        if goal.strip().endswith('?') and len(words) <= 15:
            structural_score = min(structural_score, 0.1)

        # ── Factor 4: Line-number patterns (ultra-specific edits) ──
        line_edit_score = 0.0
        if re.search(r'line\s+\d+|第\s*\d+\s*行|:\d+', goal_lower):
            line_edit_score = -0.3  # Strong signal of trivial edit

        # ── Combine ──
        raw = (token_score * 0.3 + verb_score * 0.45 +
               structural_score * 0.25 + line_edit_score)
        return max(0.0, min(1.0, raw))

    def _reflex_arc_execute(self, goal: str) -> Optional[Dict[str, Any]]:
        """Fast path: direct LLM → tool execution, bypassing full spiral.

        Only for trivially simple tasks. Returns None if reflex arc
        cannot handle this task (falls through to full spiral).
        """
        # Build a direct system prompt for single-turn tool calling
        tools_json = self.tools.to_openai_tools() if hasattr(self.tools, 'to_openai_tools') else None
        tool_descriptions = self.tools.prompt_block()[:3000] if hasattr(self.tools, 'prompt_block') else ""

        system_prompt = (
            "You are an autonomous coding agent. Execute the user's task directly "
            "using a single tool call if possible. Be precise and minimal.\n\n"
            "Available tools:\n" + tool_descriptions
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": goal},
        ]

        try:
            if tools_json:
                resp = self.llm._call(
                    messages=messages,
                    json_mode=False,
                    temperature=0.1,
                    max_tokens=self.REFLEX_MAX_TOKENS,
                    tools=tools_json,
                )
            else:
                resp = self.llm._call(
                    messages=messages,
                    json_mode=False,
                    temperature=0.1,
                    max_tokens=self.REFLEX_MAX_TOKENS,
                )
        except Exception:
            return None  # LLM call failed → fall through to full spiral

        # ── Process tool calls from response ──
        tool_calls = getattr(resp, 'tool_calls', []) or []
        if not tool_calls:
            # LLM returned text only — use as direct response
            return {
                "status": "completed",
                "spirals_completed": 0,
                "results": [{
                    "spiral": 0,
                    "goal": goal[:80],
                    "actions": [{"tool": "reflex_text", "result": {
                        "success": True,
                        "output": resp.content,
                    }}],
                    "evaluation": {"success": True, "reason": "Reflex arc direct response"},
                    "success": True,
                    "learned": False,
                }],
                "session_id": self.state.session_id,
                "summary": "Reflex arc: direct text response",
                "reflex": True,
            }

        # ── Execute tool calls ──
        actions = []
        all_safe = True
        for tc in tool_calls:
            tool_name = tc.get("function", {}).get("name", "")
            try:
                params = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                params = {}

            # Basal Ganglia safety check
            safety = self._evaluate_action_safety(tool_name, params)
            if not safety.get("allow", True):
                all_safe = False
                actions.append({
                    "tool": tool_name,
                    "result": {
                        "success": False,
                        "error": f"Reflex blocked by Basal Ganglia: {safety['reason']}",
                        "blocked_by": "basal_ganglia",
                    },
                })
                continue

            # Execute
            result = self.tools.call(tool_name, params)
            actions.append({
                "tool": tool_name,
                "params": params,
                "result": result,
            })

        # Determine success
        success = all_safe and all(
            a.get("result", {}).get("success", False) for a in actions
        )

        return {
            "status": "completed",
            "spirals_completed": 0,
            "results": [{
                "spiral": 0,
                "goal": goal[:80],
                "actions": actions,
                "evaluation": {
                    "success": success,
                    "reason": "Reflex arc: " + ("all actions succeeded" if success else "some actions failed"),
                },
                "success": success,
                "learned": False,
            }],
            "session_id": self.state.session_id,
            "summary": f"Reflex arc: {len(tool_calls)} tool calls, {'success' if success else 'partial failure'}",
            "reflex": True,
        }

    def run(self, goal: str, max_spirals: int = 3, image_path: str = "", reasoning_effort: str = "") -> Dict[str, Any]:
        """
        Execute a goal-driven spiral cycle sequence. 

        Args:
            goal: Goal to be completed (Human language description) 
            max_spirals: Maximum number of spirals to execute
            image_path: Optional path to image file to attach to the goal
            reasoning_effort: DeepSeek reasoning effort level (low/medium/high/xhigh)
        Returns:
            {status, spirals_completed, results, session_id, summary}
        """
        # Set reasoning_effort on LLM client for this run
        if reasoning_effort:
            self.llm.reasoning_effort = reasoning_effort
        # If image_path provided, prepend it to the goal for the LLM
        if image_path:
            goal = f"[Attached image: {image_path}]\n{goal}"
        elif "[Photo received:" in goal:
            # Extract photo path from Telegram format for bridge compatibility
            import re as _re
            m = _re.search(r'\[Photo received:\s*([^\]]+)\]', goal)
            if m:
                image_path = m.group(1).strip()
                if not os.path.exists(os.path.expanduser(image_path)):
                    image_path = ""  # Don't use stale paths
        
        self._log("## Worldwave v0.2 LLM Driven version")
        self._log("## Goal: " + goal)
        self.running = True

        # ── Reflex Arc: fast path for trivially simple tasks ──
        # Skip reflex arc for image/photo tasks — they need vision tools
        is_image_task = "[Photo received:" in goal or "[Attached image:" in goal or image_path
        if self.config.get("reflex_arc_enabled", True) and not is_image_task:
            complexity = self._estimate_complexity(goal)
            threshold = self.config.get("reflex_threshold", self.REFLEX_THRESHOLD)
            if complexity < threshold:
                self._log(f"## ⚡ Reflex Arc — complexity {complexity:.2f} < {threshold}")
                self._log("## Goal classified as trivial, taking fast path...")
                reflex_result = self._reflex_arc_execute(goal)
                if reflex_result is not None:
                    self._log(f"## Reflex arc complete: {reflex_result['summary']}")
                    self.running = False
                    return reflex_result
                self._log("## Reflex arc failed, falling through to full spiral...")

        # Create context session
        session_key = self.state.session_id
        self.conversation.add_message("user", goal, window_id=session_key)

        results = []
        prev_failure = None  # Track previous spiral failure for retry guidance
        for _ in range(max_spirals):
            if not self.running:
                break

            # Check for interruption
            interrupt = self.state.get_last_checkpoint()
            if interrupt:
                self._log("## Interruption detected: " + str(interrupt.interrupt_reason))
                break

            # Start a new spiral
            spiral = self.state.begin_spiral()
            self._log("")
            self._log("### Spiral #" + str(spiral.spiral_number))

            # Subconscious observation: New spiral begins
            self.subconscious.observe_spiral(0, spiral.spiral_number)
            self._log("")

            try:
                # ── 1. Perception ──
                self._log("#### [Perception] Analyzing environment...")
                spiral.perception = self._llm_perceive(goal, prev_failure=prev_failure)
                self.state.set_phase("perceive")
                obs = spiral.perception.get("observations", [])
                for o in obs[:3]:
                    self._log("  - " + str(o)[:80])

                # ── 1.5 GATE (Thalamus) — Workspace submission + attention filtering ──
                self._log("#### [Gate] Filtering through workspace...")
                self._gate_perceptions(spiral.perception, goal)

                # Update circadian rhythm metrics
                metrics = collect_system_metrics(
                    active_tasks=len(spiral.plan.get("steps", [])) if spiral.plan else 0,
                    last_user_interaction=time.time(),
                    stress_signal=self.cascade.current_stress_level(),
                    context_pressure=(
                        self.memory.get_context_pressure() if self.memory else 0.0
                    ),
                )
                self.circadian.update_metrics(metrics)

                # Depth checkpoint: Perception phase completed
                self._steps_total = len(spiral.plan.get("steps", [])) if spiral.plan else 0
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=spiral.spiral_number,
                    phase="perceive",
                    scratchpad=json.dumps(spiral.perception, ensure_ascii=False)[:2000],
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=spiral.spiral_number, phase="perceive",
                        steps_total=self._steps_total,
                        tool_history=self._tool_history,
                    ),
                )

                # ── 2. Recall ──
                self._log("#### [Recall] Recalling relevant memories...")
                spiral.recall = self._llm_recall(spiral.perception, goal)
                self.state.set_phase("recall")
                mem_count = len(spiral.recall.get("memories", []))
                self._log("  -> " + str(mem_count) + " memories recalled")

                # Depth checkpoint: Recall phase completed
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=spiral.spiral_number,
                    phase="recall",
                    scratchpad=json.dumps({"recall_query": spiral.recall.get("query",""), "memories": len(spiral.recall.get("memories",[]))}, ensure_ascii=False)[:2000],
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=spiral.spiral_number, phase="recall",
                        steps_total=self._steps_total,
                        tool_history=self._tool_history,
                    ),
                )

                # ── 3. Plan ──
                self._log("#### [Plan] Planning actions...")
                spiral.plan = self._llm_plan(spiral.perception, spiral.recall, goal)
                self.state.set_phase("plan")
                steps = spiral.plan.get("steps", [])
                self._log("  -> " + str(len(steps)) + " steps")
                for i, s in enumerate(steps[:5]):
                    self._log("    " + str(i+1) + ". " + s.get("description", s.get("tool", "?")))

                # Depth checkpoint: Planning phase completed
                steps = spiral.plan.get("steps", [])
                self._steps_total = len(steps)
                self._steps_completed = 0
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=spiral.spiral_number,
                    phase="plan",
                    scratchpad=json.dumps({"strategy": spiral.plan.get("strategy",""), "steps": [s.get("tool","") for s in steps]}, ensure_ascii=False)[:2000],
                    plan_tree={"strategy": spiral.plan.get("strategy",""), "steps": len(steps), "criteria": spiral.plan.get("success_criteria","")},
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=spiral.spiral_number, phase="plan",
                        steps_total=self._steps_total,
                        tool_history=self._tool_history,
                    ),
                )

                # ── 4. Action ──
                self._log("#### [Action] Executing...")
                spiral.actions = self._llm_act(spiral.plan, goal)
                self.state.set_phase("act")
                self._log("  -> " + str(len(spiral.actions)) + " actions executed")

                # Subconscious observation: Record each action
                for a in spiral.actions:
                    r = a.get("result", {})
                    self.subconscious.observe_action(
                        tool_name=a.get("tool", a.get("error", "?")),
                        success=r.get("success", False),
                        latency=r.get("latency", 0.0),
                    )

                # Check respond Whether the action was successful (Plain text response, No tools needed) 
                respond_outputs = [a for a in spiral.actions
                                   if a.get("tool") == "respond" and a.get("result", {}).get("success")]
                if respond_outputs:
                    output = respond_outputs[0]["result"]["output"]
                    self._log("  => Text response: " + output[:120])
                    # respond Success = Goal achieved
                    spiral.evaluation = {
                        "success": True,
                        "reason": "Direct response generated",
                        "lessons_learned": [],
                        "goal_remaining": False,
                        "next_action": "stop",
                        "response": output,
                    }
                    self.state.complete_spiral()
                    results.append({
                        "spiral": spiral.spiral_number,
                        "goal": goal[:80],
                        "steps": spiral.plan.get("steps", []),
                        "actions": spiral.actions,
                        "evaluation": spiral.evaluation,
                        "success": True,
                        "learned": False,
                    })
                    self._log("## Goal achieved - Direct response!")
                    break

                # Depth checkpoint: Action phase completed (Include the result history of each tool) 
                self._steps_completed = len(spiral.actions)
                self._tool_history = []
                for a in spiral.actions:
                    r = a.get("result", {})
                    self._tool_history.append({
                        "tool": a.get("tool", "?"),
                        "success": r.get("success", False),
                        "output_preview": str(r.get("output", ""))[:200],
                        "error": r.get("error", ""),
                    })
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=spiral.spiral_number,
                    phase="act",
                    step_number=self._steps_completed,
                    step_total=self._steps_total,
                    scratchpad=json.dumps({"actions_count": len(spiral.actions), "successes": sum(1 for a in spiral.actions if a.get("result",{}).get("success",False))}, ensure_ascii=False)[:2000],
                    tool_history=self._tool_history,
                    partial_results={f"step_{i}": a.get("result",{}) for i, a in enumerate(spiral.actions)},
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=spiral.spiral_number, phase="act",
                        steps_completed=self._steps_completed, steps_total=self._steps_total,
                        tool_history=self._tool_history,
                    ),
                )

                # ── 5. Evaluate ──
                self._log("#### [Evaluate] Evaluating results...")
                spiral.evaluation = self._llm_evaluate(spiral.plan, spiral.actions, goal)
                self.state.set_phase("evaluate")
                ev = spiral.evaluation
                self._log("  -> success=" + str(ev.get("success", "?")) + ", " + ev.get("reason", "")[:80])

                # Subconscious: Feed context metrics into feature vector
                if self.memory:
                    self.subconscious.feature_extractor.set_context_window_pressure(
                        self.memory.get_context_pressure())
                    self.subconscious.feature_extractor.set_memory_conflict_rate(
                        self.memory.get_memory_conflict_rate())

                # Subconscious: Record training samples based on evaluation results
                outcome = 0.0 if ev.get("success", False) else 1.0
                vec = self.subconscious.feature_extractor.extract(
                    spirals_completed=spiral.spiral_number,
                )
                self.subconscious.record_training_sample(vec, outcome)

                # Subconscious intervention check (4-tier)
                intervention = self.subconscious.should_intervene()
                tier = intervention.get("action", "noop")

                if intervention["intervene"] and tier == "rewind":
                    self._log("  🧠 [Subconscious] Tier 4 — Rewind: " + intervention["reason"])
                    self.state.interrupt("rewind: " + intervention["reason"])
                    self.subconscious.execute_rewind(
                        reason=intervention["reason"],
                        state_vector=intervention.get("state_vector", []),
                        risk=intervention.get("risk", 0.0),
                    )
                    # Inject guardrail into system prompt context
                    guardrail = getattr(self.subconscious, '_latest_guardrail', '')
                    if guardrail:
                        self.state.global_context["subconscious_guardrail"] = guardrail
                    break

                elif intervention["intervene"] and tier == "tool_downgrade":
                    # Tier 2: Restrict tool access when anomaly detected
                    guideline = intervention.get("guideline", intervention.get("reason", ""))
                    self._log("  🧠 [Subconscious] Tier 2 — Tool downgrade: " + guideline[:60])
                    self.state.global_context["subconscious_downgrade"] = guideline
                    # Mark tools safe set for next spiral
                    self.state.global_context["subconscious_downgrade_active"] = True

                elif intervention["intervene"] and tier == "mode_switch":
                    # Tier 3: Cognitive mode switch
                    guideline = intervention.get("guideline", intervention.get("reason", ""))
                    self._log("  🧠 [Subconscious] Tier 3 — Mode switch: " + guideline[:60])
                    self.state.global_context["subconscious_mode"] = guideline

                elif intervention["intervene"] and tier == "compress":
                    # Tier 1: Context compression alert
                    guideline = intervention.get("guideline", intervention.get("reason", ""))
                    self._log("  🧠 [Subconscious] Tier 1 — Compression: " + guideline[:60])
                    self.state.global_context["subconscious_compress"] = guideline

                elif intervention["intervene"] and tier == "warn":
                    # Existing warning mechanism
                    self._log("  🧠 [Subconscious] Warning: Risk " + str(intervention.get("risk", 0)))
                    self.state.global_context["subconscious_warning"] = (
                        intervention.get("reason", "")
                    )
                elif intervention["intervene"] and tier == "interrupt":
                    self._log("  🧠 [Subconscious] Plugin interrupt: " + intervention.get("reason", "")[:60])
                    self.state.interrupt("plugin_interrupt: " + intervention.get("reason", ""))
                    break

                # Depth checkpoint: Evaluation phase completed
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=spiral.spiral_number,
                    phase="evaluate",
                    scratchpad=json.dumps({"success": spiral.evaluation.get("success", False), "reason": spiral.evaluation.get("reason","")[:200]}, ensure_ascii=False)[:2000],
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=spiral.spiral_number, phase="evaluate",
                        steps_completed=self._steps_completed, steps_total=self._steps_total,
                        tool_history=self._tool_history,
                        extra={"evaluation": spiral.evaluation.get("reason","")},
                    ),
                )

                # ── 6. Learn ──
                self._log("#### [Learn] Codifying experience...")
                spiral.learning = self._llm_learn(spiral, goal)
                self.state.set_phase("learn")
                stored = spiral.learning.get("stored", False)
                self._log("  -> " + ("stored" if stored else "skipped"))

                # Depth checkpoint: Learning phase completed (Spiral completed) 
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=spiral.spiral_number,
                    phase="learn",
                    scratchpad=json.dumps({"learned": spiral.learning.get("stored", False), "importance": spiral.learning.get("importance", 0)}, ensure_ascii=False)[:2000],
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=spiral.spiral_number, phase="learn",
                        steps_completed=self._steps_completed, steps_total=self._steps_total,
                        tool_history=self._tool_history,
                    ),
                )
                # Update session Status
                self.checkpoint_db.update_session(
                    self.state.session_id,
                    spirals_completed=spiral.spiral_number,
                    metadata=json.dumps({"last_goal": goal[:200], "last_phase": "learn", "last_status": "completed"}),
                )

                # Context compression: Add this spiral summary to the context window
                ctx_summary = spiral.evaluation.get("reason", "")
                if ctx_summary:
                    self.conversation.add_message(
                        "assistant",
                        f"Spiral #{spiral.spiral_number} complete: {ctx_summary[:200]}",
                        window_id=self.state.session_id,
                    )

                # Complete spiral
                self.state.complete_spiral()
                results.append({
                    "spiral": spiral.spiral_number,
                    "goal": goal[:80],
                    "steps": steps,
                    "actions": spiral.actions,
                    "evaluation": spiral.evaluation,
                    "success": spiral.evaluation.get("success", False),
                    "learned": stored,
                })

                # Check whether the goal has been achieved
                if self._goal_achieved(spiral):
                    self._log("## Goal achieved!")
                    break
                else:
                    # Build failure context for retry — tell next spiral what went wrong
                    failed_steps = [a for a in spiral.actions
                                    if not a.get("result", {}).get("success", False)]
                    if failed_steps:
                        prev_failure = json.dumps([{
                            "tool": a.get("tool", "?"),
                            "error": a.get("result", {}).get("error", "unknown"),
                            "step_desc": a.get("description", ""),
                        } for a in failed_steps], indent=2, ensure_ascii=False)
                    else:
                        # No explicit failures but goal not achieved — pass the evaluation reason
                        prev_failure = spiral.evaluation.get("reason", "Goal not achieved")

            except KeyboardInterrupt:
                self._log("## Manual pause")
                self.state.interrupt("user_interrupt", resume_data={"goal": goal})
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=self.state.current_spiral,
                    phase=self.state.current_phase,
                    scratchpad=f"KeyboardInterrupt at spiral {self.state.current_spiral}, phase {self.state.current_phase}",
                    interrupted=True,
                    interrupt_reason="user_interrupt",
                    resume_data={"goal": goal},
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=self.state.current_spiral, phase=self.state.current_phase,
                        steps_completed=self._steps_completed, steps_total=self._steps_total,
                        tool_history=self._tool_history,
                    ),
                )
                break
            except Exception as e:
                self._log("## Error: " + str(e))
                self.state.interrupt("error: " + str(e))
                self.checkpoint_db.save_checkpoint(
                    session_id=self.state.session_id,
                    spiral_number=self.state.current_spiral,
                    phase=self.state.current_phase,
                    scratchpad=f"Error: {str(e)}",
                    interrupted=True,
                    interrupt_reason="error: " + str(e),
                    resume_data={"goal": goal},
                    context_snapshot=build_context_snapshot(
                        goal=goal, spiral_number=self.state.current_spiral, phase=self.state.current_phase,
                        steps_completed=self._steps_completed, steps_total=self._steps_total,
                        extra={"error": str(e)},
                    ),
                )
                break

        self.running = False

        # ── Structured log ──
        spirals_used = self.state.current_spiral
        success_count = sum(1 for r in results if r.get("success"))
        try:
            self._wwlog.info("loop.run",
                             f"Task '{goal[:60]}' completed: {len(results)} spirals, {success_count} successful",
                             data={"goal": goal[:100], "spirals": spirals_used,
                                   "results_count": len(results), "success_count": success_count,
                                   "session_id": self.state.session_id},
                             session_id=self.state.session_id)
        except Exception as log_err:
            self._log(f"## Log error (Non-critical): {log_err}")

        # ── Automatically audit after task completion ──
        try:
            last_cp = self.state.get_last_checkpoint()
            is_interrupted = last_cp and not (last_cp.interrupt_reason or "").startswith("rewind:")
            evolution_result = self.evolution.full_cycle({
                "status": "interrupted" if is_interrupted else "completed",
                "spirals_completed": self.state.current_spiral,
                "results": results,
            })
            if evolution_result.get("evolved"):
                self._log("## 🧬 Self-evolution: " + evolution_result["message"])
                for c in evolution_result.get("changes", []):
                    self._log("   " + c.get("detail", "")[:80])
                # Push to Telegram (If available) 
                self._notify_evolution(evolution_result)
        except Exception as e:
            self._log("## Audit failure (Non-critical): " + str(e))
        
        return {
            "status": "interrupted" if (self.state.get_last_checkpoint() and 
                       not (self.state.get_last_checkpoint().interrupt_reason or "").startswith("rewind:")) 
                       else "completed",
            "spirals_completed": len(results),
            "results": results,
            "session_id": self.state.session_id,
            "summary": self.state.summary(),
        }

    # ════════════════════════════════════════════════════════
    #  Biomimetic stage processors (v0.6)
    # ════════════════════════════════════════════════════════

    def _gate_perceptions(self, perception: Dict, goal: str):
        """Thalamus-inspired GATE stage: submit perceptions to global workspace.

        High-priority observations enter the workspace;
        low-priority ones are filtered out.
        """
        observations = perception.get("observations", [])
        uncertainties = perception.get("uncertainties", [])
        key_signals = perception.get("key_signals", [])

        # Submit key signals with high priority
        for signal in key_signals[:3]:
            self.workspace.submit(
                str(signal)[:200],
                source="perception",
                urgency=0.7,
                relevance=0.6,
                novelty=0.4,
            )

        # Submit observations with computed priority
        for obs in observations[:5]:
            # Compute urgency from observation content
            urgency = 0.3  # Base
            obs_str = str(obs).lower()
            if any(w in obs_str for w in ["error", "fail", "crash", "critical"]):
                urgency = 0.9
            elif any(w in obs_str for w in ["warning", "issue", "problem"]):
                urgency = 0.6

            self.workspace.submit(
                str(obs)[:200],
                source="perception",
                urgency=urgency,
                relevance=0.5,
            )

        # Submit uncertainties (potential threats)
        for uncertainty in uncertainties[:2]:
            self.workspace.submit(
                str(uncertainty)[:200],
                source="uncertainty",
                urgency=0.5,
                relevance=0.4,
            )

        # Apply time-based decay to workspace
        self.workspace.apply_decay()

        # Update amygdala stress cascade
        total_urgency = sum(
            0.9 if any(w in str(o).lower() for w in ["error", "fail", "crash"])
            else 0.3
            for o in observations[:5]
        ) / max(1, len(observations[:5]))
        self.cascade.emit_stress(
            level=total_urgency,
            source="perception",
            reason=f"Observation analysis: {len(observations)} items",
        )

    def _evaluate_action_safety(self, tool_name: str,
                                 params: Dict = None) -> Dict[str, Any]:
        """Basal Ganglia: evaluate action safety before execution.

        Runs the proposed tool through the G/N dual pathway.
        Dangerous actions are blocked at the subcortical level.
        """
        category = self.basal_ganglia.classify_action(tool_name)

        # Build state vector from current context
        state = [0.0] * 32
        # Feature 0: action category (normalized)
        action_categories = {
            "safe_read": 0, "safe_info": 1, "modify_local": 2,
            "modify_remote": 3, "delete": 4, "system": 5, "unsafe": 6,
        }
        state[0] = action_categories.get(category, 2) / 6.0
        # Feature 1: stress level from amygdala
        state[1] = self.cascade.current_stress_level()
        # Feature 2: spiral number (normalized)
        state[2] = min(1.0, self.state.current_spiral / 10.0)

        result = self.basal_ganglia.evaluate_action(
            state=state,
            action_category=category,
            action_description=tool_name,
        )
        return result

    def _tool_domain(self, tool_name: str) -> str:
        """Map tool name to prediction domain."""
        t = tool_name.lower()
        if any(w in t for w in ["shell", "terminal", "bash", "sh", "cmd"]):
            return "shell"
        if any(w in t for w in ["read", "write", "patch", "file", "search_files"]):
            return "file"
        if any(w in t for w in ["web", "api", "http", "curl", "fetch", "request"]):
            return "api"
        if any(w in t for w in ["system", "process", "kill", "service", "systemctl"]):
            return "system"
        return "shell"

    # ════════════════════════════════════════════════════════
    #  LLM Driven stage processor
    # ════════════════════════════════════════════════════════

    def _llm_perceive(self, goal: str, prev_failure: Optional[str] = None) -> Dict[str, Any]:
        """LLM Perception: Analyze goal, Collect environmental information. Load relevant Skills. """
        env = self._get_environment_state()
        
        # Build failure context if this is a retry
        failure_block = ""
        if prev_failure:
            failure_block = (
                "⚠️ Previous attempt FAILED. Do NOT repeat these approaches:\n"
                + prev_failure
                + "\n\nUsing a different approach is required.\n\n"
            )
        # Load relevant Skills (Procedural memory) 
        relevant_skills = self.skills.find_relevant(goal, max_results=5)
        skills_block = ""
        if relevant_skills:
            skills_lines = ["Relevant skill reference:"]
            for s in relevant_skills:
                skills_lines.append(s.context_block())
            skills_block = "\n\n".join(skills_lines)
        
        user_msg = (
            "Goal: " + goal + "\n\n"
            "Current environment:\n" + json.dumps(env, indent=2) + "\n\n"
            + (failure_block if failure_block else "")
            + (skills_block + "\n\n" if skills_block else "")
            + "Please analyze the goal and the environment, Output your perception results (JSON) . "
        )
        try:
            result = self.llm.chat_json(
                [{"role": "user", "content": user_msg}],
                phase="perceive",
            )
            return result
        except Exception as e:
            return {
                "observations": ["LLM perceive failed: " + str(e)],
                "key_signals": [],
                "environment_summary": "error",
                "uncertainties": ["LLM unavailable"],
            }

    def _llm_recall(self, perception: Dict, goal: str) -> Dict[str, Any]:
        """LLM Guided memory recall. """
        obs_text = "\n".join(perception.get("observations", []))[:500]
        user_msg = (
            "Current observation:\n" + obs_text + "\n\n"
            "Goal: " + goal + "\n\n"
            "Based on the above perception, What relevant experiences should I recall from memory？Output JSON. "
        )
        try:
            llm_result = self.llm.chat_json(
                [{"role": "user", "content": user_msg}],
                phase="recall",
            )
            query = llm_result.get("query", goal)
            entities = llm_result.get("entities", [])

            # Actually query memory v2
            if self.memory is not None:
                memories = self._recall_memory(query, limit=5)
            else:
                memories = []

            return {
                "query": query,
                "entities": entities,
                "memories": memories,
                "llm_insight": llm_result.get("aspect", ""),
            }
        except Exception as e:
            return {"query": goal, "entities": [], "memories": [], "error": str(e)}

    def _llm_plan(self, perception: Dict, recall: Dict, goal: str) -> Dict[str, Any]:
        """LLM Formulate a plan (Include explicit tool invocation steps) . """
        context = (
            "Goal: " + goal + "\n\n"
            "Perception summary: " + str(perception.get("environment_summary", ""))[:300] + "\n\n"
            "Recall: " + str(len(recall.get("memories", []))) + " Related memories\n\n"
            "Available tools:\n" + self.tools.prompt_block() + "\n\n"
            "Please formulate an executable plan based on the goal. Each step Must specify the used tool  and  params. "
        )

        # Inject subconscious warnings and guidelines
        warning = self.state.global_context.get("subconscious_warning", "")
        compress = self.state.global_context.get("subconscious_compress", "")
        downgrade = self.state.global_context.get("subconscious_downgrade", "")
        mode = self.state.global_context.get("subconscious_mode", "")
        
        if warning:
            context += (
                "\n\n⚠️ Subconscious prompt:\n"
                + warning
                + "\n\nPlease avoid repeating the above failure patterns. "
            )
            self.state.global_context["subconscious_warning"] = ""  # Clear after use
        
        if compress:
            context += (
                "\n\n📦 Subconscious context note:\n"
                + compress
                + "\n\nBe concise and avoid expanding the context unnecessarily. "
            )
            self.state.global_context["subconscious_compress"] = ""  # Clear after use

        if downgrade:
            context += (
                "\n\n🔧 Subconscious tool note:\n"
                + downgrade
                + "\n\nSome tools may be restricted. Focus on the core task with available tools. "
            )
            # Downgrade persists until cleared by success
            # Filter out dangerous tool categories
            downgrade_categories = ["code"]
            context = context.replace(
                "Available tools:\n" + self.tools.prompt_block(),
                "Available tools:\n" + self.tools.prompt_block(exclude_categories=downgrade_categories),
            )

        if mode:
            context += (
                "\n\n🧠 Subconscious mode guidance:\n"
                + mode
                + "\n"
            )
            self.state.global_context["subconscious_mode"] = ""  # Clear after use

        guardrail = self.state.global_context.get("subconscious_guardrail", "")
        if guardrail:
            context += (
                "\n\n🛡️ Subconscious guardrail:\n"
                + guardrail
                + "\n"
            )
            self.state.global_context["subconscious_guardrail"] = ""  # Clear after use
        try:
            result = self.llm.chat_json(
                [{"role": "user", "content": context}],
                phase="plan",
            )
            # Ensure steps Exists
            if "steps" not in result:
                result["steps"] = []
            return result
        except Exception as e:
            return {
                "goal": goal,
                "strategy": "fallback",
                "steps": [{"tool": "shell", "params": {"command": "echo 'LLM planning failed: " + str(e) + "'"}, "description": "fallback"}],
                "success_criteria": "none",
            }

    def _llm_act(self, plan: Dict, goal: str = "") -> List[Dict]:
        """Execute plan steps. """
        steps = plan.get("steps", [])
        results = []
        step_outputs: Dict[int, str] = {}  # step index → resolved output string

        def _resolve_template(val, step_outputs):
            """Replace {{stepN_output}}/{{stepN_result}} with actual output from step N."""
            if not isinstance(val, str):
                return val
            def _replacer(m):
                idx = int(m.group(1)) - 1  # convert 1-based → 0-based
                key = m.group(2)  # output or result or full result dict
                prev = step_outputs.get(idx)
                if prev is None:
                    return m.group(0)  # leave unchanged if step not executed yet
                if key == "full":
                    return prev
                # try output key first, then result key, then the whole thing
                try:
                    parsed = json.loads(prev)
                    if isinstance(parsed, dict):
                        return str(parsed.get("output", parsed.get("result", prev)))
                except (json.JSONDecodeError, TypeError):
                    pass
                return prev.strip()
            return re.sub(r"\{\{step(\d+)_(output|result|full)\}\}", _replacer, val)

        def _resolve_params(params, step_outputs):
            """Recursively resolve templates in params dict."""
            if isinstance(params, dict):
                return {k: _resolve_params(v, step_outputs) for k, v in params.items()}
            if isinstance(params, list):
                return [_resolve_params(v, step_outputs) for v in params]
            if isinstance(params, str):
                return _resolve_template(params, step_outputs)
            return params

        for i, step in enumerate(steps):
            tool_name = step.get("tool", "")
            params = _resolve_params(step.get("params", {}), step_outputs)
            desc = step.get("description", tool_name)

            self._log("  [" + str(i+1) + "/" + str(len(steps)) + "] " + desc[:60])

            # Special handling: respond — Use directly LLM Response, Do not use tools
            if tool_name == "respond":
                prompt = params.get("prompt", params.get("content", desc))
                
                # If previous steps gathered data, include tool outputs verbatim
                tool_data = ""
                if step_outputs:
                    context_lines = []
                    for si, sv in step_outputs.items():
                        try:
                            parsed = json.loads(sv)
                            out = parsed.get("output", str(parsed)[:500])
                        except (json.JSONDecodeError, TypeError):
                            out = str(sv)[:500]
                        if out and out.strip():
                            context_lines.append(out)
                    if context_lines:
                        tool_data = "\n".join(context_lines)
                
                try:
                    if tool_data:
                        # Tool outputs exist — enforce strict transcription
                        full_prompt = (
                            f"Original goal: {goal}\n\n"
                            f"Tool outputs:\n{tool_data}\n\n"
                            f"Answer using ONLY the data above. Quote facts verbatim. "
                            f"If data is insufficient, say what you found and what's missing. "
                            f"NEVER add details not in the tool outputs."
                        )
                        response_text = self.llm.chat(
                            messages=[
                                {"role": "system", "content": "You are a strict transcriber. You may rephrase for clarity but must NOT add any fact, variable name, command, or mechanism not explicitly present in the tool outputs above."},
                                {"role": "user", "content": full_prompt},
                            ],
                            json_mode=False,
                            max_tokens=1024,
                        )
                    else:
                        # No tool outputs — simple conversational respond
                        full_prompt = f"Original goal: {goal}\n\nResponse instruction: {prompt}"
                        response_text = self.llm.chat(
                            [{"role": "user", "content": full_prompt}],
                            json_mode=False,
                            max_tokens=1024,
                        )
                    step_result = {
                        "step": i,
                        "tool": "respond",
                        "description": desc,
                        "result": {"success": True, "output": response_text},
                    }
                except Exception as e:
                    step_result = {
                        "step": i,
                        "tool": "respond",
                        "description": desc,
                        "result": {"success": False, "error": str(e)},
                    }
                results.append(step_result)
                step_outputs[i] = json.dumps(step_result.get("result", {}))
                continue

            # Special handling: question — Ask the user a question (Temporarily store, The cycle will process) 
            if tool_name == "question":
                content = params.get("content", params.get("question", desc))
                self._pending_question = content
                step_result = {
                    "step": i,
                    "tool": "question",
                    "description": desc,
                    "result": {"success": True, "output": "[QUESTION: " + content[:100] + "]"},
                }
                results.append(step_result)
                step_outputs[i] = json.dumps(step_result.get("result", {}))
                continue

            if tool_name:
                # ── Basal Ganglia safety evaluation (subcortical) ──
                safety = self._evaluate_action_safety(tool_name, params)
                if not safety.get("allow", True):
                    self._log(f"    🛡️ BLOCKED by Basal Ganglia: {safety['reason']}")
                    step_result = {
                        "step": i,
                        "tool": tool_name,
                        "params": params,
                        "description": desc,
                        "result": {
                            "success": False,
                            "error": f"Action blocked by safety system: {safety['reason']}",
                            "blocked_by": "basal_ganglia",
                            "g_score": safety.get("g_score"),
                            "n_score": safety.get("n_score"),
                        },
                    }
                    results.append(step_result)
                    step_outputs[i] = json.dumps(step_result.get("result", {}))
                    # Learn from blocked action
                    self.basal_ganglia.learn_from_outcome(
                        state=[0.0] * 32,
                        action_category=self.basal_ganglia.classify_action(tool_name),
                        success=False,
                        error_description=safety["reason"],
                    )
                    continue

                result = self.tools.call(tool_name, params)
                step_result = {
                    "step": i,
                    "tool": tool_name,
                    "params": params,
                    "description": desc,
                    "result": result,
                }
                results.append(step_result)
                step_outputs[i] = json.dumps(step_result.get("result", {}))

                # ── Cerebellum: predictive model verification ──
                prediction = self.predictive_model.predict(
                    domain=self._tool_domain(tool_name),
                    action=tool_name,
                    params=params if isinstance(params, dict) else {},
                )
                delta = self.predictive_model.verify(
                    prediction=prediction,
                    actual_success=result.get("success", False),
                    actual_output=str(result.get("output", ""))[:500],
                    actual_exit_code=result.get("exit_code", 0),
                )
                if delta and delta.correctable:
                    self._log(f"    🔧 Cerebellum correction: {delta.correction_action}")

                # ── Cerebellum: skill solidification observation ──
                self.skill_solidifier.observe(
                    domain=self._tool_domain(tool_name),
                    action=tool_name,
                    params=params if isinstance(params, dict) else {},
                    success=result.get("success", False),
                    output=str(result.get("output", ""))[:500],
                    latency=0.0,
                )

                # ── Basal Ganglia: learn from outcome ──
                bg_state = [0.0] * 32
                bg_state[0] = {"safe_read": 0, "safe_info": 1, "modify_local": 2,
                               "modify_remote": 3, "delete": 4, "system": 5, "unsafe": 6}.get(
                    self.basal_ganglia.classify_action(tool_name), 2) / 6.0
                bg_state[1] = self.cascade.current_stress_level()
                self.basal_ganglia.learn_from_outcome(
                    state=bg_state,
                    action_category=self.basal_ganglia.classify_action(tool_name),
                    success=result.get("success", False),
                    error_description=result.get("error", ""),
                )

                if not result.get("success", False):
                    self._log("    ! Failure: " + result.get("error", "unknown")[:80])
                # Not specified tool → Attempt to write code Execute
                code = step.get("code", "")
                if code:
                    result = self.tools.call("code", {"code": code})
                    step_result = {
                        "step": i,
                        "tool": "code",
                        "code": code[:100],
                        "description": desc,
                        "result": result,
                    }
                    results.append(step_result)
                    step_outputs[i] = json.dumps(step_result.get("result", {}))
                else:
                    results.append({
                        "step": i,
                        "error": "no_tool_or_code",
                        "description": desc,
                    })

        return results

    def _llm_evaluate(self, plan: Dict, actions: List[Dict], goal: str) -> Dict[str, Any]:
        """LLM Evaluate execution results. """
        act_summary = []
        for a in actions[:5]:
            r = a.get("result", {})
            act_summary.append({
                "tool": a.get("tool", "?"),
                "success": r.get("success", False),
                "output_preview": r.get("output", "")[:100],
            })

        context = (
            "Goal: " + goal + "\n\n"
            "Planning strategy: " + plan.get("strategy", "") + "\n\n"
            "Execution result:\n" + json.dumps(act_summary, indent=2) + "\n\n"
            "Please evaluate whether the goal has been achieved. Output JSON. "
        )
        try:
            return self.llm.chat_json(
                [{"role": "user", "content": context}],
                phase="evaluate",
            )
        except Exception as e:
            return {
                "success": False,
                "reason": "LLM evaluate failed: " + str(e),
                "lessons_learned": [],
                "goal_remaining": True,
                "next_action": "stop",
            }

    def _llm_learn(self, spiral: SpiralState, goal: str) -> Dict[str, Any]:
        """LLM Driven learning: Extract experiences and store them in memory. """
        spiral_summary = {
            "goal": goal,
            "perception": list(spiral.perception.get("observations", []))[:3],
            "plan": spiral.plan.get("strategy", ""),
            "actions_count": len(spiral.actions),
            "evaluation": spiral.evaluation.get("reason", ""),
        }
        context = (
            "The following is a summary of the just-completed cognitive cycle:\n"
            + json.dumps(spiral_summary, indent=2) + "\n\n"
            "Please extract lessons and insights worth storing. Output JSON. "
        )
        try:
            lesson = self.llm.chat_json(
                [{"role": "user", "content": context}],
                phase="learn",
            )
            content = lesson.get("content", "")
            entities = lesson.get("entities", [goal])
            importance = lesson.get("importance", 0.5)

            if content and importance >= 0.3:
                mid = self._store_memory(
                    content=content,
                    source="ww_loop",
                    entities=entities,
                )
                
                # Store only to the memory system, Do not automatically create skill File
                # skill The file is human-curated procedural knowledge, Auto-generated noise outweighs value
                return {
                    "stored": mid is not None,
                    "memory_id": mid,
                    "importance": importance,
                    "content_preview": content[:100],
                    "entities": entities,
                }
            return {
                "stored": False,
                "reason": "low_importance",
                "importance": importance,
            }
        except Exception as e:
            return {"stored": False, "error": str(e)}

    # ════════════════════════════════════════════════════════
    #  Auxiliary method
    # ════════════════════════════════════════════════════════

    def start_autonomous(self, interval: int = 300, max_spirals_per_cycle: int = 3):
        """
        Autonomous mode: Continuous cycle. 
        interval: Interval between each cycle ( seconds) 
        """
        self._log("## Autonomous mode activated (interval=" + str(interval) + "s)")
        self.running = True
        while self.running:
            goal = self._generate_goal()
            self.run(goal, max_spirals=max_spirals_per_cycle)
            if self.running:
                self._log("## Wait " + str(interval) + "s...")
                time.sleep(interval)

    def stop(self):
        """Stop cycle. """
        self.running = False
        self._log("## Stopped")

    def _get_environment_state(self) -> Dict[str, Any]:
        """Get system status. """
        try:
            load = os.popen("uptime 2>/dev/null").read().strip()[:80]
            mem = os.popen("free -h 2>/dev/null | head -2").read().strip()[:100]
            return {
                "hostname": os.uname().nodename,
                "uptime": load,
                "memory": mem,
                "memory_available": self.memory is not None,
                "tools": self.tools.tool_names(),
            }
        except:
            return {"info": "unavailable"}

    def _goal_achieved(self, spiral: SpiralState) -> bool:
        """Check whether the goal has been achieved. """
        ev = spiral.evaluation
        if isinstance(ev, dict):
            success = ev.get("success", False)
            remaining = ev.get("goal_remaining", True)
            return success is True and remaining is False
        return False

    def _notify_evolution(self, evolution_result: Dict) -> None:
        """Push evolution results to Telegram (Non-critical, Ignore failure) . """
        try:
            token = os.environ.get("TELEGRAM_WW_TOKEN", "")
            workspace = os.environ.get("TELEGRAM_WW_WORKSPACE", "")
            if not token or not workspace:
                return
            msg = evolution_result.get("message", "WW Completed self-evolution")
            changes = evolution_result.get("changes", [])
            detail = ""
            for c in changes[:5]:
                d = c.get("detail", "")[:100]
                if d:
                    detail += "\n• " + d
            text = f"🧬 **WW Self-evolution**\n\n{msg}\n{detail}"
            import urllib.request
            payload = json.dumps({
                "chat_id": workspace,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }).encode()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # Telegram Push failure does not affect the main flow

    def _generate_goal(self) -> str:
        """Generate goals in autonomous mode. """
        env = self._get_environment_state()
        prompt = (
            "Current system status:\n" + json.dumps(env, indent=2) + "\n\n"
            "As a self-disciplined and autonomous AI Agent, What should you focus on at this moment？Generate a specific check/Explore goal. "
        )
        try:
            result = self.llm.chat_json(
                [{"role": "user", "content": prompt}],
                phase="",  # No need phase prompt
                temperature=0.8,
            )
            return result.get("goal", "Check the basic health status of the system")
        except Exception:
            return "Check the basic health status of the system"

    def _log(self, msg: str):
        if self.verbose:
            ts = datetime.now().strftime("%H:%M:%S")
            print("[" + ts + "] " + msg)


def create_ww(
    model: str = "deepseek/deepseek-v4-flash",
    persist_dir: str = "",
) -> Worldwave:
    """Quickly establish a Worldwave Instance. """
    return Worldwave(
        model=model,
        persist_dir=persist_dir,
    )


# Alias for external consumers  
SpiralLoop = Worldwave
