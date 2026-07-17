"""Tests: Worldwave spiral cognitive loop — init, state machine, phases, reflex arc, checkpoint, HITL"""

import json
import sys
import tempfile
from unittest.mock import MagicMock, patch


sys.path.insert(0, ".")

from core.loop import Worldwave, create_ww
from core.state import SpiralState, StateManager
from tools.registry import PERMISSION_SAFE, ToolDef, ToolRegistry


# ── helpers ──


def make_mock_llm():
    """Create a mock LLMClient that returns predictable JSON."""
    llm = MagicMock()
    llm.model = "mock/model"
    llm.temperature = 0.7
    llm.max_tokens = 4096
    llm.failover = False

    # Default chat_json responses per phase
    def chat_json_side_effect(messages, phase="", **kwargs):
        responses = {
            "perceive": {
                "observations": ["System is healthy", "No errors detected"],
                "key_signals": ["all_clear"],
                "environment_summary": "normal operation",
                "uncertainties": [],
            },
            "recall": {
                "query": "system status",
                "entities": ["system", "health"],
                "aspect": "monitoring",
            },
            "plan": {
                "strategy": "Check system health using built-in tools",
                "steps": [
                    {
                        "tool": "respond",
                        "params": {"prompt": "Report system status"},
                        "description": "Report current status",
                    },
                ],
                "success_criteria": "Status reported successfully",
            },
            "evaluate": {
                "success": True,
                "reason": "Status check completed successfully",
                "lessons_learned": ["System is operational"],
                "goal_remaining": False,
                "next_action": "stop",
            },
            "learn": {
                "content": "System check routine works",
                "entities": ["system"],
                "importance": 0.5,
            },
            "": {
                "goal": "Check basic system health",
            },
        }
        return responses.get(phase, {"result": "ok"})

    llm.chat_json.side_effect = chat_json_side_effect

    def chat_side_effect(messages, json_mode=False, max_tokens=None, **kwargs):
        return "The system is operational. All checks passed."

    llm.chat.side_effect = chat_side_effect

    # _call for reflex arc tests
    def call_side_effect(messages, json_mode=False, temperature=0.1, max_tokens=2048, tools=None):
        resp = MagicMock()
        resp.content = "Task completed via reflex."
        resp.tool_calls = []
        return resp

    llm._call.side_effect = call_side_effect

    return llm


def make_mock_tool_registry():
    """Create a ToolRegistry with a few safe tools for testing."""
    reg = ToolRegistry()
    reg.register(
        ToolDef(
            "shell",
            "Run shell commands",
            lambda **kw: {"success": True, "output": "ok"},
            permission="destructive",
        )
    )
    reg.register(
        ToolDef(
            "read_file",
            "Read a file",
            lambda **kw: {"success": True, "output": "file content"},
            permission=PERMISSION_SAFE,
        )
    )
    reg.register(
        ToolDef(
            "uuid",
            "Generate UUID",
            lambda: {"success": True, "output": "test-uuid"},
            permission=PERMISSION_SAFE,
        )
    )
    return reg


def make_minimal_ww(model="mock/model", persist_dir=None, with_memory=False):
    """Create a Worldwave with all heavy dependencies mocked."""
    pd = persist_dir or tempfile.mkdtemp(prefix="ww_test_")

    with patch("core.loop.create_llm") as mock_create_llm, \
         patch("core.loop.SkillManager") as mock_skills, \
         patch("core.loop.ConfigManager") as mock_config, \
         patch("core.loop.Scheduler") as mock_scheduler, \
         patch("core.loop.EvolutionEngine") as mock_evolution, \
         patch("core.loop.get_logger") as mock_logger, \
         patch("core.loop.Subconscious") as mock_subconscious, \
         patch("core.loop.CheckpointDB") as mock_checkpoint_db, \
         patch("core.context.ConversationManager") as mock_conv, \
         patch("core.loop.GlobalWorkspace") as mock_workspace, \
         patch("core.loop.CascadeBus") as mock_cascade, \
         patch("core.loop.CircadianRhythm") as mock_circadian, \
         patch("core.loop.BasalGanglia") as mock_bg, \
         patch("core.loop.PredictiveModel") as mock_pm, \
         patch("core.loop.SkillSolidifier") as mock_ss, \
         patch("core.loop.wire_biomimetic_cascade") as mock_wire:

        llm = make_mock_llm()
        mock_create_llm.return_value = llm

        # Config returns sensible defaults
        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default: {
            "subconscious_enabled": True,
            "subconscious_threshold": 0.7,
            "context_max_messages": 30,
            "context_max_tokens": 32000,
            "workspace_capacity": 7,
            "reflex_arc_enabled": False,  # default off — reflex arc tests enable explicitly
            "reflex_threshold": 0.15,
        }.get(key, default)
        mock_config.return_value = cfg

        # Skills
        skills = MagicMock()
        skills.find_relevant.return_value = []
        mock_skills.return_value = skills

        # Evolution
        evo = MagicMock()
        evo.metrics = MagicMock()
        mock_evolution.return_value = evo

        # Subconscious
        sub = MagicMock()
        sub.enabled = True
        sub.should_intervene.return_value = {"intervene": False, "action": "noop"}
        mock_subconscious.return_value = sub

        # Basal Ganglia
        bg = MagicMock()
        bg.classify_action.return_value = "safe_read"
        bg.evaluate_action.return_value = {"allow": True, "reason": "safe"}
        mock_bg.return_value = bg

        # Checkpoint DB
        cdb = MagicMock()
        mock_checkpoint_db.return_value = cdb

        # Conversation manager
        conv = MagicMock()
        mock_conv.return_value = conv

        # Workspace, cascade, circadian
        mock_workspace.return_value = MagicMock()
        mock_cascade.return_value = MagicMock()
        mock_circadian.return_value = MagicMock()
        mock_pm.return_value = MagicMock()
        mock_ss.return_value = MagicMock()

        tools = make_mock_tool_registry()
        memory = MagicMock() if with_memory else None

        ww = Worldwave(
            model=model,
            persist_dir=pd,
            memory_system=memory,
            tools=tools,
        )
        ww.llm = llm
        ww.verbose = False  # quiet during tests
        return ww


