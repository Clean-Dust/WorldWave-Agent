"""Tests: ToolRegistry — registration, permissions, guardrails, hooks, call()"""

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, ".")

from tools.registry import (
    PERMISSION_APPROVAL,
    PERMISSION_DESTRUCTIVE,
    PERMISSION_SAFE,
    ToolDef,
    ToolRegistry,
    _base64_handler,
    _uuid_handler,
    _timestamp_handler,
)


# ── helpers ──


def make_safe_tool(name="test_tool", category="test"):
    """Create a minimal safe ToolDef."""

    def handler(**kwargs):
        return {"output": "ok", "data": kwargs}

    return ToolDef(
        name=name,
        description="A test tool",
        handler=handler,
        parameters={"arg1": {"type": "string"}},
        category=category,
        permission=PERMISSION_SAFE,
    )


def make_destructive_tool(name="rm_tool"):
    """Create a destructive-level ToolDef."""

    def handler(**kwargs):
        return {"output": "removed"}

    return ToolDef(
        name=name,
        description="Destroys things",
        handler=handler,
        permission=PERMISSION_DESTRUCTIVE,
    )


def make_approval_tool(name="write_tool"):
    """Create an approval-level ToolDef."""

    def handler(**kwargs):
        return {"output": "written"}

    return ToolDef(
        name=name,
        description="Writes things",
        handler=handler,
        permission=PERMISSION_APPROVAL,
    )


# ── Registration & retrieval ──


class TestRegistration:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = make_safe_tool("my_tool")
        reg.register(tool)
        assert reg.get("my_tool") is tool

    def test_register_from_def(self):
        reg = ToolRegistry()

        def handler(x=0):
            return {"value": x * 2}

        reg.register_from_def(
            "double",
            "doubles a number",
            handler,
            parameters={"x": {"type": "int"}},
            examples=['double(x=5)'],
            category="math",
            permission=PERMISSION_SAFE,
        )
        t = reg.get("double")
        assert t is not None
        assert t.name == "double"
        assert t.description == "doubles a number"
        assert t.category == "math"
        assert t.permission == PERMISSION_SAFE
        assert t.parameters == {"x": {"type": "int"}}
        assert t.examples == ['double(x=5)']

    def test_get_unknown_returns_none(self):
        reg = ToolRegistry()
        assert reg.get("nonexistent") is None

    def test_list_tools(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("a"))
        reg.register(make_safe_tool("b"))
        names = {t.name for t in reg.list_tools()}
        assert names == {"a", "b"}

    def test_list_by_category(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("t1", category="file"))
        reg.register(make_safe_tool("t2", category="system"))
        reg.register(make_safe_tool("t3", category="file"))
        file_tools = reg.list_by_category("file")
        assert len(file_tools) == 2
        assert {t.name for t in file_tools} == {"t1", "t3"}

    def test_list_by_category_empty(self):
        reg = ToolRegistry()
        assert reg.list_by_category("nonexistent") == []

    def test_tool_names(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("alpha"))
        reg.register(make_safe_tool("beta"))
        assert sorted(reg.tool_names()) == ["alpha", "beta"]

    def test_category_counts(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("a", category="file"))
        reg.register(make_safe_tool("b", category="file"))
        reg.register(make_safe_tool("c", category="system"))
        counts = reg.category_counts()
        assert counts == {"file": 2, "system": 1}


# ── ToolDef formatting ──


class TestToolDef:
    def test_to_prompt_block_safe(self):
        tool = make_safe_tool("safe_tool")
        block = tool.to_prompt_block()
        assert "safe_tool" in block
        assert "(" in block  # category
        assert "A test tool" in block
        # no emoji for safe tools
        assert "⚠" not in block

    def test_to_prompt_block_with_permission(self):
        tool = make_destructive_tool("nuke")
        block = tool.to_prompt_block()
        assert "nuke" in block
        assert "destructive" in block

    def test_to_prompt_block_with_examples(self):
        tool = ToolDef(
            name="example_tool",
            description="Has examples",
            handler=lambda **kw: {},
            examples=["example_tool(x=1)", "example_tool(x=2)"],
        )
        block = tool.to_prompt_block()
        assert "example_tool(x=1)" in block
        assert "example_tool(x=2)" in block

    def test_to_prompt_block_no_params(self):
        tool = ToolDef(name="noparam", description="No parameters", handler=lambda: {})
        block = tool.to_prompt_block()
        assert "{}" in block

    def test_repr(self):
        tool = make_safe_tool("repr_test", category="util")
        r = repr(tool)
        assert "repr_test" in r
        assert "util" in r


