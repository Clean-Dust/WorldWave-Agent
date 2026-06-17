"""
Visual Diff Engine — ANSI terminal + API unified diff.

Provides Claude Code / Cursor quality visual diffs:
  - Unified diff with ANSI color
  - Side-by-side view
  - Structured JSON diff for API consumers
  - Auto-snapshot before editing for checkpoint/rollback

Usage:
  from core.diff import DiffEngine
  d = DiffEngine()
  d.snapshot(path)           # save before-state
  # ... edit file ...
  result = d.diff(path)      # generate colored diff
  print(result.ansi)         # terminal output
  result.json()              # API output
"""

import difflib
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ── ANSI color codes ────────────────────────────────────────────

class ANSI:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"


# ── Data structures ──────────────────────────────────────────────

@dataclass
class HunkLine:
    """A single line in a diff hunk."""
    kind: str          # "+", "-", " ", "@"
    content: str
    old_lineno: Optional[int] = None
    new_lineno: Optional[int] = None


@dataclass
class Hunk:
    """One hunk of changes."""
    header: str                    # "@@ -1,5 +1,6 @@"
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class DiffResult:
    """Complete diff result for a file."""
    path: str
    old_content: str
    new_content: str
    old_hash: str
    new_hash: str
    hunks: List[Hunk] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)  # {added, removed, unchanged}
    timestamp: str = ""

    @property
    def ansi(self) -> str:
        """Render as ANSI-colored unified diff for terminal."""
        return _render_ansi(self)

    @property
    def plain(self) -> str:
        """Render as plain unified diff."""
        return _render_plain(self)

    @property
    def side_by_side(self) -> str:
        """Render as ANSI side-by-side view."""
        return _render_side_by_side(self)

    def json(self) -> dict:
        """Structured JSON for API consumers."""
        return _to_json(self)

    @property
    def summary(self) -> str:
        """One-line summary: "+3 -1 in path/to/file.py"."""
        a = self.stats.get("added", 0)
        r = self.stats.get("removed", 0)
        return f"+{a} -{r} in {self.path}"


# ── Engine ───────────────────────────────────────────────────────