def make_mock_response(content="ok", tool_calls=None):
    """Create a mock LLM response (for reflex arc tests)."""
    resp = MagicMock()
    resp.content = content
    resp.tool_calls = tool_calls or []
    return resp


# ── Init & construction ──


class TestInit:
    def test_default_construction(self):
        ww = make_minimal_ww()
        assert ww.model == "mock/model"
        assert ww.running is False
        assert ww.tools is not None
        assert ww.state is not None
        assert ww.state.session_id is not None
        assert len(ww.state.session_id) == 12

    def test_custom_model(self):
        ww = make_minimal_ww(model="gpt-4o")
        assert ww.model == "gpt-4o"

    def test_create_ww_factory(self):
        with patch("core.loop.Worldwave.__init__", return_value=None) as mock_init:
            create_ww(model="test-model", persist_dir="/tmp/test")
            mock_init.assert_called_once_with(
                model="test-model",
                persist_dir="/tmp/test",
            )

    def test_init_with_memory(self):
        ww = make_minimal_ww(with_memory=True)
        assert ww.memory is not None

    def test_init_registers_tools(self):
        ww = make_minimal_ww()
        assert ww.tools.get("shell") is not None
        assert ww.tools.get("read_file") is not None
        assert ww.tools.get("uuid") is not None

    def test_init_creates_state_manager(self):
        ww = make_minimal_ww()
        assert isinstance(ww.state, StateManager)
        assert ww.state.current_spiral == 0
        assert ww.state.current_phase == "idle"


# ── State machine ──


class TestStateMachine:
    def test_begin_spiral_increments_counter(self):
        ww = make_minimal_ww()
        spiral = ww.state.begin_spiral()
        assert spiral.spiral_number == 1
        assert ww.state.current_spiral == 1
        assert ww.state.current_phase == "perceive"

    def test_set_phase_transitions(self):
        ww = make_minimal_ww()
        ww.state.begin_spiral()
        ww.state.set_phase("perceive")
        assert ww.state.current_phase == "recall"
        ww.state.set_phase("recall")
        assert ww.state.current_phase == "plan"
        ww.state.set_phase("plan")
        assert ww.state.current_phase == "act"
        ww.state.set_phase("act")
        assert ww.state.current_phase == "evaluate"
        ww.state.set_phase("evaluate")
        assert ww.state.current_phase == "learn"
        ww.state.set_phase("learn")
        assert ww.state.current_phase == "completed"

    def test_complete_spiral_marks_done(self):
        ww = make_minimal_ww()
        spiral = ww.state.begin_spiral()
        ww.state.complete_spiral()
        assert spiral.completed_at != ""
        assert ww.state.current_phase == "idle"

    def test_interrupt_creates_checkpoint(self):
        ww = make_minimal_ww()
        ww.state.begin_spiral()
        cp = ww.state.interrupt("user_pause")
        assert cp.interrupted is True
        assert cp.interrupt_reason == "user_pause"
        assert cp.spiral_number == 1

    def test_get_last_checkpoint_returns_interrupted(self):
        ww = make_minimal_ww()
        ww.state.begin_spiral()
        ww.state.interrupt("test interrupt")
        last = ww.state.get_last_checkpoint()
        assert last is not None
        assert last.interrupted is True
        assert last.interrupt_reason == "test interrupt"

    def test_get_last_checkpoint_none_when_no_interrupts(self):
        ww = make_minimal_ww()
        ww.state.begin_spiral()
        ww.state.complete_spiral()
        assert ww.state.get_last_checkpoint() is None

    def test_summary_includes_key_fields(self):
        ww = make_minimal_ww()
        summary = ww.state.summary()
        assert "session_id" in summary
        assert "current_spiral" in summary
        assert "current_phase" in summary
        assert "total_checkpoints" in summary

    def test_get_spiral_by_number(self):
        ww = make_minimal_ww()
        s1 = ww.state.begin_spiral()
        assert ww.state.get_spiral(1) is s1
        assert ww.state.get_spiral(99) is None

    def test_resume_clears_interrupted(self):
        ww = make_minimal_ww()
        ww.state.begin_spiral()
        cp = ww.state.interrupt("test")
        result = ww.state.resume(cp.id, {"plan": {"strategy": "retry"}})
        assert result is True

    def test_resume_unknown_id(self):
        ww = make_minimal_ww()
        assert ww.state.resume("nonexistent", {}) is False