# ── call() with mocked tools ──


class TestCall:
    def test_call_safe_tool_returns_success(self):
        reg = ToolRegistry()

        def handler(name="world"):
            return {"greeting": f"hello {name}"}

        reg.register(ToolDef("greet", "greets", handler, permission=PERMISSION_SAFE))
        result = reg.call("greet", {"name": "test"})
        assert result["success"] is True
        assert result["greeting"] == "hello test"

    def test_call_unknown_tool(self):
        reg = ToolRegistry()
        result = reg.call("nope")
        assert result["success"] is False
        assert "unknown tool" in result["error"]

    def test_call_tool_that_returns_string(self):
        reg = ToolRegistry()

        def handler():
            return "plain string"

        reg.register(ToolDef("str_tool", "returns string", handler))
        result = reg.call("str_tool")
        assert result["success"] is True
        assert result["output"] == "plain string"
        assert result["data"] == "plain string"

    def test_call_tool_that_returns_dict_without_success(self):
        reg = ToolRegistry()

        def handler():
            return {"value": 42}

        reg.register(ToolDef("val_tool", "returns dict", handler))
        result = reg.call("val_tool")
        assert result["success"] is True
        assert result["value"] == 42

    def test_call_tool_that_returns_dict_with_error(self):
        reg = ToolRegistry()

        def handler():
            return {"error": "something went wrong"}

        reg.register(ToolDef("err_tool", "returns error", handler))
        result = reg.call("err_tool")
        assert result["success"] is False
        assert result["error"] == "something went wrong"

    def test_call_tool_that_returns_dict_with_stderr_and_nonzero_exit(self):
        reg = ToolRegistry()

        def handler():
            return {"stderr": "traceback...", "exit_code": 1}

        reg.register(ToolDef("stderr_tool", "returns stderr", handler))
        result = reg.call("stderr_tool")
        assert result["success"] is False

    def test_call_tool_handler_raises_exception(self):
        reg = ToolRegistry()

        def handler():
            raise RuntimeError("boom")

        reg.register(ToolDef("crash", "crashes", handler))
        result = reg.call("crash")
        assert result["success"] is False
        assert "boom" in result["error"]


# ── Permission checking ──


