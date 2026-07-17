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
import re
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

        try:
            from coding.policy import check_content_secrets
            sec = check_content_secrets(new_content)
            if not sec.get("allowed", True):
                return {
                    "success": False,
                    "error": sec.get("reason", "Secret detected"),
                    "rollback": True,
                    "secret_blocked": True,
                }
        except Exception:
            pass

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

        try:
            from coding.policy import check_content_secrets
            sec = check_content_secrets(content)
            if not sec.get("allowed", True):
                return {
                    "success": False,
                    "error": sec.get("reason", "Secret detected"),
                    "rollback": True,
                    "secret_blocked": True,
                }
        except Exception:
            pass

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

    def edit_symbol(self, path: str, symbol_name: str, new_body: str) -> Dict:
        """Replace a function/class body by name via AST; syntax check + rollback.

        *new_body* may be:
        - full def/class statement including signature, or
        - only the indented body lines (will replace interior of the symbol).
        """
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            return {"success": False, "error": f"File not found: {path}", "rollback": False}

        # Circuit before edit
        circuit_info = None
        try:
            from coding.circuit import get_breaker
            circuit_info = get_breaker().before_edit(path)
            if circuit_info.get("tripped"):
                return {
                    "success": False,
                    "error": f"Circuit breaker tripped for {path}",
                    "circuit": circuit_info,
                    "rollback": False,
                }
        except Exception:
            pass

        # Secret scan on new body
        try:
            from coding.policy import check_content_secrets, record_coding_write, append_edit_log, find_project_root
            sec = check_content_secrets(new_body)
            if not sec.get("allowed", True):
                return {"success": False, "error": sec["reason"], "rollback": True, "secret_blocked": True}
        except Exception:
            check_content_secrets = None  # type: ignore
            record_coding_write = None  # type: ignore
            append_edit_log = None  # type: ignore
            find_project_root = None  # type: ignore

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
        original_lines = original.splitlines(keepends=True)

        try:
            tree = ast.parse(original)
        except SyntaxError as e:
            return {"success": False, "error": f"Cannot parse original file: {e}", "rollback": False}

        target = None
        bare = symbol_name.split(".")[-1]
        # Prefer qualified match Class.method
        if "." in symbol_name:
            cls_name, meth = symbol_name.split(".", 1)
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == cls_name:
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == meth:
                            target = child
                            break
        if target is None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == bare or node.name == symbol_name:
                        target = node
                        # Prefer top-level / exact
                        if node.name == symbol_name:
                            break

        if target is None:
            return {
                "success": False,
                "error": f"Symbol not found: {symbol_name}",
                "rollback": False,
            }

        start = target.lineno  # 1-indexed
        end = target.end_lineno or target.lineno
        # Detect if new_body is a full replacement (starts with def/class/async)
        stripped = new_body.lstrip()
        is_full = stripped.startswith(("def ", "class ", "async def ", "@"))

        if is_full:
            # Replace entire symbol including signature
            # Preserve original indentation of the def line
            orig_indent = ""
            if start - 1 < len(original_lines):
                line = original_lines[start - 1]
                orig_indent = line[: len(line) - len(line.lstrip())]
            body_lines = new_body.splitlines(keepends=True)
            if body_lines and not body_lines[0].startswith(orig_indent) and orig_indent:
                # Re-indent relative to first line of new_body
                first_indent = body_lines[0][: len(body_lines[0]) - len(body_lines[0].lstrip())]
                reindented = []
                for bl in body_lines:
                    if bl.strip() == "":
                        reindented.append("\n" if bl.endswith("\n") else "")
                        continue
                    if first_indent and bl.startswith(first_indent):
                        bl = orig_indent + bl[len(first_indent):]
                    else:
                        bl = orig_indent + bl.lstrip() if not bl.startswith(orig_indent) else bl
                    if not bl.endswith("\n"):
                        bl += "\n"
                    reindented.append(bl)
                body_lines = reindented
            else:
                body_lines = [
                    (bl if bl.endswith("\n") else bl + "\n") for bl in new_body.splitlines()
                ] or ["pass\n"]
            new_lines = original_lines[: start - 1] + body_lines + original_lines[end:]
        else:
            # Replace only interior body (keep signature line(s) through first line after colon)
            # Find body start: first line after the def/class line that is more indented
            sig_line = original_lines[start - 1]
            base_indent = len(sig_line) - len(sig_line.lstrip())
            body_start = start  # 0-index later
            # Account for decorators: target.lineno is def line, but decorators are before
            # For end we use end_lineno; for body interior skip decorator+sig
            # Find first body line
            i = start  # 1-indexed next line after def
            while i <= end:
                if i - 1 >= len(original_lines):
                    break
                ln = original_lines[i - 1]
                if ln.strip() == "":
                    i += 1
                    continue
                ind = len(ln) - len(ln.lstrip())
                if ind > base_indent:
                    body_start = i
                    break
                # multi-line signature
                i += 1
            else:
                body_start = start + 1

            # Indent new body to base_indent + 4
            child_indent = " " * (base_indent + 4)
            body_parts = []
            for bl in new_body.splitlines():
                if bl.strip() == "":
                    body_parts.append("\n")
                else:
                    body_parts.append(child_indent + bl.lstrip() + "\n")
            if not body_parts:
                body_parts = [child_indent + "pass\n"]
            new_lines = (
                original_lines[: body_start - 1]
                + body_parts
                + original_lines[end:]
            )

        new_text = "".join(new_lines)

        # Syntax validation
        if self._lint_enabled:
            result = self._validate_syntax(path, new_text)
            if not result.valid:
                try:
                    from coding.circuit import get_breaker
                    get_breaker().after_edit(
                        path, False, error_text="\n".join(result.errors), diff=""
                    )
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": "Syntax validation failed",
                    "errors": result.errors,
                    "rollback": True,
                    "symbol": symbol_name,
                }

        # Ensure new AST still has the symbol when full replace
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError as e:
            return {
                "success": False,
                "error": f"Syntax validation failed: {e}",
                "errors": [str(e)],
                "rollback": True,
            }

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(path) or ".", prefix=".ww_edit_", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp_path, path)
        except (IOError, OSError) as e:
            return {"success": False, "error": f"Write failed: {e}", "rollback": True}

        diff = self._simple_diff(original, new_text, path)
        try:
            from coding.circuit import get_breaker
            get_breaker().after_edit(path, True, diff=diff)
        except Exception:
            pass
        try:
            from coding.policy import record_coding_write, append_edit_log, find_project_root
            record_coding_write(path, "edit_symbol")
            append_edit_log(find_project_root(path), {
                "tool": "coding_edit_symbol",
                "path": path,
                "symbol": symbol_name,
                "success": True,
            })
        except Exception:
            pass

        return {
            "success": True,
            "path": path,
            "symbol": symbol_name,
            "start_line": start,
            "end_line": end,
            "diff": diff,
            "ast_ok": True,
        }

    def apply_patch(self, patch_text: str, reverse: bool = False) -> Dict:
        """Apply a unified diff with syntax check + rollback on failure."""
        if not patch_text or not patch_text.strip():
            return {"success": False, "error": "Empty patch", "rollback": False}

        try:
            from coding.policy import check_content_secrets
            sec = check_content_secrets(patch_text)
            if not sec.get("allowed", True):
                return {
                    "success": False,
                    "error": sec["reason"],
                    "rollback": True,
                    "secret_blocked": True,
                }
        except Exception:
            pass

        # Parse unified diff into file hunks
        files = _parse_unified_diff(patch_text)
        if not files:
            return {"success": False, "error": "Could not parse unified diff", "rollback": False}

        backups: Dict[str, str] = {}
        applied: List[str] = []
        try:
            for fpath, hunks in files.items():
                abs_path = os.path.abspath(os.path.expanduser(fpath))
                # Circuit
                try:
                    from coding.circuit import get_breaker
                    info = get_breaker().before_edit(abs_path)
                    if info.get("tripped"):
                        raise RuntimeError(f"Circuit breaker tripped for {abs_path}")
                except RuntimeError:
                    raise
                except Exception:
                    pass

                if os.path.isfile(abs_path):
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        original = f.read()
                else:
                    original = ""
                backups[abs_path] = original
                new_content = _apply_hunks(original, hunks, reverse=reverse)

                if abs_path.endswith(".py") or abs_path.endswith(".pyi"):
                    if self._lint_enabled and new_content.strip():
                        vr = self._validate_syntax(abs_path, new_content)
                        if not vr.valid:
                            raise SyntaxError("; ".join(vr.errors))

                os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(abs_path) or ".", prefix=".ww_patch_", suffix=".tmp"
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(new_content)
                os.replace(tmp_path, abs_path)
                applied.append(abs_path)
                try:
                    from coding.policy import record_coding_write, append_edit_log, find_project_root
                    record_coding_write(abs_path, "apply_patch")
                    append_edit_log(find_project_root(abs_path), {
                        "tool": "coding_apply_patch",
                        "path": abs_path,
                        "success": True,
                    })
                except Exception:
                    pass
                try:
                    from coding.circuit import get_breaker
                    get_breaker().after_edit(abs_path, True)
                except Exception:
                    pass

            return {
                "success": True,
                "files": applied,
                "count": len(applied),
            }
        except Exception as e:
            # Rollback all
            for abs_path, content in backups.items():
                try:
                    if content == "" and not os.path.isfile(abs_path):
                        continue
                    if content == "" and os.path.isfile(abs_path):
                        # was newly created — remove
                        try:
                            os.unlink(abs_path)
                        except OSError:
                            pass
                    else:
                        with open(abs_path, "w", encoding="utf-8") as f:
                            f.write(content)
                except OSError:
                    pass
            for abs_path in applied:
                try:
                    from coding.circuit import get_breaker
                    get_breaker().after_edit(abs_path, False, error_text=str(e))
                except Exception:
                    pass
            return {
                "success": False,
                "error": str(e),
                "rollback": True,
                "files_attempted": list(backups.keys()),
            }