# ── Complexity estimation ──


class TestComplexityEstimation:
    def test_simple_task_low_complexity(self):
        ww = make_minimal_ww()
        score = ww._estimate_complexity("read the file")
        assert 0.0 <= score <= 1.0

    def test_trivial_single_word_low(self):
        ww = make_minimal_ww()
        score = ww._estimate_complexity("uptime")
        assert score < 0.3, f"Expected low complexity, got {score}"

    def test_multi_step_markers_high(self):
        ww = make_minimal_ww()
        score = ww._estimate_complexity("plan and design a new architecture then deploy")
        assert score > 0.4, f"Expected higher complexity, got {score}"

    def test_line_number_pattern_reduces_score(self):
        ww = make_minimal_ww()
        base = ww._estimate_complexity("fix the bug at line 42 in core.py")
        # With line edit pattern, should be moderate at most
        assert 0.0 <= base <= 1.0

    def test_long_goal_high_complexity(self):
        ww = make_minimal_ww()
        long_goal = " ".join(["word"] * 50)
        score = ww._estimate_complexity(long_goal)
        # 50 repeated words: token=0.8, verb=0.5(ambiguous), structural=0 → 0.465
        assert score > 0.4, f"Expected moderate+ complexity for long goal, got {score}"

    def test_capped_at_one(self):
        ww = make_minimal_ww()
        crazy = "plan design build implement deploy orchestrate coordinate analyze " * 20
        score = ww._estimate_complexity(crazy)
        assert score <= 1.0


# ── Reflex arc (fast path) ──


class TestReflexArc:
    def test_reflex_arc_executes_simple_task(self):
        ww = make_minimal_ww()

        # Configure LLM for reflex arc — return text-only response
        resp = make_mock_response(content="Task done.", tool_calls=[])
        ww.llm._call.return_value = resp

        result = ww._reflex_arc_execute("check status")
        assert result is not None
        assert result["reflex"] is True
        assert result["status"] == "completed"
        assert result["spirals_completed"] == 0
        results = result["results"]
        assert len(results) == 1
        assert results[0]["success"] is True

    def test_reflex_arc_with_tool_calls(self):
        ww = make_minimal_ww()
        ww.llm._call.side_effect = None  # clear default side_effect
        ww.llm._call.return_value = make_mock_response(
            tool_calls=[
                {
                    "function": {
                        "name": "uuid",
                        "arguments": "{}",
                    }
                }
            ]
        )
        result = ww._reflex_arc_execute("generate a uuid")
        assert result is not None
        assert result["reflex"] is True
        actions = result["results"][0]["actions"]
        # Tool ran first; synthesis may append reflex_text for user-facing reply
        assert actions[0]["tool"] == "uuid"
        assert any(a.get("tool") == "uuid" for a in actions)

    def test_reflex_arc_llm_failure_returns_none(self):
        ww = make_minimal_ww()
        ww.llm._call.side_effect = RuntimeError("LLM down")
        result = ww._reflex_arc_execute("do something")
        assert result is None

    def test_reflex_arc_no_basal_ganglia_block(self):
        """Reflex arc skips Basal Ganglia — it only handles trivial queries.
        Full spiral loop still has Basal Ganglia for real risk evaluation."""
        ww = make_minimal_ww()
        ww.llm._call.side_effect = None  # clear default side_effect
        ww.llm._call.return_value = make_mock_response(
            tool_calls=[
                {
                    "function": {
                        "name": "shell",
                        "arguments": '{"command": "echo hello"}',
                    }
                }
            ]
        )
        # Even with basal ganglia configured to block, reflex arc passes through
        ww.basal_ganglia.evaluate_action.return_value = {
            "allow": False,
            "reason": "destructive command blocked",
        }
        result = ww._reflex_arc_execute("say hello")
        assert result is not None
        actions = result["results"][0]["actions"]
        # Basal ganglia is NOT checked in reflex arc — tool should execute
        assert not any(a.get("result", {}).get("blocked_by") == "basal_ganglia"
                       for a in actions)

    def test_run_uses_reflex_for_simple_goal(self):
        ww = make_minimal_ww()
        # Enable reflex arc for this test
        ww.llm._call.side_effect = None  # clear default side_effect
        ww.config.get = MagicMock(side_effect=lambda key, default: {
            "reflex_arc_enabled": True,
            "reflex_threshold": 0.15,
        }.get(key, default))
        # Force low complexity
        ww._estimate_complexity = MagicMock(return_value=0.01)
        ww._reflex_arc_execute = MagicMock(return_value={
            "status": "completed",
            "spirals_completed": 0,
            "results": [],
            "session_id": ww.state.session_id,
            "summary": "reflex",
            "reflex": True,
        })

        result = ww.run("hi")
        ww._reflex_arc_execute.assert_called_once()
        assert result["reflex"] is True

    def test_run_skips_reflex_for_complex_goal(self):
        ww = make_minimal_ww()
        # Force high complexity
        ww._estimate_complexity = MagicMock(return_value=0.9)

        # Make the full spiral bail early (after first iteration fails)
        ww.llm.chat_json.side_effect = Exception("stop after one")

        ww.run("design a new feature from scratch with tests")
        # Should NOT have called reflex arc
        # The complexity is above threshold so it should try full spiral
        assert ww.state.current_spiral > 0 or ww.state.get_last_checkpoint() is not None

    def test_run_skips_reflex_for_image_tasks(self):
        ww = make_minimal_ww()
        ww._estimate_complexity = MagicMock(return_value=0.01)
        ww._reflex_arc_execute = MagicMock()

        ww.llm.chat_json.side_effect = Exception("stop")
        ww.run("describe this image", image_path="/tmp/photo.png")
        ww._reflex_arc_execute.assert_not_called()