class TestPermissions:
    def test_safe_tool_passes_permission_check(self):
        reg = ToolRegistry()
        tool = make_safe_tool()
        result = reg._check_permission(tool)
        assert result is None

    def test_deny_mode_blocks_destructive(self):
        reg = ToolRegistry()
        reg.set_approval_mode("deny")
        tool = make_destructive_tool()
        result = reg._check_permission(tool)
        assert result is not None
        assert result["blocked"] is True
        assert result["block_reason"] == "denied_by_policy"

    def test_deny_mode_blocks_approval(self):
        reg = ToolRegistry()
        reg.set_approval_mode("deny")
        tool = make_approval_tool()
        result = reg._check_permission(tool)
        assert result is not None
        assert result["blocked"] is True

    def test_auto_mode_allows_destructive(self):
        reg = ToolRegistry()
        reg.set_approval_mode("auto")
        tool = make_destructive_tool()
        result = reg._check_permission(tool)
        assert result is None

    def test_hitl_with_suspend_callback(self):
        reg = ToolRegistry()
        reg.set_approval_mode("hitl")

        suspend_results = []

        def suspend_cb(tool_name, params):
            suspend_results.append((tool_name, params))
            return "cp-001"

        reg.set_suspend_callback(suspend_cb)
        tool = make_destructive_tool("danger_zone")
        result = reg._check_permission(tool, {"dry_run": False})

        assert result is not None
        assert result["blocked"] is True
        assert result["suspend"] is True
        assert result["checkpoint_id"] == "cp-001"
        assert result["approval_required"] is True
        assert len(suspend_results) == 1
        assert suspend_results[0][0] == "danger_zone"

    def test_hitl_with_approval_callback_approved(self):
        reg = ToolRegistry()
        reg.set_approval_mode("hitl")

        def approve_cb(tool_name, params):
            return True

        reg.set_approval_callback(approve_cb)
        tool = make_destructive_tool()
        result = reg._check_permission(tool)

        assert result is None  # approved, no block

    def test_hitl_with_approval_callback_rejected(self):
        reg = ToolRegistry()
        reg.set_approval_mode("hitl")

        def reject_cb(tool_name, params):
            return False

        reg.set_approval_callback(reject_cb)
        tool = make_destructive_tool()
        result = reg._check_permission(tool)

        assert result is not None
        assert result["block_reason"] == "denied_by_user"

    def test_hitl_no_callback_creates_pending(self):
        reg = ToolRegistry()
        reg.set_approval_mode("hitl")
        tool = make_destructive_tool()
        result = reg._check_permission(tool)

        assert result is not None
        assert result["block_reason"] == "pending_approval"
        assert result["approval_id"] is not None
        assert result["approval_required"] is True
        assert reg._pending_approvals[result["approval_id"]]["status"] == "pending"

    def test_safe_tool_ignores_hitl_mode(self):
        reg = ToolRegistry()
        reg.set_approval_mode("hitl")
        tool = make_safe_tool()
        result = reg._check_permission(tool)
        assert result is None

    def test_set_approval_mode_invalid_raises(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError):
            reg.set_approval_mode("invalid_mode")

    def test_call_returns_permission_block(self):
        reg = ToolRegistry()
        reg.set_approval_mode("deny")
        tool = make_destructive_tool("danger")
        reg.register(tool)

        result = reg.call("danger")
        assert result["success"] is False
        assert result["blocked"] is True


# ── Guardrails integration ──


class TestGuardrailsIntegration:
    def test_guardrails_blocks_shell(self):
        reg = ToolRegistry()
        reg.register(
            ToolDef("shell", "runs commands", lambda **kw: {"output": "ok"},
                    permission=PERMISSION_DESTRUCTIVE)
        )
        mock_gr = MagicMock()
        mock_gr.enabled = True
        mock_result = MagicMock()
        mock_result.__bool__.return_value = False
        mock_result.reason = "blocked: destructive command"
        mock_gr.check_shell_command.return_value = mock_result
        reg.set_guardrails(mock_gr)

        result = reg.call("shell", {"command": "rm -rf /"})
        assert result["success"] is False
        assert "secureGuardrail" in result["error"]
        assert result["guardrails_blocked"] is True
        mock_gr.check_shell_command.assert_called_once_with("rm -rf /")

    def test_guardrails_allows_safe_shell(self):
        reg = ToolRegistry()
        reg.register(
            ToolDef("shell", "runs commands", lambda **kw: {"output": "ok"},
                    permission=PERMISSION_DESTRUCTIVE)
        )
        mock_gr = MagicMock()
        mock_gr.enabled = True
        mock_result = MagicMock()
        mock_result.__bool__.return_value = True
        mock_gr.check_shell_command.return_value = mock_result
        reg.set_guardrails(mock_gr)
        reg.set_approval_mode("auto")

        result = reg.call("shell", {"command": "ls -la"})
        assert result["success"] is True

    def test_guardrails_blocks_file_write(self):
        reg = ToolRegistry()
        reg.register(
            ToolDef("file_write", "writes files", lambda **kw: {"output": "ok"},
                    permission=PERMISSION_APPROVAL)
        )
        mock_gr = MagicMock()
        mock_gr.enabled = True
        mock_result = MagicMock()
        mock_result.__bool__.return_value = False
        mock_result.reason = "blocked: sensitive path"
        mock_gr.check_file_write.return_value = mock_result
        reg.set_guardrails(mock_gr)
        reg.set_approval_mode("auto")

        result = reg.call("file_write", {"path": "/etc/shadow", "content": "x"})
        assert result["success"] is False
        assert "secureGuardrail" in result["error"]
        mock_gr.check_file_write.assert_called_once_with("/etc/shadow")

    def test_guardrails_blocks_code(self):
        reg = ToolRegistry()
        reg.register(
            ToolDef("code", "executes code", lambda **kw: {"output": "ok"},
                    permission=PERMISSION_DESTRUCTIVE)
        )
        mock_gr = MagicMock()
        mock_gr.enabled = True
        mock_result = MagicMock()
        mock_result.__bool__.return_value = False
        mock_result.reason = "blocked: dangerous code"
        mock_gr.check_code.return_value = mock_result
        reg.set_guardrails(mock_gr)
        reg.set_approval_mode("auto")

        result = reg.call("code", {"code": "import os; os.system('rm -rf /')"})
        assert result["success"] is False
        mock_gr.check_code.assert_called_once()

    def test_guardrails_disabled_skips_check(self):
        reg = ToolRegistry()
        reg.register(
            ToolDef("shell", "runs commands",
                    lambda **kw: {"output": "ok", "success": True},
                    permission=PERMISSION_DESTRUCTIVE)
        )
        mock_gr = MagicMock()
        mock_gr.enabled = False
        reg.set_guardrails(mock_gr)
        reg.set_approval_mode("auto")

        result = reg.call("shell", {"command": "rm -rf /"})
        assert result["success"] is True
        mock_gr.check_shell_command.assert_not_called()

    def test_no_guardrails_set_proceeds_normally(self):
        reg = ToolRegistry()
        reg.register(
            ToolDef("shell", "runs commands",
                    lambda **kw: {"output": "ok", "success": True},
                    permission=PERMISSION_DESTRUCTIVE)
        )
        reg.set_approval_mode("auto")
        result = reg.call("shell", {"command": "ls"})
        assert result["success"] is True