def _parse_unified_diff(patch_text: str) -> Dict[str, List[Dict]]:
    """Parse unified diff → {path: [hunks]}."""
    files: Dict[str, List[Dict]] = {}
    current_path = None
    current_hunk = None
    for line in patch_text.splitlines(keepends=True):
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            raw = line[4:].strip()
            # strip a/ b/ prefixes
            if raw.startswith("b/") or raw.startswith("a/"):
                raw = raw[2:]
            # strip timestamp tabs
            raw = raw.split("\t")[0].strip()
            current_path = raw
            files.setdefault(current_path, [])
            current_hunk = None
            continue
        if line.startswith("@@"):
            # @@ -l,s +l,s @@
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m or current_path is None:
                continue
            current_hunk = {
                "old_start": int(m.group(1)),
                "old_count": int(m.group(2) or "1"),
                "new_start": int(m.group(3)),
                "new_count": int(m.group(4) or "1"),
                "lines": [],
            }
            files[current_path].append(current_hunk)
            continue
        if current_hunk is not None and (line.startswith(" ") or line.startswith("+") or line.startswith("-") or line.startswith("\\")):
            current_hunk["lines"].append(line if line.endswith("\n") else line + "\n")
    return files


def _apply_hunks(original: str, hunks: List[Dict], reverse: bool = False) -> str:
    """Apply hunks to original content. Simple sequential apply from bottom."""
    if not hunks:
        return original
    lines = original.splitlines(keepends=True)
    # Apply from bottom so line numbers stay valid
    for hunk in sorted(hunks, key=lambda h: h["old_start"], reverse=True):
        old_start = hunk["old_start"]
        # unified diff is 1-indexed; empty file uses 0
        idx = max(0, old_start - 1) if old_start > 0 else 0
        old_lines = []
        new_lines = []
        for hl in hunk["lines"]:
            if hl.startswith("\\"):
                continue
            tag = hl[0] if hl else " "
            content = hl[1:] if len(hl) > 0 else "\n"
            if not content.endswith("\n") and content != "":
                content += "\n"
            if reverse:
                if tag == "+":
                    old_lines.append(content)
                elif tag == "-":
                    new_lines.append(content)
                else:
                    old_lines.append(content)
                    new_lines.append(content)
            else:
                if tag == "-":
                    old_lines.append(content)
                elif tag == "+":
                    new_lines.append(content)
                else:
                    old_lines.append(content)
                    new_lines.append(content)
        # Verify context matches loosely
        end = idx + len(old_lines)
        if old_lines and lines[idx:end] != old_lines:
            # Try to find nearby
            found = False
            for delta in range(0, 20):
                for start in (idx + delta, idx - delta):
                    if start < 0:
                        continue
                    if lines[start:start + len(old_lines)] == old_lines:
                        idx = start
                        end = idx + len(old_lines)
                        found = True
                        break
                if found:
                    break
            if not found and old_lines:
                raise ValueError(
                    f"Hunk context mismatch at line {old_start}: "
                    f"expected {old_lines[:2]!r} got {lines[idx:idx+2]!r}"
                )
        lines = lines[:idx] + new_lines + lines[end:]
    return "".join(lines)


