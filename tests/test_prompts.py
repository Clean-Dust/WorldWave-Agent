"""Tests: Prompt Assembler module"""
import sys; sys.path.insert(0, ".")
from core.prompts import PromptAssembler, build_system_prompt

# Default assistant prompt
p1 = build_system_prompt(role="assistant")
assert "Worldwave" in p1
assert len(p1) > 100
print(f"Assistant prompt: {len(p1)} chars")

# Expert mode
p2 = build_system_prompt(role="expert")
assert "expert" in p2.lower() or "professional" in p2 or "technical" in p2
assert len(p2) > 50
print(f"Expert prompt: {len(p2)} chars")

# Autonomous mode
p3 = build_system_prompt(role="autonomous")
assert "autonomous" in p3.lower() or "autonomous" in p3
assert len(p3) > 50
print(f"Autonomous prompt: {len(p3)} chars")

# Custom config
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
print(f"Full config prompt: {len(p4)} chars")

# With additional overrides
p5 = build_system_prompt(role="assistant", extra_instruction="Be verbose")
assert "verbose" in p5.lower() or "Be verbose" in p5
print(f"Override prompt: {len(p5)} chars")

print("ALL PROMPT TESTS PASSED")