# ── Hooks (pre/post tool) ──


class TestHooks:
    def test_pre_hook_allows_execution(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("hooked_tool"))
        reg._run_pre_tool_hooks = MagicMock(return_value=[
            {"allowed": True, "modified_params": None, "stop_reason": None}
        ])
        reg._run_post_tool_hooks = MagicMock(return_value=[])

        result = reg.call("hooked_tool", {"arg1": "val"})
        assert result["success"] is True

    def test_pre_hook_blocks_execution(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("blocked_tool"))
        reg._run_pre_tool_hooks = MagicMock(return_value=[
            {"allowed": False, "modified_params": None, "stop_reason": "policy violation"}
        ])
        reg._run_post_tool_hooks = MagicMock(return_value=[])

        result = reg.call("blocked_tool", {"arg1": "val"})
        assert result["success"] is False
        assert result["hook_blocked"] is True
        assert "Hook blocked" in result["error"]

    def test_pre_hook_modifies_params(self):
        reg = ToolRegistry()

        captured_params = {}

        def handler(**kwargs):
            captured_params.update(kwargs)
            return {"output": "ok"}

        reg.register(ToolDef("param_tool", "param tool", handler,
                             permission=PERMISSION_SAFE))
        reg._run_pre_tool_hooks = MagicMock(return_value=[
            {"allowed": True, "modified_params": {"extra": "injected"}, "stop_reason": None}
        ])
        reg._run_post_tool_hooks = MagicMock(return_value=[])

        result = reg.call("param_tool", {"original": "yes"})
        assert result["success"] is True
        assert captured_params.get("original") == "yes"
        assert captured_params.get("extra") == "injected"

    def test_post_hook_runs_after_execution(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("post_hook_tool"))
        reg._run_pre_tool_hooks = MagicMock(return_value=[])
        reg._run_post_tool_hooks = MagicMock(return_value=[])

        result = reg.call("post_hook_tool")
        assert result["success"] is True
        reg._run_post_tool_hooks.assert_called_once()

    def test_pre_hook_results_empty_means_allowed(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("empty_hook_tool"))
        reg._run_pre_tool_hooks = MagicMock(return_value=[])  # all allowed
        reg._run_post_tool_hooks = MagicMock(return_value=[])

        result = reg.call("empty_hook_tool")
        assert result["success"] is True