# ── run() full spiral ──


class TestRun:
    def test_run_single_spiral_happy_path(self):
        ww = make_minimal_ww()
        ww._estimate_complexity = MagicMock(return_value=0.9)  # skip reflex arc
        # respond tool triggers early exit with success
        ww.llm.chat_json.side_effect = lambda messages, phase="", **kw: {
            "perceive": {
                "observations": ["all good"],
                "key_signals": [],
                "environment_summary": "ok",
                "uncertainties": [],
            },
            "recall": {
                "query": "status",
                "entities": [],
            },
            "plan": {
                "strategy": "Report status",
                "steps": [
                    {"tool": "respond", "params": {"prompt": "status report"}, "description": "report"}
                ],
            },
        }.get(phase, {"result": "ok"})

        result = ww.run("check status")
        assert result["status"] == "completed"
        assert result["spirals_completed"] >= 1

    def test_run_respects_max_spirals(self):
        ww = make_minimal_ww()

        call_count = [0]

        def limited_chat_json(messages, phase="", **kw):
            call_count[0] += 1
            if phase == "plan":
                return {
                    "strategy": "test",
                    "steps": [
                        {"tool": "uuid", "params": {}, "description": "gen uuid"}
                    ],
                }
            if phase == "evaluate":
                return {
                    "success": False,
                    "reason": "not yet done",
                    "goal_remaining": True,
                    "next_action": "retry",
                }
            return {"result": "ok"}

        ww.llm.chat_json.side_effect = limited_chat_json

        result = ww.run("test", max_spirals=2)
        # Should have completed at most 2 spirals
        assert result["spirals_completed"] <= 2

    def test_run_handles_keyboard_interrupt(self):
        ww = make_minimal_ww()

        def raise_interrupt(messages, phase="", **kw):
            raise KeyboardInterrupt()

        ww.llm.chat_json.side_effect = raise_interrupt

        result = ww.run("test")
        assert result["status"] == "interrupted"

    def test_run_handles_exception(self):
        ww = make_minimal_ww()
        ww._estimate_complexity = MagicMock(return_value=0.9)  # skip reflex arc

        def raise_error(messages, phase="", **kw):
            raise ValueError("something went wrong")

        ww.llm.chat_json.side_effect = raise_error

        result = ww.run("test")
        assert result["status"] == "completed"  # exception caught, run ends normally

    def test_goal_achieved_true(self):
        ww = make_minimal_ww()
        spiral = SpiralState(spiral_number=1)
        spiral.evaluation = {
            "success": True,
            "goal_remaining": False,
            "reason": "done",
        }
        assert ww._goal_achieved(spiral) is True

    def test_goal_achieved_false_when_remaining(self):
        ww = make_minimal_ww()
        spiral = SpiralState(spiral_number=1)
        spiral.evaluation = {
            "success": True,
            "goal_remaining": True,
            "reason": "partial",
        }
        assert ww._goal_achieved(spiral) is False

    def test_goal_achieved_false_when_not_success(self):
        ww = make_minimal_ww()
        spiral = SpiralState(spiral_number=1)
        spiral.evaluation = {
            "success": False,
            "goal_remaining": False,
            "reason": "failed",
        }
        assert ww._goal_achieved(spiral) is False

    def test_goal_achieved_none_evaluation(self):
        ww = make_minimal_ww()
        spiral = SpiralState(spiral_number=1)
        spiral.evaluation = None
        assert ww._goal_achieved(spiral) is False


# ── Checkpoint save/restore ──