# ── Tool definitions for WW registry ──────────────────────────────────

def _wrap_edit_lines(editor: DefensiveEditor):
    def handler(path, start_line, end_line, new_content):
        try:
            from coding.policy import check_content_secrets, record_coding_write, append_edit_log, find_project_root
            sec = check_content_secrets(new_content)
            if not sec.get("allowed", True):
                return {"success": False, "error": sec["reason"], "rollback": True, "secret_blocked": True}
        except Exception:
            record_coding_write = None  # type: ignore
            append_edit_log = None  # type: ignore
            find_project_root = None  # type: ignore
        try:
            from coding.circuit import get_breaker
            info = get_breaker().before_edit(path)
            if info.get("tripped"):
                return {"success": False, "error": f"Circuit breaker tripped for {path}", "circuit": info}
        except Exception:
            pass
        result = editor.edit_lines(path, start_line, end_line, new_content)
        try:
            from coding.circuit import get_breaker
            get_breaker().after_edit(
                path,
                bool(result.get("success")),
                error_text=result.get("error", "") or "\n".join(result.get("errors") or []),
                diff=result.get("diff", ""),
            )
        except Exception:
            pass
        if result.get("success"):
            try:
                from coding.policy import record_coding_write, append_edit_log, find_project_root
                record_coding_write(path, "edit_lines")
                append_edit_log(find_project_root(path), {
                    "tool": "coding_edit_lines", "path": path, "success": True,
                })
            except Exception:
                pass
        return result
    return handler


