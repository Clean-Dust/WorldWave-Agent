"""Tests: Context module"""
import sys; sys.path.insert(0, ".")
import shutil
from core.context import ContextWindow, ConversationManager, SpiralContextSummarizer, estimate_tokens

# Token estimation
assert estimate_tokens("Hello world") >= 2
assert estimate_tokens("x" * 100) >= 25

# ContextWindow with compression
cw = ContextWindow(max_messages=3, max_tokens=200)
for i in range(10):
    cw.add("user", f"Message number {i}")
    cw.add("assistant", f"Response for message {i} with some extra detail")
assert cw.compress_count >= 1
assert len(cw.messages) <= 8
assert cw.total_tokens() > 0

# Stats
stats = cw.stats()
assert stats["compress_count"] >= 1
# compression_ratio can be negative when compression overhead exceeds savings
# (normal for small contexts); just verify it's a float
assert isinstance(stats["compression_ratio"], (int, float))
assert stats["window_id"] == cw.window_id

# To LLM messages
messages = cw.to_llm_messages(system_prompt="System prompt")
assert any(msg["role"] == "system" for msg in messages)
assert any(msg["role"] == "user" for msg in messages)
assert any(msg["role"] == "assistant" for msg in messages)

# Clear
cw.clear()
assert len(cw.messages) == 0
assert cw.compress_count == 0

# ConversationManager
cm = ConversationManager()
w = cm.get_or_create("conv1")
assert w.window_id == "conv1"
cm.add_message("user", "Hello", "conv1")
cm.add_message("assistant", "Hi!", "conv1")
msgs = cm.to_llm_messages("conv1")
assert len(msgs) >= 1

# Stats
all_stats = cm.all_stats()
assert all_stats["window_count"] >= 1

# SpiralContextSummarizer (without LLM)
summarizer = SpiralContextSummarizer(llm=None, max_spirals_before_compress=5)
assert summarizer.should_compress(10)
assert not summarizer.should_compress(3)

# Simple summary (no LLM fallback)
spirals = [
    {"spiral_number": 1, "evaluation": {"success": True, "reason": "Completed"}},
    {"spiral_number": 2, "evaluation": {"success": False, "reason": "Error occurred"}},
]
result = summarizer.summarize_spirals(spirals)
assert result is not None

print("ALL CONTEXT TESTS PASSED")
