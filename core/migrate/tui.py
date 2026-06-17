"""Migration TUI & Cross-Platform Conversation History Importer v0.1

Interactive terminal wizard for guided migration, plus import of
conversation histories from other AI agent frameworks.

Features:
  - Environment scan with diff-tree preview
  - Step-by-step guided migration (InquirerPy-free, pure text UI)
  - Claude / Hermes / OpenClaw session log → semantic summary → WW memory
  - Rollback confirmation with snapshot preview
"""

from __future__ import annotations
import datetime
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.migrate.tui")


# ══════════════════════════════════════════════════════════════
# Interactive Migration Wizard
# ══════════════════════════════════════════════════════════════

@dataclass
class MigrationWizard:
    """Simple interactive migration wizard (no external TUI deps)."""

    def run(self, detected: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Run the interactive migration wizard.

        Args:
            detected: Results from detect_and_list()

        Returns:
            User's migration choices or None if cancelled
        """
        print("\n" + "="*60)
        print("  Worldwave Migration Wizard")
        print("="*60)

        if not detected:
            print("\n  No other AI agent systems detected.")
            print("  Nothing to migrate.\n")
            return None

        # Show what was found
        print(f"\n  Found {len(detected)} system(s):\n")
        for i, d in enumerate(detected, 1):
            status = "[RUNNING]" if d.get("running") else "[idle]"
            print(f"  [{i}] {d['source']:15} {status}")
            if d.get("paths"):
                for p in d["paths"][:3]:
                    print(f"      {p}")

        # Show diff-tree preview
        self._render_diff_tree(detected)

        # Confirmation
        print("\n  Options:")
        print("  [a] Migrate ALL")
        for i, d in enumerate(detected, 1):
            print(f"  [{i}] Migrate {d['source']} only")
        print("  [d] Dry-run preview (no changes)")
        print("  [q] Quit")

        choice = input("\n  Choice [a]: ").strip().lower() or "a"

        if choice == "q":
            print("  Cancelled.\n")
            return None

        if choice == "d":
            return {"dry_run": True, "sources": [d["source"] for d in detected]}

        if choice == "a":
            return {"sources": [d["source"] for d in detected]}

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(detected):
                return {"sources": [detected[idx]["source"]]}
        except ValueError:
            pass

        return {"sources": [d["source"] for d in detected]}

    @staticmethod
    def _render_diff_tree(detected: List[Dict[str, Any]]):
        """Render a tree diff preview of migration changes (Gemini Pillar 2).

        Shows what will be created (+), modified (~), and any warnings (!)
        before the user commits to migration.
        """
        print(f"\n  {Colors.bold('Migration Preview')}")
        print(f"  {'─' * 40}")

        for d in detected:
            source = d["source"]
            running = d.get("running", False)

            # Root node
            prefix = "  "
            print(f"{prefix}{Colors.cyan('┌─')} {Colors.bold(source)}")

            # Config
            print(f"{prefix}{Colors.dim('│')} {Colors.green('+')} Config → ~/.worldwave/config.json")

            # Skills
            print(f"{prefix}{Colors.dim('│')} {Colors.green('+')} Skills → ~/.worldwave/skills/")

            # Aliases
            print(f"{prefix}{Colors.dim('│')} {Colors.green('+')} Aliases → shell RC files")

            # MCP servers
            print(f"{prefix}{Colors.dim('│')} {Colors.green('+')} MCP servers → ~/.worldwave/mcp_servers.json")

            # Slash commands
            print(f"{prefix}{Colors.dim('│')} {Colors.green('+')} Slash commands → ~/.worldwave/slash_compat.json")

            # Memory
            print(f"{prefix}{Colors.dim('│')} {Colors.green('+')} Memory → ~/.worldwave/data/memory/")

            # Source backup
            for p in d.get("paths", [])[:2]:
                p_short = os.path.basename(p)
                print(f"{prefix}{Colors.dim('│')} {Colors.yellow('~')} Backup → snapshots/{source}/{p_short}")

            # Service warning
            if running:
                services = ", ".join(d.get("services", []))
                print(f"{prefix}{Colors.dim('│')} {Colors.red('!')} {Colors.yellow('Will stop services: ' + services)}")

            # Footer
            print(f"{prefix}{Colors.dim('└─')} {Colors.green(str(d.get('items', 0)) + ' items')} ready to migrate")

            if d != detected[-1]:
                print("")


# Color helper for TUI (no external deps)
class Colors:
    """ANSI color codes for terminal output."""
    @staticmethod
    def bold(s): return f"\033[1m{s}\033[0m"
    @staticmethod
    def dim(s): return f"\033[2m{s}\033[0m"
    @staticmethod
    def green(s): return f"\033[32m{s}\033[0m"
    @staticmethod
    def yellow(s): return f"\033[33m{s}\033[0m"
    @staticmethod
    def red(s): return f"\033[31m{s}\033[0m"
    @staticmethod
    def cyan(s): return f"\033[36m{s}\033[0m"


# ══════════════════════════════════════════════════════════════
# Cross-Platform Conversation History Importer
# ══════════════════════════════════════════════════════════════

@dataclass
class ConversationImporter:
    """Import conversation histories from other AI agent frameworks.

    Extraction sources:
      - Claude Code: ~/.claude/conversations/*.jsonl
      - Hermes Agent: ~/.hermes/data/messages.db (SQLite)
      - OpenClaw: ~/.openclaw/sessions/*.jsonl

    Processing pipeline:
      Extract → Summarize (SLM) → Vectorize → Inject into WW memory
    """

    ww_memory_dir: str = field(default_factory=lambda: os.path.expanduser("~/.worldwave/data/memory/"))

    def import_all(self, sources: Optional[List[str]] = None) -> Dict[str, int]:
        """Import conversations from all detected or specified sources.

        Returns: {source: entries_imported}
        """
        results = {}

        extractors = {
            "hermes": self._extract_hermes,
            "claude-code": self._extract_claude,
            "openclaw": self._extract_openclaw,
        }

        for source, extractor_fn in extractors.items():
            if sources and source not in sources:
                continue
            try:
                entries = extractor_fn()
                if entries:
                    count = self._write_to_memory(source, entries)
                    results[source] = count
                    logger.info("Imported %d conversations from %s", count, source)
            except Exception as e:
                logger.warning("Failed to import %s conversations: %s", source, e)

        return results

    # ── Extractors ───────────────────────────────────────────────

    @staticmethod
    def _extract_hermes() -> List[Dict[str, Any]]:
        """Extract conversation history from Hermes SQLite message DB."""
        entries = []
        db_paths = [
            os.path.expanduser("~/.hermes/data/messages.db"),
            os.path.expanduser("~/.hermes/memory.db"),
        ]
        for db_path in db_paths:
            if not os.path.isfile(db_path):
                continue
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                # Try known table names
                for table in ("messages", "conversations", "sessions"):
                    try:
                        cursor.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 500")
                        for row in cursor.fetchall():
                            entry = dict(row)
                            entry["_source"] = "hermes"
                            entry["_imported_at"] = datetime.datetime.now().isoformat()
                            entries.append(entry)
                        break
                    except sqlite3.OperationalError:
                        continue
                conn.close()
            except Exception as e:
                logger.debug("Hermes conversation extract error: %s", e)

        return entries

    @staticmethod
    def _extract_claude() -> List[Dict[str, Any]]:
        """Extract conversation history from Claude Code JSONL files."""
        entries = []
        conv_dir = os.path.expanduser("~/.claude/conversations/")
        if not os.path.isdir(conv_dir):
            # Try alternate locations
            for alt in ("~/.claude/sessions/", "~/.claude/history/"):
                alt_dir = os.path.expanduser(alt)
                if os.path.isdir(alt_dir):
                    conv_dir = alt_dir
                    break
            else:
                return entries

        for fname in sorted(os.listdir(conv_dir), reverse=True)[:50]:
            if not fname.endswith((".jsonl", ".json")):
                continue
            fpath = os.path.join(conv_dir, fname)
            try:
                with open(fpath, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            msg["_source"] = "claude-code"
                            msg["_imported_at"] = datetime.datetime.now().isoformat()
                            entries.append(msg)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.debug("Claude conversation parse error: %s - %s", fpath, e)

        return entries

    @staticmethod
    def _extract_openclaw() -> List[Dict[str, Any]]:
        """Extract conversation history from OpenClaw sessions."""
        entries = []
        sessions_dir = os.path.expanduser("~/.openclaw/sessions/")
        if not os.path.isdir(sessions_dir):
            return entries

        for fname in sorted(os.listdir(sessions_dir), reverse=True)[:50]:
            if not fname.endswith((".jsonl", ".json")):
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                with open(fpath, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            msg["_source"] = "openclaw"
                            msg["_imported_at"] = datetime.datetime.now().isoformat()
                            entries.append(msg)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.debug("OpenClaw session parse error: %s - %s", fpath, e)

        return entries

    # ── Write to WW memory ───────────────────────────────────────

    def _write_to_memory(self, source: str, entries: List[Dict[str, Any]]) -> int:
        """Write imported conversation entries to WW memory store.

        Two outputs:
        1. JSONL file for WW memory system (hippocampus ingestion)
        2. context_injection.json — high-density summary for initial context injection
           (Gemini Pillar 5: lossless context transfer)
        """
        os.makedirs(self.ww_memory_dir, exist_ok=True)
        memory_path = os.path.join(self.ww_memory_dir, f"imported_{source}_conversations.jsonl")

        written = 0
        with open(memory_path, "a") as f:
            for entry in entries:
                # Convert to WW memory atom format
                atom = {
                    "type": "conversation_import",
                    "source": source,
                    "content": self._summarize_entry(entry, source),
                    "original": entry,
                    "trust": 0.5,  # Imported content gets moderate trust
                    "imported_at": datetime.datetime.now().isoformat(),
                }
                f.write(json.dumps(atom, ensure_ascii=False) + "\n")
                written += 1

        # Generate and write context injection summary
        if entries:
            summary = semantic_summarize(entries)
            if summary:
                injection_path = os.path.join(
                    self.ww_memory_dir, f"context_injection_{source}.json"
                )
                injection = {
                    "source": source,
                    "generated_at": datetime.datetime.now().isoformat(),
                    "total_messages": summary.get("total_messages", 0),
                    "time_range": summary.get("time_range", {}),
                    "decisions": summary.get("decisions", []),
                    "unfinished_tasks": summary.get("unfinished_tasks", []),
                    "key_files": summary.get("key_files", []),
                    "usage": (
                        "WW loads this file on first run after migration to provide "
                        "immediate project context without requiring the user to "
                        "re-explain the codebase state."
                    ),
                }
                with open(injection_path, "w") as f:
                    json.dump(injection, f, indent=2, ensure_ascii=False)

        return written

    @staticmethod
    def _summarize_entry(entry: Dict[str, Any], source: str) -> str:
        """Create a text summary of a conversation entry for WW memory.

        This is a heuristic summarizer — ideally replaced by an SLM pass.
        """
        role = entry.get("role", entry.get("type", "unknown"))
        content = entry.get("content", entry.get("text", entry.get("message", "")))

        # Truncate long content
        if isinstance(content, str) and len(content) > 500:
            content = content[:500] + "..."

        # Extract key metadata
        timestamp = entry.get("timestamp", entry.get("created_at", "unknown"))
        model = entry.get("model", "unknown")

        parts = [
            f"[{source}] {role}",
            f"Content: {content}",
        ]
        if timestamp != "unknown":
            parts.append(f"Time: {timestamp}")
        if model != "unknown":
            parts.append(f"Model: {model}")

        return " | ".join(parts)


# ══════════════════════════════════════════════════════════════
# Semantically summarize long conversations (SLM placeholder)
# ══════════════════════════════════════════════════════════════

def semantic_summarize(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize a batch of conversation entries into a high-density
    semantic summary for injection into WW's initial context.

    Currently uses heuristic extraction. Future: integrate SLM.
    """
    if not entries:
        return {}

    summary = {
        "total_messages": len(entries),
        "time_range": {
            "earliest": None,
            "latest": None,
        },
        "topics": [],
        "decisions": [],
        "unfinished_tasks": [],
        "key_files": set(),
    }

    # Extract topics and decisions heuristically
    decision_keywords = ["decided", "decision", "agreed", "final", "conclusion",
                         "will use", "going with", "settled on"]
    task_keywords = ["TODO", "FIXME", "to do", "need to", "pending", "blocked",
                     "waiting for", "remaining", "left to"]

    for entry in entries:
        content = entry.get("content", entry.get("text", ""))
        if not isinstance(content, str):
            continue

        # Track timestamps
        ts = entry.get("timestamp", entry.get("created_at"))
        if ts:
            if not summary["time_range"]["earliest"] or ts < summary["time_range"]["earliest"]:
                summary["time_range"]["earliest"] = ts
            if not summary["time_range"]["latest"] or ts > summary["time_range"]["latest"]:
                summary["time_range"]["latest"] = ts

        # Detect decisions
        for kw in decision_keywords:
            if kw.lower() in content.lower():
                # Extract the sentence containing the keyword
                sentences = re.split(r'[.!?]+', content)
                for sent in sentences:
                    if kw.lower() in sent.lower():
                        summary["decisions"].append(sent.strip()[:200])
                        break
                break

        # Detect unfinished tasks
        for kw in task_keywords:
            if kw in content:
                sentences = re.split(r'[.!?]+', content)
                for sent in sentences:
                    if kw in sent:
                        summary["unfinished_tasks"].append(sent.strip()[:200])
                        break
                break

        # Extract file references
        file_refs = re.findall(r'(?:^|\s)([/~][\w./-]+\.\w{1,6})', content)
        summary["key_files"].update(file_refs)

    # Deduplicate
    summary["decisions"] = list(dict.fromkeys(summary["decisions"]))[:10]
    summary["unfinished_tasks"] = list(dict.fromkeys(summary["unfinished_tasks"]))[:10]
    summary["key_files"] = list(summary["key_files"])[:20]

    return summary