def _wrap_write_file(editor: DefensiveEditor):
    def handler(path, content):
        try:
            from coding.policy import check_content_secrets, record_coding_write, append_edit_log, find_project_root
            sec = check_content_secrets(content)
            if not sec.get("allowed", True):
                return {"success": False, "error": sec["reason"], "rollback": True, "secret_blocked": True}
        except Exception:
            pass
        result = editor.write_file(path, content)
        if result.get("success"):
            try:
                from coding.policy import record_coding_write, append_edit_log, find_project_root
                record_coding_write(path, "write_file")
                append_edit_log(find_project_root(path), {
                    "tool": "coding_write_file", "path": path, "success": True,
                })
            except Exception:
                pass
        return result
    return handler


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
            "permission": "safe",
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
            "permission": "safe",
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
            "permission": "safe",
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
            "permission": "safe",
        },
        {
            "name": "coding_close",
            "description": "Close the currently open file.",
            "parameters": {"type": "object", "properties": {}},
            "handler": viewer.close,
            "category": "code_aci",
            "permission": "safe",
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
            "handler": _wrap_edit_lines(editor),
            "category": "code_aci",
            "permission": "requires_approval",
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
            "handler": _wrap_write_file(editor),
            "category": "code_aci",
            "permission": "requires_approval",
        },
        {
            "name": "coding_edit_symbol",
            "description": "Replace a function or class body by name via AST. Syntax check + auto-rollback on failure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python file path"},
                    "symbol_name": {
                        "type": "string",
                        "description": "Function/class name (or Class.method)",
                    },
                    "new_body": {
                        "type": "string",
                        "description": "New full def/class source or indented body only",
                    },
                },
                "required": ["path", "symbol_name", "new_body"],
            },
            "handler": lambda path, symbol_name, new_body: editor.edit_symbol(path, symbol_name, new_body),
            "category": "code_aci",
            "permission": "requires_approval",
        },
        {
            "name": "coding_apply_patch",
            "description": "Apply a unified diff patch with syntax check and full rollback on failure. Secret scan blocks keys.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_text": {"type": "string", "description": "Unified diff text"},
                    "reverse": {"type": "boolean", "default": False},
                },
                "required": ["patch_text"],
            },
            "handler": lambda patch_text, reverse=False: editor.apply_patch(patch_text, reverse=reverse),
            "category": "code_aci",
            "permission": "requires_approval",
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