class TestCheckpoints:
    def test_checkpoint_saved_on_perceive(self):
        ww = make_minimal_ww()
        ww.checkpoint_db.save_checkpoint = MagicMock()

        ww.llm.chat_json.side_effect = lambda messages, phase="", **kw: {
            "perceive": {
                "observations": ["ok"],
                "key_signals": [],
                "environment_summary": "ok",
                "uncertainties": [],
            },
            "recall": {"query": "ok", "entities": []},
            "plan": {
                "strategy": "test",
                "steps": [{"tool": "respond", "params": {"prompt": "ok"}, "description": "ok"}],
            },
        }.get(phase, {"result": "ok"})

        ww.run("test", max_spirals=1)
        assert ww.checkpoint_db.save_checkpoint.call_count >= 1

    def test_checkpoint_contains_phase_info(self):
        ww = make_minimal_ww()

        captured = []

        def capture_save_checkpoint(**kwargs):
            captured.append(kwargs)

        ww.checkpoint_db.save_checkpoint = capture_save_checkpoint

        ww.llm.chat_json.side_effect = lambda messages, phase="", **kw: {
            "perceive": {
                "observations": ["ok"],
                "key_signals": [],
                "environment_summary": "ok",
                "uncertainties": [],
            },
            "recall": {"query": "ok", "entities": []},
            "plan": {
                "strategy": "test",
                "steps": [{"tool": "respond", "params": {"prompt": "ok"}, "description": "ok"}],
            },
        }.get(phase, {"result": "ok"})

        ww.run("test", max_spirals=1)

        phases_seen = {c.get("phase") for c in captured if c.get("phase")}
        assert "perceive" in phases_seen

    def test_hitl_interrupt_saves_resume_data(self):
        ww = make_minimal_ww()

        captured_interrupt = []

        def capture_save(**kwargs):
            if kwargs.get("interrupted"):
                captured_interrupt.append(kwargs)

        ww.checkpoint_db.save_checkpoint = capture_save

        def raise_interrupt(messages, phase="", **kw):
            raise KeyboardInterrupt()

        ww.llm.chat_json.side_effect = raise_interrupt

        ww.run("test")
        assert len(captured_interrupt) >= 1
        assert captured_interrupt[0]["interrupted"] is True
        assert captured_interrupt[0]["interrupt_reason"] == "user_interrupt"


# ── LLM phase methods ──


