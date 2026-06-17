"""
tests/eval/test_tool_correctness.py — tool call accuracy evaluation

test WW's tool calling correctness:
1. parameter match rate — JSON schema adherence
2. errorrecovery — toolfailed  process
3. permission interception — whether HITL mechanism is triggered correctly

usage:
    python -m pytest tests/eval/test_tool_correctness.py -v
    # or
    python tests/eval/test_tool_correctness.py
"""

import os
import sys
import tempfile
import unittest


class TestToolCorrectness(unittest.TestCase):
    """Tool calls core feature test."""

    @classmethod
    def setUpClass(cls):
        """Initialize once (shared by all tests)."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        # Do not start the entire WW, only test the registry
        from tools.registry import default_registry, PERMISSION_SAFE, PERMISSION_APPROVAL, PERMISSION_DESTRUCTIVE
        cls.registry = default_registry()
        cls.SAFE = PERMISSION_SAFE
        cls.APPROVAL = PERMISSION_APPROVAL
        cls.DESTRUCTIVE = PERMISSION_DESTRUCTIVE

    def test_all_tools_have_permission(self):
        """All tools should have permission tags."""
        tools = self.registry.list_tools()
        for t in tools[:10]:  # sample check
            self.assertIn(t.permission, ("safe", "requires_approval", "destructive"),
                          f"{t.name} has invalid permission: {t.permission}")

    def test_destructive_blocked_in_hitl(self):
        """HITL mode should intercept destructive tools."""
        self.registry.set_approval_mode("hitl")
        result = self.registry.call("delete", {"path": "/tmp/test"})
        self.assertTrue(result.get("blocked", False))
        self.assertIn("block_reason", result)

    def test_safe_tool_passes_in_hitl(self):
        """HITL mode should not intercept secure tools."""
        self.registry.set_approval_mode("hitl")
        result = self.registry.call("timestamp", {"format": "unix"})
        self.assertFalse(result.get("blocked", False))

    def test_destructive_denied_in_deny_mode(self):
        """DENY mode should reject all high-risk tools."""
        self.registry.set_approval_mode("deny")
        for name in ("shell", "delete", "code"):
            t = self.registry.get(name)
            if t and t.permission == "destructive":
                result = self.registry.call(name, {"command": "echo test"})
                self.assertTrue(result.get("blocked", False),
                                f"{name} should be blocked in deny mode")

    def test_auto_mode_passes_destructive(self):
        """AUTO mode should allow all tools (such as passing guardrails)."""
        self.registry.set_approval_mode("auto")
        self.registry.set_guardrails(None)
        result = self.registry.call("shell", {"command": "echo hi"})
        self.assertFalse(result.get("blocked", False))
        self.assertTrue(result.get("success", False))

    def test_approval_callback(self):
        """approval_callback should make correct decisions."""
        self.registry.set_approval_mode("hitl")
        calls = []

        def callback(name, params):
            calls.append(name)
            return True  # approve

        self.registry.set_approval_callback(callback)
        result = self.registry.call("shell", {"command": "echo test"})
        self.assertFalse(result.get("blocked", False))
        self.assertEqual(calls, ["shell"])

        # reject test
        def reject(name, params):
            return False

        self.registry.set_approval_callback(reject)
        result2 = self.registry.call("delete", {"path": "/x"})
        self.assertTrue(result2.get("blocked", False))

    def test_unknown_tool_returns_error(self):
        """unknown tool should return error, not crash."""
        result = self.registry.call("nonexistent_tool", {})
        self.assertFalse(result.get("success", True))
        self.assertIn("unknown tool", result.get("error", ""))


class TestCheckpointCorrectness(unittest.TestCase):
    """Checkpoint system correctness test."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    def setUp(self):
        from core.checkpoint import CheckpointDB
        self.db_path = tempfile.mkstemp(suffix=".db")[1]
        self.db = CheckpointDB(db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_create_session(self):
        """creating a session should return a valid ID."""
        sid = self.db.create_session("Test goal")
        self.assertTrue(len(sid) > 0)
        s = self.db.get_session(sid)
        self.assertEqual(s["goal"], "Test goal")

    def test_save_and_retrieve_checkpoint(self):
        """saved checkpoint should be correctly read."""
        sid = self.db.create_session("Test")
        cp_id = self.db.save_checkpoint(
            session_id=sid,
            spiral_number=1,
            phase="act",
            step_number=3,
            step_total=5,
            scratchpad="test scratchpad",
            tool_history=[{"tool": "shell", "success": True}],
        )
        cp = self.db.get_checkpoint(cp_id)
        self.assertIsNotNone(cp)
        self.assertEqual(cp["spiral_number"], 1)
        self.assertEqual(cp["phase"], "act")
        self.assertEqual(len(cp["tool_history"]), 1)

    def test_interrupted_checkpoint(self):
        """breakpoints should be correctly marked."""
        sid = self.db.create_session("Test interruption")
        self.db.save_checkpoint(
            session_id=sid, spiral_number=2, phase="plan",
            interrupted=True, interrupt_reason="user_requested",
            resume_data={"goal": "Resume goal"},
        )
        icp = self.db.get_last_interrupted(sid)
        self.assertIsNotNone(icp)
        self.assertTrue(icp["is_interrupted"])
        self.assertEqual(icp["interrupt_reason"], "user_requested")

    def test_mark_resolved(self):
        """recovery break markers should be cleared."""
        sid = self.db.create_session("Resolve test")
        cp_id = self.db.save_checkpoint(
            session_id=sid, spiral_number=1, phase="act",
            interrupted=True, interrupt_reason="test",
        )
        self.db.mark_resolved(cp_id)
        cp = self.db.get_checkpoint(cp_id)
        self.assertFalse(cp["is_interrupted"])

    def test_get_checkpoint_by_spiral(self):
        """should be able to precisely locate checkpoint by spiral+phase."""
        sid = self.db.create_session("Spiral lookup")
        self.db.save_checkpoint(sid, 1, "perceive")
        self.db.save_checkpoint(sid, 1, "plan")
        cp = self.db.get_checkpoint_by_spiral(sid, 1, "plan")
        self.assertEqual(cp["phase"], "plan")

    def test_session_list(self):
        """list_sessions should return the correct count of sessions."""
        sid1 = self.db.create_session("Goal A")
        sid2 = self.db.create_session("Goal B")
        sessions = self.db.list_sessions(limit=10)
        self.assertTrue(len(sessions) >= 2)

    def test_delete_cleanup(self):
        """delete session should clean up all data."""
        sid = self.db.create_session("Delete me")
        self.db.save_checkpoint(sid, 1, "perceive")
        self.db.delete_session(sid)
        s = self.db.get_session(sid)
        self.assertIsNone(s)
        cps = self.db.get_checkpoints(sid)
        self.assertEqual(len(cps), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
