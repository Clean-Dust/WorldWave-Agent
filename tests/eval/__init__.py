"""
tests/eval/__init__.py — Worldwave evaluation benchmark framework v0.1

Design goal: Use objective data to prove WW's 3-layer Smart Degradation is superior to a single API.

Currently covers:
1. tool_correctness — tool call accuracy (JSON format, parameter match rate)
2. task_completion — task completion rate (end-to-end goal achievement)
3. recovery — crash recovery capability (forced interrupt resume accuracy)

 and SWE-bench / WebArena standard test set integration WIP.
"""
