"""ww/tools/file_tools.py — File operation tool

Dependencies: None (pure stdlib)
Purpose: read/write files, search file content, list directories
"""

from __future__ import annotations
import glob
import os
import re
from typing import Optional

from tools.registry import ToolRegistry, ToolDef
from core.diff import get_diff_engine


def register_tools(registry: ToolRegistry):
    """Register file tools with the given registry."""

    # ── read_file ──────────────────────────────────────

    def handle_read_file(path: str, offset: int = 1, limit: int = 500, **kwargs) -> dict:
        """Read a text file with line numbers and pagination."""
        try:
            path = os.path.expanduser(path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            total = len(lines)
            start = max(0, offset - 1)
            end = min(total, start + limit)
            content = "".join(lines[start:end])
            return {
                "result": content,
                "total_lines": total,
                "lines_returned": end - start,
                "offset": offset,
            }
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}
        except IsADirectoryError:
            return {"error": f"Path is a directory: {path}"}
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="read_file",
        description="Read a text file with line numbers. Use offset and limit for pagination.",
        handler=handle_read_file,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (supports ~ expansion)"},
                "offset": {"type": "integer", "description": "Starting line number (1-indexed)", "default": 1},
                "limit": {"type": "integer", "description": "Max lines to return", "default": 500},
            },
            "required": ["path"],
        },
        category="file",
    ))

    # ── write_file ─────────────────────────────────────

    def handle_write_file(path: str, content: str, **kwargs) -> dict:
        """Write content to a file, overwriting existing content."""
        try:
            path = os.path.expanduser(path)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"result": f"Written {len(content)} bytes to {path}"}
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="write_file",
        description="Write content to a file. Creates parent directories automatically. OVERWRITES existing content.",
        handler=handle_write_file,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        category="file",
    ))

    # ── search_files ───────────────────────────────────

    def handle_search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None, limit: int = 50, **kwargs) -> dict:
        """Search file contents or find files by name."""
        try:
            path = os.path.expanduser(path)
            matches = []

            if file_glob:
                # Search inside specific file types
                files = []
                for f in glob.glob(os.path.join(path, "**", file_glob), recursive=True):
                    if os.path.isfile(f):
                        files.append(f)
            else:
                # Walk and search all files
                files = []
                for root, dirs, fnames in os.walk(path):
                    # Skip hidden directories
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for fname in fnames:
                        if fname.startswith("."):
                            continue
                        files.append(os.path.join(root, fname))

            count = 0
            for fpath in files:
                if count >= limit:
                    break
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if re.search(pattern, line, re.IGNORECASE):
                                matches.append({
                                    "file": fpath,
                                    "line": lineno,
                                    "content": line.rstrip()[:200],
                                })
                                count += 1
                                if count >= limit:
                                    break
                except (UnicodeDecodeError, PermissionError, OSError):
                    continue

            return {
                "result": matches,
                "total_matches": len(matches),
                "files_searched": len(files),
            }
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="search_files",
        description="Search file contents or find files by glob pattern. Supports regex content search.",
        handler=handle_search_files,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory to search", "default": "."},
                "file_glob": {"type": "string", "description": "Filter by file pattern e.g. '*.py'"},
                "limit": {"type": "integer", "description": "Max matches to return", "default": 50},
            },
            "required": ["pattern"],
        },
        category="file",
    ))

    # ── list_directory ─────────────────────────────────

    def handle_list_directory(path: str = ".", **kwargs) -> dict:
        """List files and directories at a path."""
        try:
            path = os.path.expanduser(path)
            entries = []
            for entry in sorted(os.listdir(path)):
                full = os.path.join(path, entry)
                stat = os.stat(full)
                is_dir = os.path.isdir(full)
                entries.append({
                    "name": entry,
                    "type": "dir" if is_dir else "file",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
            return {"result": entries, "path": path, "total": len(entries)}
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="list_directory",
        description="List files and directories at a path, sorted by name.",
        handler=handle_list_directory,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path", "default": "."},
            },
            "required": [],
        },
        category="file",
    ))

    # ── diff tools ─────────────────────────────────────

    _diff_engine = get_diff_engine()

    def handle_diff(path: str, **kwargs) -> dict:
        """Show ANSI-colored diff for a file (requires prior snapshot)."""
        try:
            path = os.path.expanduser(path)
            result = _diff_engine.diff(path)
            if result is None:
                return {"error": f"No snapshot for {path}. Call snapshot_file first."}
            return {
                "path": path,
                "ansi_diff": result.ansi,
                "plain_diff": result.plain,
                "stats": result.stats,
                "summary": result.summary,
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_snapshot(path: str, **kwargs) -> dict:
        """Snapshot a file's current state for later diffing."""
        try:
            path = os.path.expanduser(path)
            _diff_engine.snapshot(path)
            return {"path": path, "snapshotted": True}
        except Exception as e:
            return {"error": str(e)}

    def handle_diff_preview(path: str, new_content: str, **kwargs) -> dict:
        """Preview what a diff would look like without writing the file."""
        try:
            path = os.path.expanduser(path)
            if not _diff_engine.has_snapshot(path):
                _diff_engine.snapshot(path)
            result = _diff_engine.diff(path, new_content=new_content)
            return {
                "path": path,
                "ansi_diff": result.ansi,
                "side_by_side": result.side_by_side,
                "stats": result.stats,
                "summary": result.summary,
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_show_diff(path: str, mode: str = "unified", **kwargs) -> dict:
        """Show a rich visual diff. Modes: unified (ANSI), side-by-side, json."""
        try:
            path = os.path.expanduser(path)
            result = _diff_engine.diff(path)
            if result is None:
                return {"error": f"No snapshot for {path}."}
            output = {"path": path, "stats": result.stats, "summary": result.summary}
            if mode == "side-by-side":
                output["side_by_side"] = result.side_by_side
            elif mode == "json":
                output["json"] = result.json()
            else:
                output["ansi_diff"] = result.ansi
            return output
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="diff",
        description="Show ANSI-colored unified diff for a snapshotted file.",
        handler=handle_diff,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to diff."},
            },
            "required": ["path"],
        },
        category="file",
    ))

    registry.register(ToolDef(
        name="snapshot_file",
        description="Save a file's current state so changes can be diffed later.",
        handler=handle_snapshot,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to snapshot."},
            },
            "required": ["path"],
        },
        category="file",
    ))

    registry.register(ToolDef(
        name="diff_preview",
        description="Preview a visual diff (ANSI + side-by-side) before writing changes.",
        handler=handle_diff_preview,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "new_content": {"type": "string", "description": "Proposed new content."},
            },
            "required": ["path", "new_content"],
        },
        category="file",
    ))

    registry.register(ToolDef(
        name="show_diff",
        description="Rich visual diff: unified ANSI, side-by-side, or JSON. Modes: unified, side-by-side, json.",
        handler=handle_show_diff,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "mode": {"type": "string", "description": "unified, side-by-side, or json", "default": "unified"},
            },
            "required": ["path"],
        },
        category="file",
    ))
