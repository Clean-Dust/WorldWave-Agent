"""ww/pm/aci.py — Defensive Agent-Computer Interface v0.1

Implements Gemini's WW-PM Subsystem 2 (Defensive ACI):
- Windowed file viewer with coordinate-awareness (SWE-agent inspired)
- Atomic syntax-verified editor with auto-rollback
- Block-diff replace format for safe code editing

Architecture:
  WindowedFileViewer — stateful file reading with pagination, overlap, and goto
  DefensiveEditor   — atomic write → lint → commit/rollback lifecycle
"""

from __future__ import annotations
import ast
import os
import subprocess
import tempfile
from typing import Dict, List


# ── Windowed File Viewer ──────────────────────────────────────────────

class WindowState:
    """Tracking the viewer's current position in a file."""

    def __init__(self, path: str = "", offset: int = 1, total_lines: int = 0):
        self.path = path
        self.offset = offset  # 1-indexed first line shown
        self.total_lines = total_lines

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "offset": self.offset,
            "total_lines": self.total_lines,
        }


class WindowedFileViewer:
    """ACI-safe file viewer with paging, overlap, and coordinate awareness.

    Key design:
    - Default window: 100 lines (configurable)
    - Overlap: 3 lines on each edge when scrolling
    - Goto offset: target line placed at ~1/6 of window (not top)
    - State metadata attached as JSON edge channel
    """

    WINDOW_SIZE = 100
    OVERLAP = 3
    MAX_FILE_SIZE = 200 * 1024  # 200KB hard limit for reading

    def __init__(self, window_size: int = WINDOW_SIZE, overlap: int = OVERLAP):
        self._window_size = window_size
        self._overlap = min(overlap, window_size // 4)
        self._state = WindowState()

    @property
    def state(self) -> Dict:
        return self._state.to_dict()

    def open(self, path: str) -> Dict:
        """Open a file for viewing. Returns the first window."""
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            return {"error": f"File not found: {path}"}

        file_size = os.path.getsize(path)
        if file_size > self.MAX_FILE_SIZE:
            return {"error": f"File too large ({file_size} bytes > {self.MAX_FILE_SIZE} limit)"}

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)
        self._state = WindowState(path=path, offset=1, total_lines=total)
        return self._read_window(lines, 1, total)

    def scroll_down(self, lines_count: int = None) -> Dict:
        """Scroll forward by lines_count (default: window_size - overlap)."""
        lines_count = lines_count or (self._window_size - self._overlap)
        new_offset = min(self._state.offset + lines_count, self._state.total_lines)
        return self._goto(new_offset)

    def scroll_up(self, lines_count: int = None) -> Dict:
        """Scroll backward by lines_count (default: window_size - overlap)."""
        lines_count = lines_count or (self._window_size - self._overlap)
        new_offset = max(self._state.offset - lines_count, 1)
        return self._goto(new_offset)

    def goto(self, line_num: int) -> Dict:
        """Jump to a specific line. Target placed at ~1/6 of window."""
        if line_num < 1:
            return {"error": "Line number must be >= 1"}
        return self._goto(line_num)

    def _goto(self, target_line: int) -> Dict:
        """Internal goto with smart offset."""
        target_line = max(1, min(target_line, self._state.total_lines))
        path = self._state.path
        if not path or not os.path.isfile(path):
            return {"error": "No file open"}

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)
        # Place target at 1/6 of window (not top)
        offset = max(1, target_line - self._window_size // 6)
        self._state = WindowState(path=path, offset=offset, total_lines=total)
        return self._read_window(lines, offset, total)

    def _read_window(self, lines: List[str], offset: int, total: int) -> Dict:
        """Extract a window of lines with metadata."""
        end = min(offset + self._window_size, total + 1)
        window_lines = lines[offset - 1 : end - 1]
        content = "".join(window_lines)

        result = {
            "content": content,
            "metadata": {
                "path": self._state.path,
                "start_line": offset,
                "end_line": end - 1,
                "total_lines": total,
                "window_size": len(window_lines),
            },
        }

        if offset > 1:
            result["metadata"]["previous_start"] = max(1, offset - self._window_size)
        if end - 1 < total:
            result["metadata"]["next_start"] = end

        return result

    def close(self) -> Dict:
        """Close the current file."""
        path = self._state.path
        self._state = WindowState()
        return {"closed": path}


# ── Defensive Code Editor ─────────────────────────────────────────────

class ValidationResult:
    """Result of a syntax/lint check."""

    def __init__(self, valid: bool, errors: List[str] = None):
        self.valid = valid
        self.errors = errors or []
        self.error_text = "\n".join(self.errors)


class DefensiveEditor:
    """Atomic syntax-verified code editor with auto-rollback.

    Each edit follows:
    1. Read original content
    2. Apply change in memory buffer
    3. Run syntax/lint check on buffer
    4. If valid → write to disk; if invalid → return error, keep original
    """

    def __init__(self, lint_enabled: bool = True):
        self._lint_enabled = lint_enabled

    def edit_lines(
        self, path: str, start_line: int, end_line: int, new_content: str
    ) -> Dict:
        """Replace lines start_line..end_line with new_content.

        Args:
            path: Absolute or relative file path
            start_line: First line to replace (1-indexed, inclusive)
            end_line: Last line to replace (1-indexed, inclusive)
            new_content: Replacement text (can be multi-line)

        Returns:
            Dict with:
            - success: bool
            - diff: unified diff string
            - errors: list of lint/syntax errors (if failed)
            - rollback: bool (True if changes were rolled back)
        """
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            return {"success": False, "error": f"File not found: {path}"}

        # Read original content
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            original_lines = f.readlines()

        if start_line < 1 or end_line > len(original_lines):
            return {
                "success": False,
                "error": f"Line range {start_line}-{end_line} out of range (1-{len(original_lines)})",
            }

        # Build new content
        new_lines = new_content.splitlines(keepends=True)
        # Ensure new content ends with newline if original does
        if original_lines and original_lines[-1].endswith("\n") and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"

        modified = (
            original_lines[: start_line - 1] + new_lines + original_lines[end_line:]
        )
        new_text = "".join(modified)

        # Lint check before writing
        if self._lint_enabled:
            result = self._validate_syntax(path, new_text)
            if not result.valid:
                return {
                    "success": False,
                    "error": "Syntax validation failed",
                    "errors": result.errors,
                    "rollback": True,
                }

        # Write to temp file first, then atomic rename
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(path) or ".", prefix=".ww_edit_", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp_path, path)
        except (IOError, OSError) as e:
            return {
                "success": False,
                "error": f"Write failed: {e}",
                "rollback": True,
            }

        # Generate simple diff
        old_text = "".join(original_lines)
        diff = self._simple_diff(old_text, new_text, path)

        return {
            "success": True,
            "diff": diff,
            "lines_changed": len(original_lines) - len(modified) if len(modified) != len(original_lines) else 0,
        }

    def write_file(self, path: str, content: str) -> Dict:
        """Safe write with syntax validation."""
        path = os.path.abspath(os.path.expanduser(path))

        if self._lint_enabled:
            result = self._validate_syntax(path, content)
            if not result.valid:
                return {
                    "success": False,
                    "error": "Syntax validation failed",
                    "errors": result.errors,
                    "rollback": True,
                }

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(path) or ".", prefix=".ww_write_", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except (IOError, OSError) as e:
            return {"success": False, "error": f"Write failed: {e}"}

        return {"success": True, "path": path, "bytes": len(content)}

    def _validate_syntax(self, path: str, content: str) -> ValidationResult:
        """Run language-appropriate syntax check on content."""
        errors = []
        ext = os.path.splitext(path)[1].lower()

        if ext in (".py",):
            return self._check_python_syntax(content)
        elif ext in (".js", ".jsx", ".mjs"):
            return self._check_node_syntax(content)
        elif ext in (".ts", ".tsx"):
            return self._check_ts_syntax(content)
        elif ext in (".json",):
            return self._check_json_syntax(content)
        elif ext in (".yaml", ".yml"):
            return self._check_yaml_syntax(content)
        elif ext in (".md", ".txt", ".rst", ""):
            return ValidationResult(True)  # No syntax check for text/markup
        else:
            return ValidationResult(True)  # Unknown extension, skip

    def _check_python_syntax(self, content: str) -> ValidationResult:
        """Validate Python syntax using ast.parse."""
        try:
            ast.parse(content)
            return ValidationResult(True)
        except SyntaxError as e:
            return ValidationResult(False, [f"Python SyntaxError: {e}"])

    def _check_node_syntax(self, content: str) -> ValidationResult:
        """Check JS/JSX syntax using node --check."""
        import tempfile
        import os
        try:
            fd, tmp = tempfile.mkstemp(suffix=".js")
            with os.fdopen(fd, "w") as f:
                f.write(content)
            r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=10)
            os.unlink(tmp)
            if r.returncode == 0:
                return ValidationResult(True)
            return ValidationResult(False, [r.stderr.strip()])
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ValidationResult(True)

    def _check_ts_syntax(self, content: str) -> ValidationResult:
        """TypeScript syntax check via tsc (if available)."""
        import tempfile
        import os
        try:
            fd, tmp = tempfile.mkstemp(suffix=".ts")
            with os.fdopen(fd, "w") as f:
                f.write(content)
            r = subprocess.run(["tsc", "--noEmit", "--lib", "es6", tmp], capture_output=True, text=True, timeout=15)
            os.unlink(tmp)
            if r.returncode == 0:
                return ValidationResult(True)
            return ValidationResult(False, [r.stderr.strip()])
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ValidationResult(True)

    def _check_json_syntax(self, content: str) -> ValidationResult:
        """Validate JSON."""
        import json as json_mod

        try:
            json_mod.loads(content)
            return ValidationResult(True)
        except json_mod.JSONDecodeError as e:
            return ValidationResult(False, [f"JSON Error: {e}"])

    def _check_yaml_syntax(self, content: str) -> ValidationResult:
        """Validate YAML using Python's yaml library (if available)."""
        try:
            import yaml
            yaml.safe_load(content)
            return ValidationResult(True)
        except ImportError:
            return ValidationResult(True)
        except yaml.YAMLError as e:
            return ValidationResult(False, [f"YAML Error: {e}"])

    def _simple_diff(self, old: str, new: str, path: str) -> str:
        """Generate a minimal unified diff."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

        # Simple line-level diff
        diff_parts = [f"--- {path}", f"+++ {path}"]
        i, j = 0, 0
        while i < len(old_lines) and j < len(new_lines):
            if old_lines[i] != new_lines[j]:
                start = i + 1
                ctx_before = max(0, i - 3)
                ctx_after = min(len(old_lines), i + 4)

                old_slice = old_lines[i : i + 5]
                new_slice = new_lines[j : j + 5]
                chunk_len = max(len(old_slice), len(new_slice))

                # Simple chunk header
                diff_parts.append(f"@@ -{i+1},{chunk_len} +{j+1},{chunk_len} @@")
                for k in range(chunk_len):
                    if k < len(old_slice):
                        diff_parts.append(f"-{old_slice[k].rstrip()}")
                    if k < len(new_slice):
                        diff_parts.append(f"+{new_slice[k].rstrip()}")
                i += chunk_len
                j += chunk_len
            else:
                i += 1
                j += 1

        return "\n".join(diff_parts) if len(diff_parts) > 2 else "(no changes)"


# ── Tool definitions for WW registry ──────────────────────────────────

def create_viewer_tools(viewer: WindowedFileViewer) -> List[Dict]:
    """Create tool definitions for the windowed file viewer."""
    return [
        {
            "name": "coding_open",
            "description": "Open a file for viewing. Returns first 100 lines with context metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to open"}
                },
                "required": ["path"],
            },
            "handler": viewer.open,
            "category": "code_aci",
        },
        {
            "name": "coding_scroll_down",
            "description": "Scroll forward in the current open file by ~97 lines (with 3-line overlap).",
            "parameters": {
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Lines to scroll (default: window - overlap)",
                    }
                },
            },
            "handler": viewer.scroll_down,
            "category": "code_aci",
        },
        {
            "name": "coding_scroll_up",
            "description": "Scroll backward in the current open file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lines": {"type": "integer"}
                },
            },
            "handler": viewer.scroll_up,
            "category": "code_aci",
        },
        {
            "name": "coding_goto",
            "description": "Jump to a specific line number. Target line appears at ~1/6 of the window (not top).",
            "parameters": {
                "type": "object",
                "properties": {
                    "line": {
                        "type": "integer",
                        "description": "Target line number (1-indexed)",
                    }
                },
                "required": ["line"],
            },
            "handler": viewer.goto,
            "category": "code_aci",
        },
        {
            "name": "coding_close",
            "description": "Close the currently open file.",
            "parameters": {"type": "object", "properties": {}},
            "handler": viewer.close,
            "category": "code_aci",
        },
    ]


def create_editor_tools(editor: DefensiveEditor) -> List[Dict]:
    """Create tool definitions for the defensive code editor."""
    return [
        {
            "name": "coding_edit_lines",
            "description": "Replace a range of lines in a file. Runs syntax check before writing. Auto-rollback on failure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "start_line": {
                        "type": "integer",
                        "description": "First line to replace (1-indexed, inclusive)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to replace (1-indexed, inclusive)",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "Replacement text (can be multi-line)",
                    },
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
            "handler": editor.edit_lines,
            "category": "code_aci",
        },
        {
            "name": "coding_write_file",
            "description": "Write content to a file with syntax validation. Uses atomic temp-file + rename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["path", "content"],
            },
            "handler": editor.write_file,
            "category": "code_aci",
        },
    ]


# ── module-level singletons (lazy init) ───────────────────────────────

_viewer: WindowedFileViewer = None
_editor: DefensiveEditor = None


def get_viewer() -> WindowedFileViewer:
    global _viewer
    if _viewer is None:
        _viewer = WindowedFileViewer()
    return _viewer


def get_editor() -> DefensiveEditor:
    global _editor
    if _editor is None:
        _editor = DefensiveEditor()
    return _editor


def get_aci_tools() -> List[Dict]:
    """Get all ACI tool definitions for registration."""
    return create_viewer_tools(get_viewer()) + create_editor_tools(get_editor())