class DiffEngine:
    """Visual diff engine with snapshot-based before/after tracking."""

    def __init__(self, context_lines: int = 3, max_width: int = 120):
        self.context_lines = context_lines
        self.max_width = max_width
        self._snapshots: Dict[str, str] = {}  # path → content

    # ── Snapshot management ──────────────────────────────────────

    def snapshot(self, path: str) -> str:
        """Save current file state for later diff. Returns content hash."""
        if not os.path.exists(path):
            self._snapshots[path] = ""
            return "0" * 40
        content = _read_file(path)
        self._snapshots[path] = content
        return hashlib.sha1(content.encode()).hexdigest()

    def snapshot_content(self, path: str, content: str) -> str:
        """Save a content string as the 'before' state."""
        self._snapshots[path] = content
        return hashlib.sha1(content.encode()).hexdigest()

    def clear_snapshot(self, path: str):
        """Remove snapshot for a path."""
        self._snapshots.pop(path, None)

    def has_snapshot(self, path: str) -> bool:
        """Check if a snapshot exists for path."""
        return path in self._snapshots

    # ── Diff operations ──────────────────────────────────────────

    def diff(self, path: str, new_content: Optional[str] = None) -> Optional[DiffResult]:
        """Generate diff for a file. Uses snapshot as old, current file or new_content as new."""
        old = self._snapshots.get(path)
        if old is None:
            return None

        if new_content is None:
            if not os.path.exists(path):
                new = ""
            else:
                new = _read_file(path)
        else:
            new = new_content

        return self._compute(path, old, new)

    def diff_strings(self, old: str, new: str, path: str = "<string>") -> DiffResult:
        """Diff two strings directly."""
        return self._compute(path, old, new)

    def diff_paths(self, old_path: str, new_path: str) -> DiffResult:
        """Diff two files on disk."""
        old = _read_file(old_path)
        new = _read_file(new_path)
        return self._compute(new_path, old, new)

    # ── Batch diff (multiple files) ──────────────────────────────

    def diff_all(self) -> List[DiffResult]:
        """Generate diffs for all snapshotted files."""
        results = []
        for path in self._snapshots:
            r = self.diff(path)
            if r and (r.stats.get("added", 0) > 0 or r.stats.get("removed", 0) > 0):
                results.append(r)
        return results

    def batch_summary(self) -> str:
        """Summary of all changed files."""
        results = self.diff_all()
        if not results:
            return "No changes."
        lines = [f"{len(results)} file(s) changed:"]
        total_added = 0
        total_removed = 0
        for r in results:
            lines.append(f"  {r.summary}")
            total_added += r.stats.get("added", 0)
            total_removed += r.stats.get("removed", 0)
        lines.append("  ───────────────")
        lines.append(f"  +{total_added} -{total_removed} total")
        return "\n".join(lines)

    # ── Internal ─────────────────────────────────────────────────

    def _compute(self, path: str, old: str, new: str) -> DiffResult:
        """Compute diff between old and new content."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        ops = matcher.get_opcodes()

        hunks = []
        stats = {"added": 0, "removed": 0, "unchanged": 0}

        # Group operations into hunks with context
        for tag, i1, i2, j1, j2 in ops:
            if tag == "equal":
                stats["unchanged"] += (i2 - i1)
                continue

            # Build hunk: include context lines before and after
            ctx = self.context_lines
            hunk_old_start = max(0, i1 - ctx)
            hunk_old_end = min(len(old_lines), i2 + ctx)
            hunk_new_start = max(0, j1 - ctx)
            hunk_new_end = min(len(new_lines), j2 + ctx)

            # Header
            old_count = hunk_old_end - hunk_old_start
            new_count = hunk_new_end - hunk_new_start
            header = f"@@ -{hunk_old_start + 1},{old_count} +{hunk_new_start + 1},{new_count} @@"

            hunk = Hunk(
                header=header,
                old_start=hunk_old_start + 1,
                old_count=old_count,
                new_start=hunk_new_start + 1,
                new_count=new_count,
            )

            # Context before change
            for k in range(hunk_old_start, i1):
                hunk.lines.append(HunkLine(
                    kind=" ", content=old_lines[k].rstrip("\n"),
                    old_lineno=k + 1,
                    new_lineno=hunk_new_start + (k - hunk_old_start) + 1,
                ))

            # Removed lines
            for k in range(i1, i2):
                hunk.lines.append(HunkLine(
                    kind="-", content=old_lines[k].rstrip("\n"),
                    old_lineno=k + 1,
                ))
                stats["removed"] += 1

            # Added lines
            for k in range(j1, j2):
                hunk.lines.append(HunkLine(
                    kind="+", content=new_lines[k].rstrip("\n"),
                    new_lineno=k + 1,
                ))
                stats["added"] += 1

            # Context after change
            for k in range(j2, hunk_new_end):
                hunk.lines.append(HunkLine(
                    kind=" ", content=new_lines[k].rstrip("\n"),
                    old_lineno=i2 + (k - j2) + 1 if i2 + (k - j2) < len(old_lines) else None,
                    new_lineno=k + 1,
                ))

            hunks.append(hunk)

        result = DiffResult(
            path=path,
            old_content=old,
            new_content=new,
            old_hash=hashlib.sha1(old.encode()).hexdigest(),
            new_hash=hashlib.sha1(new.encode()).hexdigest(),
            hunks=hunks,
            stats=stats,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        return result


# ── Rendering ────────────────────────────────────────────────────

def _render_ansi(result: DiffResult) -> str:
    """Render unified diff with ANSI colors."""
    lines = []
    # Header
    lines.append(f"{ANSI.BOLD}{ANSI.YELLOW}─── a/{result.path}{ANSI.RESET}")
    lines.append(f"{ANSI.BOLD}{ANSI.GREEN}+++ b/{result.path}{ANSI.RESET}")

    for hunk in result.hunks:
        lines.append(f"{ANSI.CYAN}{hunk.header}{ANSI.RESET}")
        for hl in hunk.lines:
            lineno = ""
            if hl.kind == " ":
                lines.append(f"  {hl.content}")
            elif hl.kind == "-":
                lines.append(f"{ANSI.BG_RED}{ANSI.RED}- {hl.content}{ANSI.RESET}")
            elif hl.kind == "+":
                lines.append(f"{ANSI.BG_GREEN}{ANSI.GREEN}+ {hl.content}{ANSI.RESET}")

    # Stats footer
    a = result.stats.get("added", 0)
    r = result.stats.get("removed", 0)
    lines.append(f"{ANSI.DIM}── +{a} -{r}{ANSI.RESET}")
    return "\n".join(lines)


def _render_plain(result: DiffResult) -> str:
    """Render plain unified diff (no ANSI)."""
    lines = [f"--- a/{result.path}", f"+++ b/{result.path}"]
    for hunk in result.hunks:
        lines.append(hunk.header)
        for hl in hunk.lines:
            lines.append(f"{hl.kind} {hl.content}")
    return "\n".join(lines)


def _render_side_by_side(result: DiffResult, width: int = 80) -> str:
    """Render side-by-side diff with ANSI colors."""
    half = width // 2 - 3
    lines = []
    lines.append(f"{ANSI.BOLD}{'OLD':^{half}}{ANSI.DIM} │ {ANSI.BOLD}{'NEW':^{half}}{ANSI.RESET}")
    lines.append(f"{ANSI.DIM}{'─' * half}─┼─{'─' * half}{ANSI.RESET}")

    for hunk in result.hunks:
        lines.append(f"{ANSI.CYAN}── {hunk.header}{ANSI.RESET}")
        for hl in hunk.lines:
            content = hl.content[:half]
            left = ""
            right = ""
            if hl.kind == " ":
                left = f"{ANSI.DIM}{content:<{half}}{ANSI.RESET}"
                right = f"{ANSI.DIM}{content:<{half}}{ANSI.RESET}"
            elif hl.kind == "-":
                left = f"{ANSI.RED}{content:<{half}}{ANSI.RESET}"
                right = " " * half
            elif hl.kind == "+":
                left = " " * half
                right = f"{ANSI.GREEN}{content:<{half}}{ANSI.RESET}"
            lines.append(f"{left} {ANSI.DIM}│{ANSI.RESET} {right}")

    return "\n".join(lines)


def _to_json(result: DiffResult) -> dict:
    """Structured JSON representation for API consumers."""
    return {
        "path": result.path,
        "old_hash": result.old_hash,
        "new_hash": result.new_hash,
        "stats": result.stats,
        "timestamp": result.timestamp,
        "hunks": [
            {
                "header": h.header,
                "old_start": h.old_start,
                "old_count": h.old_count,
                "new_start": h.new_start,
                "new_count": h.new_count,
                "lines": [
                    {
                        "kind": hl.kind,
                        "content": hl.content,
                        "old_lineno": hl.old_lineno,
                        "new_lineno": hl.new_lineno,
                    }
                    for hl in h.lines
                ],
            }
            for h in result.hunks
        ],
    }


# ── Helpers ──────────────────────────────────────────────────────

def _read_file(path: str) -> str:
    """Read file content, normalize line endings."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ── Singleton ────────────────────────────────────────────────────

_diff_engine: Optional[DiffEngine] = None


def get_diff_engine() -> DiffEngine:
    """Get the global DiffEngine singleton."""
    global _diff_engine
    if _diff_engine is None:
        _diff_engine = DiffEngine()
    return _diff_engine
