"""Tests: Memory Integration module"""

import sys; sys.path.insert(0, ".")
import pytest

from core.memory_integration import MemoryIntegrator
from core.llm import create_llm


@pytest.fixture
def mi():
    llm = create_llm()
    return MemoryIntegrator(llm=llm)


def test_record_messages(mi):
    """Test recording user and assistant messages."""
    mi.record_user_message("Hello, what can you do?")
    mi.record_assistant_response("I can analyze systems, run commands, and more.")
    ctx = mi.get_context("test session")
    assert "Hello" in ctx
    print("Context contains user message: OK")


def test_record_spiral(mi):
    """Test recording a spiral."""
    spiral_data = {
        "spiral_number": 1,
        "plan": {"steps": [{"description": "Analyze system"}]},
        "evaluation": {"success": True, "reason": "Completed successfully"},
    }
    mi.record_spiral(spiral_data)


def test_stats(mi):
    """Test memory statistics."""
    mi.record_user_message("Hello")
    mi.record_assistant_response("Hi there")
    spiral_data = {
        "spiral_number": 1,
        "evaluation": {"success": True},
    }
    mi.record_spiral(spiral_data)
    mi.record_action({"tool": "shell", "params": {"command": "ls"}, "success": True})

    stats = mi.get_stats()
    assert "session_id" in stats
    assert stats["spirals"] >= 1
    assert stats["context_tokens"] > 0
    assert stats["context_messages"] > 0
    print(f"Stats: {stats['context_messages']} msgs, {stats['context_tokens']} tokens")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