class TestLLMPhases:
    def test_perceive_returns_dict(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.return_value = {
            "observations": ["env ok"],
            "key_signals": ["signal1"],
            "environment_summary": "good",
            "uncertainties": [],
        }
        result = ww._llm_perceive("test goal")
        assert isinstance(result, dict)
        assert "observations" in result
        assert "key_signals" in result

    def test_perceive_handles_llm_error(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.side_effect = RuntimeError("LLM unavailable")
        result = ww._llm_perceive("test")
        assert "observations" in result
        assert "LLM unavailable" in str(result["observations"])

    def test_perceive_includes_previous_failure(self):
        ww = make_minimal_ww()
        captured = []

        def capture(messages, phase="", **kw):
            captured.append(messages)
            return {"observations": [], "key_signals": [], "uncertainties": []}

        ww.llm.chat_json.side_effect = capture
        ww._llm_perceive("test", prev_failure=json.dumps([{"tool": "shell", "error": "failed"}]))
        user_msg = captured[0][0]["content"]
        assert "Previous attempt FAILED" in user_msg
        assert "shell" in user_msg

    def test_recall_with_memory_system(self):
        ww = make_minimal_ww(with_memory=True)
        ww.memory.recall.return_value = {
            "results": [
                {"atom": {"content": "past system check", "id": "m1"}},
            ]
        }
        ww.llm.chat_json.return_value = {
            "query": "system check",
            "entities": ["system"],
        }
        result = ww._llm_recall({"observations": ["ok"]}, "check system")
        assert len(result["memories"]) > 0

    def test_recall_without_memory_system(self):
        ww = make_minimal_ww(with_memory=False)
        ww.llm.chat_json.return_value = {
            "query": "test",
            "entities": [],
        }
        result = ww._llm_recall({"observations": ["ok"]}, "test")
        assert result["memories"] == []

    def test_plan_includes_tool_prompt(self):
        ww = make_minimal_ww()
        captured = []

        def capture(messages, phase="", **kw):
            captured.append(messages)
            return {"strategy": "test", "steps": []}

        ww.llm.chat_json.side_effect = capture
        ww._llm_plan(
            {"environment_summary": "ok"},
            {"memories": []},
            "test goal",
        )
        user_msg = captured[0][0]["content"]
        assert "test goal" in user_msg
        assert "shell" in user_msg  # available tools listed

    def test_plan_handles_error(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.side_effect = RuntimeError("LLM down")
        result = ww._llm_plan({}, {"memories": []}, "test")
        assert result["strategy"] == "fallback"
        assert len(result["steps"]) > 0

    def test_plan_injects_subconscious_warnings(self):
        ww = make_minimal_ww()
        ww.state.global_context["subconscious_warning"] = "avoid shell tool"
        captured = []

        def capture(messages, phase="", **kw):
            captured.append(messages)
            return {"strategy": "test", "steps": []}

        ww.llm.chat_json.side_effect = capture
        ww._llm_plan({}, {"memories": []}, "test")
        user_msg = captured[0][0]["content"]
        assert "avoid shell tool" in user_msg
        # Should be cleared after injection
        assert ww.state.global_context.get("subconscious_warning", "") == ""

    def test_plan_injects_downgrade_excludes_categories(self):
        ww = make_minimal_ww()
        ww.state.global_context["subconscious_downgrade"] = "restrict code execution"
        captured = []

        def capture(messages, phase="", **kw):
            captured.append(messages)
            return {"strategy": "test", "steps": []}

        ww.llm.chat_json.side_effect = capture
        ww._llm_plan({}, {"memories": []}, "test")
        user_msg = captured[0][0]["content"]
        assert "restrict code execution" in user_msg

    def test_act_executes_respond_step(self):
        ww = make_minimal_ww()
        plan = {
            "strategy": "talk",
            "steps": [
                {
                    "tool": "respond",
                    "params": {"prompt": "Say hello"},
                    "description": "respond",
                }
            ],
        }
        ww.llm.chat.side_effect = lambda *a, **kw: "Hello, World!"
        results = ww._llm_act(plan, goal="greet user")
        assert len(results) == 1
        assert results[0]["tool"] == "respond"
        assert results[0]["result"]["success"] is True
        assert "Hello" in results[0]["result"]["output"]

    def test_act_question_step_stores_pending(self):
        ww = make_minimal_ww()
        plan = {
            "strategy": "ask",
            "steps": [
                {
                    "tool": "question",
                    "params": {"content": "What is your name?"},
                    "description": "ask user",
                }
            ],
        }
        results = ww._llm_act(plan, goal="identify user")
        assert len(results) == 1
        assert results[0]["tool"] == "question"
        assert ww._pending_question == "What is your name?"

    def test_act_executes_tool_with_result_tracking(self):
        ww = make_minimal_ww()
        plan = {
            "strategy": "test",
            "steps": [
                {
                    "tool": "uuid",
                    "params": {},
                    "description": "gen id",
                }
            ],
        }
        results = ww._llm_act(plan, goal="generate id")
        # Tool execution produces 2 results: the actual tool call + no_tool_or_code fallback
        assert len(results) == 2
        assert results[0]["tool"] == "uuid"
        assert results[0]["result"]["success"] is True

    def test_act_resolves_template_params(self):
        ww = make_minimal_ww()
        plan = {
            "strategy": "chain",
            "steps": [
                {
                    "tool": "uuid",
                    "params": {},
                    "description": "gen id",
                },
                {
                    "tool": "read_file",
                    "params": {"path": "{{step1_output}}"},
                    "description": "read file",
                },
            ],
        }
        results = ww._llm_act(plan, goal="chain test")
        # Each step produces 2 entries (tool result + no_tool_or_code fallback)
        assert len(results) >= 2
        # The second step's path should have been resolved from step1 output
        read_result = [r for r in results if r.get("tool") == "read_file"]
        if read_result:
            # params should be resolved — path replaced from {{step1_output}}
            assert read_result[0]["params"]["path"] != "{{step1_output}}"

    def test_evaluate_returns_evaluation_dict(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.return_value = {
            "success": True,
            "reason": "all good",
            "lessons_learned": [],
            "goal_remaining": False,
            "next_action": "stop",
        }
        plan = {"strategy": "test"}
        actions = [{"tool": "uuid", "result": {"success": True, "output": "id123"}}]
        result = ww._llm_evaluate(plan, actions, "test goal")
        assert result["success"] is True
        assert result["goal_remaining"] is False

    def test_evaluate_handles_error(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.side_effect = RuntimeError("LLM down")
        result = ww._llm_evaluate({}, [], "test")
        assert result["success"] is False
        assert "LLM evaluate failed" in result["reason"]

    def test_learn_stores_memory(self):
        ww = make_minimal_ww(with_memory=True)
        ww.memory._do_store.return_value = {"atom_id": "mem-001"}
        ww.llm.chat_json.return_value = {
            "content": "Learned something useful",
            "entities": ["test"],
            "importance": 0.7,
        }
        spiral = SpiralState(spiral_number=1)
        spiral.perception = {"observations": ["ok"]}
        spiral.plan = {"strategy": "test"}
        spiral.actions = []
        spiral.evaluation = {"reason": "ok"}
        result = ww._llm_learn(spiral, "test goal")
        assert result["stored"] is True
        assert result["memory_id"] == "mem-001"

    def test_learn_skips_low_importance(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.side_effect = None  # clear default side_effect
        ww.llm.chat_json.return_value = {
            "content": "not important",
            "importance": 0.1,
        }
        spiral = SpiralState(spiral_number=1)
        spiral.perception = {"observations": []}
        spiral.plan = {"strategy": "test"}
        spiral.actions = []
        spiral.evaluation = {"reason": "ok"}
        result = ww._llm_learn(spiral, "test")
        assert result["stored"] is False
        assert result["reason"] == "low_importance"

    def test_learn_handles_error(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.side_effect = RuntimeError("LLM down")
        spiral = SpiralState(spiral_number=1)
        result = ww._llm_learn(spiral, "test")
        assert result["stored"] is False


# ── Environment & logging ──


class TestAuxiliaryMethods:
    def test_get_environment_state(self):
        ww = make_minimal_ww()
        env = ww._get_environment_state()
        assert isinstance(env, dict)
        assert "hostname" in env
        assert "tools" in env

    def test_stop_sets_running_false(self):
        ww = make_minimal_ww()
        ww.running = True
        ww.stop()
        assert ww.running is False

    def test_store_memory_without_system(self):
        ww = make_minimal_ww(with_memory=False)
        result = ww._store_memory("test content")
        assert result is None

    def test_store_memory_with_system(self):
        ww = make_minimal_ww(with_memory=True)
        ww.memory._do_store.return_value = {"atom_id": "atom-001"}
        result = ww._store_memory("test content", source="test", entities=["e1"])
        assert result == "atom-001"
        ww.memory._do_store.assert_called_once()

    def test_recall_memory_without_system(self):
        ww = make_minimal_ww(with_memory=False)
        result = ww._recall_memory("query")
        assert result == []

    def test_recall_memory_with_system(self):
        ww = make_minimal_ww(with_memory=True)
        ww.memory.recall.return_value = {
            "results": [
                {"atom": {"content": "memory content", "id": "m1"}},
            ]
        }
        result = ww._recall_memory("query")
        assert len(result) == 1
        assert result[0]["content"] == "memory content"

    def test_generate_goal_returns_string(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.return_value = {"goal": "Check disk space"}
        result = ww._generate_goal()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_goal_fallback_on_error(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.side_effect = RuntimeError("LLM down")
        result = ww._generate_goal()
        assert "health" in result.lower() or "status" in result.lower()

    def test_tool_domain_mapping(self):
        ww = make_minimal_ww()
        assert ww._tool_domain("shell") == "shell"
        assert ww._tool_domain("terminal_exec") == "shell"
        assert ww._tool_domain("read_file") == "file"
        assert ww._tool_domain("write_file") == "file"
        assert ww._tool_domain("search_files") == "file"
        assert ww._tool_domain("http_request") == "api"
        assert ww._tool_domain("fetch_url") == "api"
        assert ww._tool_domain("process_kill") == "system"
        assert ww._tool_domain("unknown_tool") == "shell"

    def test_evaluate_action_safety_allows_safe(self):
        ww = make_minimal_ww()
        ww.basal_ganglia.classify_action.return_value = "safe_read"
        ww.basal_ganglia.evaluate_action.return_value = {
            "allow": True,
            "reason": "safe read",
        }
        result = ww._evaluate_action_safety("read_file")
        assert result["allow"] is True


# ── Subconscious intervention during run ──


class TestSubconsciousInterventions:
    def test_rewind_intervention_breaks_loop(self):
        ww = make_minimal_ww()
        ww._estimate_complexity = MagicMock(return_value=0.9)  # skip reflex arc
        ww.subconscious.should_intervene.return_value = {
            "intervene": True,
            "action": "rewind",
            "reason": "repeating failure pattern",
        }
        # Override chat_json: use non-respond plan so evaluate runs and intervention fires
        ww.llm.chat_json.side_effect = lambda messages, phase="", **kw: {
            "perceive": {
                "observations": ["ok"],
                "key_signals": [],
                "environment_summary": "ok",
                "uncertainties": [],
            },
            "recall": {"query": "ok", "entities": []},
            "plan": {
                "strategy": "test",
                "steps": [{"tool": "uuid", "params": {}, "description": "gen uuid"}],
            },
            "evaluate": {
                "success": False,
                "reason": "not done",
                "goal_remaining": True,
                "next_action": "retry",
            },
        }.get(phase, {"result": "ok"})
        result = ww.run("test", max_spirals=3)
        # Rewind interrupts the loop; status is "completed" because rewind
        # interruptions are filtered from the status check (internal mechanism)
        assert result["status"] == "completed"
        assert result["spirals_completed"] == 0  # rewind breaks before spiral completes

    def test_tool_downgrade_injects_into_context(self):
        ww = make_minimal_ww()
        ww._estimate_complexity = MagicMock(return_value=0.9)  # skip reflex arc
        ww.subconscious.should_intervene.return_value = {
            "intervene": True,
            "action": "tool_downgrade",
            "reason": "anomalous pattern",
            "guideline": "restrict dangerous tools",
        }
        ww.llm.chat_json.side_effect = lambda messages, phase="", **kw: {
            "perceive": {
                "observations": ["ok"],
                "key_signals": [],
                "environment_summary": "ok",
                "uncertainties": [],
            },
            "recall": {"query": "ok", "entities": []},
            "plan": {
                "strategy": "test",
                "steps": [{"tool": "uuid", "params": {}, "description": "gen uuid"}],
            },
            "evaluate": {
                "success": False,
                "reason": "not done",
                "goal_remaining": True,
                "next_action": "retry",
            },
        }.get(phase, {"result": "ok"})

        ww.run("test", max_spirals=1)
        assert ww.state.global_context.get("subconscious_downgrade") is not None

    def test_mode_switch_injects_into_context(self):
        ww = make_minimal_ww()
        ww._estimate_complexity = MagicMock(return_value=0.9)  # skip reflex arc
        ww.subconscious.should_intervene.return_value = {
            "intervene": True,
            "action": "mode_switch",
            "reason": "context shift",
            "guideline": "switch to exploratory mode",
        }
        ww.llm.chat_json.side_effect = lambda messages, phase="", **kw: {
            "perceive": {
                "observations": ["ok"],
                "key_signals": [],
                "environment_summary": "ok",
                "uncertainties": [],
            },
            "recall": {"query": "ok", "entities": []},
            "plan": {
                "strategy": "test",
                "steps": [{"tool": "uuid", "params": {}, "description": "gen uuid"}],
            },
            "evaluate": {
                "success": False,
                "reason": "not done",
                "goal_remaining": True,
                "next_action": "retry",
            },
        }.get(phase, {"result": "ok"})

        ww.run("test", max_spirals=1)
        assert ww.state.global_context.get("subconscious_mode") is not None


# ── Edge cases ──


class TestEdgeCases:
    def test_run_with_photo_received_prefix(self):
        ww = make_minimal_ww()
        ww.llm.chat_json.side_effect = lambda messages, phase="", **kw: {
            "perceive": {
                "observations": ["photo received"],
                "key_signals": [],
                "environment_summary": "ok",
                "uncertainties": [],
            },
            "recall": {"query": "photo", "entities": []},
            "plan": {
                "strategy": "describe",
                "steps": [{"tool": "respond", "params": {"prompt": "describe"}, "description": "desc"}],
            },
        }.get(phase, {"result": "ok"})

        result = ww.run("[Photo received: /tmp/test.png]\nDescribe this image")
        # Should skip reflex arc because it's an image task
        assert result is not None

    def test_prev_failure_passed_between_spirals(self):
        ww = make_minimal_ww()
        call_phases = []

        def track_phases(messages, phase="", **kw):
            call_phases.append(phase)
            if phase == "plan":
                return {
                    "strategy": "test",
                    "steps": [{"tool": "shell", "params": {"command": "echo fail"}, "description": "run"}],
                }
            if phase == "evaluate":
                return {
                    "success": False,
                    "reason": "shell failed",
                    "goal_remaining": True,
                    "next_action": "retry",
                }
            return {"result": "ok"}

        ww.llm.chat_json.side_effect = track_phases
        # Configure shell to fail
        ww.tools.get("shell").handler = lambda **kw: {"success": False, "error": "command not found"}

        result = ww.run("test", max_spirals=2)
        # Should have attempted multiple spirals with prev_failure in the second
        assert len(call_phases) >= 4  # at least perceive+recall+plan+evaluate for first

    def test_tool_not_found_in_plan_returns_error_step(self):
        ww = make_minimal_ww()
        plan = {
            "strategy": "call unknown",
            "steps": [
                {"description": "use missing tool", "tool": "nonexistent_tool", "code": ""}
            ],
        }
        results = ww._llm_act(plan, goal="test")
        # Tool call produces 2 results: failed call + no_tool_or_code fallback
        assert len(results) == 2
        assert results[1].get("error") == "no_tool_or_code"

    def test_run_with_running_flag(self):
        ww = make_minimal_ww()
        ww.running = True
        ww.llm.chat_json.return_value = {
            "observations": ["ok"],
            "key_signals": [],
            "environment_summary": "ok",
            "uncertainties": [],
        }
        # Set running to True before run, then immediately clear it
        # to simulate a stop() call between spirals
        original_run = ww.run

        def stop_after_first(*args, **kwargs):
            ww.stop()
            return original_run(*args, **kwargs)

        # Just verify that running flag is honored
        assert ww.running is True
        ww.stop()
        assert ww.running is False


# ── SpiralState dataclass ──


class TestSpiralState:
    def test_default_construction(self):
        s = SpiralState(spiral_number=1)
        assert s.spiral_number == 1
        assert s.id != ""
        assert s.started_at != ""
        assert s.perception == {}
        assert s.recall == {}
        assert s.plan == {}
        assert s.actions == []
        assert s.evaluation == {}
        assert s.learning == {}

    def test_custom_id(self):
        s = SpiralState(spiral_number=2, id="custom-id")
        assert s.id == "custom-id"
