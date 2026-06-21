"""Tests: Prompt Assembler module"""
import sys; sys.path.insert(0, ".")
from core.prompts import PromptAssembler, build_system_prompt


def test_default_assistant_prompt():
    p1 = build_system_prompt(role="assistant")
    assert "Worldwave" in p1
    assert len(p1) > 100


def test_expert_mode_prompt():
    p2 = build_system_prompt(role="expert")
    assert "expert" in p2.lower() or "professional" in p2 or "technical" in p2
    assert len(p2) > 50


def test_autonomous_mode_prompt():
    p3 = build_system_prompt(role="autonomous")
    assert "autonomous" in p3.lower() or "autonomous" in p3
    assert len(p3) > 50


def test_custom_config_assembler():
    config = {
        "role": "expert",
        "expert_mode": True,
        "show_env_info": True,
        "tools_enabled": True,
        "subconscious_enabled": True,
    }
    assembler = PromptAssembler(config=config)
    p4 = assembler.build()
    assert "OS" in p4 or "Linux" in p4 or "macOS" in p4 or "Darwin" in p4
    assert "tool" in p4.lower() or "tool" in p4
    assert "subconscious" in p4.lower() or "subconscious" in p4


def test_override_prompt():
    p5 = build_system_prompt(role="assistant", extra_instruction="Be verbose")
    assert "verbose" in p5.lower() or "Be verbose" in p5
