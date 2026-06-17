"""
ww/core/subconscious/rule_dict.py — Optimize rule dictionary library

Subconscious does not output text, only outputs Rule ID. This dictionary is responsible for translating Rule ID into
System Prompt snippets, API parameters, or execute interception actions that the main consciousness can understand.

This is collective wisdom obtained from Nostr federated learning:
- Local can be directly edited (to meet personalization needs)
- P2P sync (share common rules)
- Default built-in set of validated core rules

ruletype：
  system_prompt → inject into <system> tag text fragment
  param_tune → modify LLM API parameters (temperature, top_p, etc.)
  action_code → execute action (lint, check AST, retry, etc.)

Rule ID encode：
  0-99 reserved for built-in rules
  100-199 community sync rule (Nostr federated learning)
  200-255 user custom rule (local only)
"""

from __future__ import annotations
import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger("ww.subconscious.rule_dict")

RULE_DICT_FILE = os.path.expanduser("~/worldwave/data/subconscious/rule_dict.json")

# ── Built-in rules ──
# Rule ID = index, never changes. New rules appended to the end.
# Community sync rules will be appended at the local end, ids allocated by sync negotiation.

BUILTIN_RULES = [
    # id=0: no operation (subconscious thinks everything is normal)
    {"id": 0, "type": "noop", "trigger": "everything is normal",
     "content": None},

    # id=1: network request anti-blocking
    {"id": 1, "type": "system_prompt", "trigger": "detect network request",
     "content": "note：thistaskinvolves and externalnetworkrequest。pleaseuseRandomlatency interval，"
                "andat each timerequest Add 1-5 seconds Random wait。"
                "if encounterto  HTTP 429/403，Switch User-Agent And exponential backoffretry。"},

    # id=2: reduce hallucination (precise mode)
    {"id": 2, "type": "param_tune", "trigger": "needs precise math/logic inference",
     "content": {"temperature": 0.1, "top_p": 0.5, "max_tokens": 4096}},

    # id=3: creative mode
    {"id": 3, "type": "param_tune", "trigger": "needs creative/divergent thinking",
     "content": {"temperature": 0.8, "top_p": 0.9}},

    # id=4: code execute first lint
    {"id": 4, "type": "action_code", "trigger": "code security check",
     "content": "Execute_Linter_First"},

    # id=5: compress context then send
    {"id": 5, "type": "system_prompt", "trigger": "context too long",
     "content": "hint：when  Dialoguecontext Approachlength limit。pleaseat Reply Prioritize giving the most critical Information，"
                "avoidRepeat it mentionto  content。May considerusesummaryTo retain key information。"},

    # id=6: optimize crawler line
    {"id": 6, "type": "system_prompt", "trigger": "web crawling task",
     "content": "strategyhint：proceedlineWeb crawler ，Priorityuse request + BeautifulSoup Combination，"
                "onlyat  JavaScript Rendering necessary thenuse Selenium。"
                "Add random delay and retry Mechanism。"},

    # id=7: memorysecuremode
    {"id": 7, "type": "action_code", "trigger": "memoryuseexception",
     "content": "Memory_Safe_Mode"},

    # id=8: API rate limiting
    {"id": 8, "type": "param_tune", "trigger": "API latencyexception",
     "content": {"temperature": 0.3, "top_p": 0.7, "max_tokens": 2048}},

    # id=9: needs retrieval history
    {"id": 9, "type": "action_code", "trigger": "needs memory backtrack",
     "content": "Recall_History"},

    # id=10: clean output (avoid duplication)
    {"id": 10, "type": "system_prompt", "trigger": "output duplication loop",
     "content": "note: system detects to duplication mode. Please try a completely different strategy or angle to reply to the question."
                "if stuck, first briefly summarize state, then give a completely new solution."},
]


class RuleDictionary:
    """
    Optimize rule dictionary library.

    Responsible for:
    - load/saverule
    - based on Rule ID retrieve rule
    - export/import (Nostr sync)
    - community rule merge
    """

    def __init__(self, rules_file: str = RULE_DICT_FILE):
        self.rules_file = rules_file
        self._rules: Dict[int, dict] = {}  # id -> rule
        self._loaded = False
        self._dirty = False

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.rules_file), exist_ok=True)

    def load(self):
        if self._loaded:
            return
        self._rules = {}

        # load built-in rule
        for rule in BUILTIN_RULES:
            self._rules[rule["id"]] = dict(rule)

        # load local rule (overwrite/append)
        self._ensure_dir()
        if os.path.exists(self.rules_file):
            try:
                with open(self.rules_file) as f:
                    local_rules = json.load(f)
                    if isinstance(local_rules, list):
                        for rule in local_rules:
                            rid = rule.get("id")
                            if rid is not None:
                                self._rules[rid] = rule
                    elif isinstance(local_rules, dict):
                        for rid, rule in local_rules.items():
                            self._rules[int(rid)] = rule
                    logger.info(f"Rule dict loaded: {len(local_rules)} local rules, "
                                f"{len(self._rules)} total")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Rule dict load failed: {e}")

        self._loaded = True

    def save(self):
        self._ensure_dir()
        # only save non-built-in rules (built-in rules provided by code)
        custom = {rid: rule for rid, rule in self._rules.items()
                  if rule.get("source") != "builtin" and rid > 99}
        with open(self.rules_file, "w") as f:
            json.dump(list(custom.values()), f, indent=2, ensure_ascii=False)
        logger.info(f"Rule dict saved: {len(custom)} custom rules")

    def get(self, rule_id: int) -> Optional[dict]:
        self.load()
        return self._rules.get(rule_id)

    def get_by_type(self, rule_type: str) -> List[dict]:
        self.load()
        return [r for r in self._rules.values() if r.get("type") == rule_type]

    def add_rule(self, rule: dict, source: str = "local") -> int:
        """Add rule, auto assign ID or use custom ID."""
        self.load()
        if "id" not in rule or rule["id"] is None:
            # auto assign ID
            used = set(self._rules.keys())
            rule["id"] = max(used) + 1 if used else 0
        rule["source"] = source
        self._rules[rule["id"]] = rule
        self._dirty = True
        self.save()
        return rule["id"]

    def remove_rule(self, rule_id: int):
        self.load()
        if rule_id in self._rules:
            del self._rules[rule_id]
            self.save()

    def export_for_sync(self) -> dict:
        """Export non-built-in rules for Nostr sync."""
        self.load()
        rules = [r for r in self._rules.values()
                 if r.get("source") in ("local", "community")]
        return {
            "version": 1,
            "rules": rules,
        }

    def merge_synced(self, remote_rules: List[dict]):
        """
        merge rules from Nostr community.
        conflict keep newer version (based on timestamp) or local version.
        """
        self.load()
        merged = 0
        for rule in remote_rules:
            rid = rule.get("id")
            if rid is None or rid < 100:
                continue  # do not overwrite built-in rule
            existing = self._rules.get(rid)
            if existing and existing.get("source") == "local":
                continue  # keep local customization
            self._rules[rid] = rule
            merged += 1
        if merged:
            self.save()
            logger.info(f"Merged {merged} community rules from sync")

    def all_rules(self) -> List[dict]:
        self.load()
        return list(self._rules.values())

    def stats(self) -> dict:
        self.load()
        types = {}
        sources = {}
        for r in self._rules.values():
            types[r.get("type", "unknown")] = types.get(r.get("type", "unknown"), 0) + 1
            sources[r.get("source", "unknown")] = sources.get(r.get("source", "unknown"), 0) + 1
        return {
            "total": len(self._rules),
            "by_type": types,
            "by_source": sources,
        }