# ── prompt_block ──


class TestPromptBlock:
    def test_prompt_block_includes_all_tools(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("t1", category="file"))
        reg.register(make_safe_tool("t2", category="system"))
        block = reg.prompt_block()
        assert "t1" in block
        assert "t2" in block
        assert "FILE" in block
        assert "SYSTEM" in block

    def test_prompt_block_exclude_categories(self):
        reg = ToolRegistry()
        reg.register(make_safe_tool("t1", category="file"))
        reg.register(make_safe_tool("t2", category="dangerous"))
        block = reg.prompt_block(exclude_categories=["dangerous"])
        assert "t1" in block
        assert "t2" not in block

    def test_prompt_block_empty_registry(self):
        reg = ToolRegistry()
        block = reg.prompt_block()
        assert "availabletool" in block


# ── Integration: full default_registry ──


class TestDefaultRegistry:
    def test_default_registry_has_tools(self):
        from tools.registry import default_registry

        reg = default_registry()
        tools = reg.list_tools()
        assert len(tools) > 5, f"Expected >5 tools, got {len(tools)}"

    def test_default_registry_has_categories(self):
        from tools.registry import default_registry

        reg = default_registry()
        counts = reg.category_counts()
        assert "system" in counts
        assert "file" in counts

    def test_default_registry_tool_retrieval(self):
        from tools.registry import default_registry

        reg = default_registry()
        shell = reg.get("shell")
        assert shell is not None
        assert shell.permission == PERMISSION_DESTRUCTIVE

        read_file = reg.get("read_file")
        assert read_file is not None
        assert read_file.permission == PERMISSION_SAFE


# ── Edge cases ──


class TestEdgeCases:
    def test_set_guardrails_overwrites(self):
        reg = ToolRegistry()
        gr1 = MagicMock()
        gr2 = MagicMock()
        reg.set_guardrails(gr1)
        reg.set_guardrails(gr2)
        assert reg.guardrails is gr2

    def test_set_approval_callback_replaces(self):
        reg = ToolRegistry()
        cb1 = lambda name, params: True
        cb2 = lambda name, params: False
        reg.set_approval_callback(cb1)
        reg.set_approval_callback(cb2)
        assert reg.approval_callback is cb2

    def test_set_suspend_callback_replaces(self):
        reg = ToolRegistry()
        sc1 = lambda name, params: "id1"
        sc2 = lambda name, params: "id2"
        reg.set_suspend_callback(sc1)
        reg.set_suspend_callback(sc2)
        assert reg.suspend_callback is sc2

    def test_call_uses_suspend_callback_over_approval(self):
        reg = ToolRegistry()
        reg.set_approval_mode("hitl")

        suspend_called = []
        approve_called = []

        reg.set_suspend_callback(lambda n, p: suspend_called.append(n) or "cp-x")
        reg.set_approval_callback(lambda n, p: approve_called.append(n) or True)

        tool = make_destructive_tool()
        result = reg._check_permission(tool)

        assert result["suspend"] is True
        assert len(suspend_called) == 1
        # approval callback should not be called when suspend is available
        assert len(approve_called) == 0


# ── Built-in handler tests ──


class TestBuiltinHandlers:
    def test_uuid_handler(self):
        result = _uuid_handler()
        assert result["success"] is True
        assert len(result["output"]) == 36  # UUID4 string length

    def test_timestamp_handler_iso(self):
        result = _timestamp_handler("iso")
        assert result["success"] is True
        assert "T" in result["output"]

    def test_timestamp_handler_unix(self):
        result = _timestamp_handler("unix")
        assert result["success"] is True
        assert int(result["output"]) > 0

    def test_base64_handler_encode_decode(self):
        enc = _base64_handler("encode", "hello")
        assert enc["success"] is True
        dec = _base64_handler("decode", enc["output"])
        assert dec["success"] is True
        assert dec["output"] == "hello"

    def test_base64_handler_invalid_action(self):
        result = _base64_handler("invalid", "data")
        assert result["success"] is False
