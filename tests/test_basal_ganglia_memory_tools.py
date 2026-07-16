"""Regression: memory self-edit tools are safe_info and never BG-blocked.

Locks classify_action / evaluate_action contract so remember/forget/recall_*
and memory_* stay product-usable (not mis-classified as delete/unsafe).
"""

from __future__ import annotations

import pytest

from core.subconscious.basal_ganglia import BasalGanglia


MEMORY_SAFE_TOOLS = (
    "remember",
    "forget",
    "recall_mine",
    "recall",
    "switch_topic",
    "memory_search",
    "memory_store",
    "memory_recall",
    "memory_stats",
    "memory_list",
    "memory_get",
)


@pytest.fixture
def bg():
    return BasalGanglia(state_dim=32)


@pytest.mark.parametrize("tool", MEMORY_SAFE_TOOLS)
def test_classify_action_memory_tools_are_safe_info(bg, tool):
    assert bg.classify_action(tool) == "safe_info"
    # Case-insensitive
    assert bg.classify_action(tool.upper()) == "safe_info"


def test_classify_action_memory_prefix(bg):
    assert bg.classify_action("memory_whatever_new") == "safe_info"
    assert bg.classify_action("MEMORY_X") == "safe_info"


def test_forget_not_classified_as_delete(bg):
    """forget is a memory tool; must not fall through to delete category."""
    assert bg.classify_action("forget") == "safe_info"
    assert bg.classify_action("forget") != "delete"


def test_evaluate_action_always_allows_safe_info_memory_tools(bg):
    """safe_info short-circuit: remember etc. always allow regardless of state."""
    # Extreme stress / danger-ish state vector — still must pass for safe_info
    state = [1.0] * 32
    for tool in ("remember", "forget", "recall_mine", "memory_search"):
        cat = bg.classify_action(tool)
        assert cat == "safe_info"
        result = bg.evaluate_action(state, cat, action_description=tool)
        assert result["allow"] is True
        assert result.get("action_category") == "safe_info"
        assert "safe" in result.get("reason", "").lower()


def test_evaluate_action_safe_info_never_blocked_under_high_caution(bg):
    bg.set_caution(10.0)
    bg.set_stress_level(1.0)
    result = bg.evaluate_action([0.9] * 32, "safe_info", "remember")
    assert result["allow"] is True


def test_spiral_path_uses_classify_then_evaluate(bg):
    """Mirror loop._evaluate_action_safety: classify → evaluate_action."""
    tool_name = "remember"
    category = bg.classify_action(tool_name)
    state = [0.0] * 32
    safety = bg.evaluate_action(
        state=state,
        action_category=category,
        action_description=tool_name,
    )
    assert safety["allow"] is True
    assert category == "safe_info"
