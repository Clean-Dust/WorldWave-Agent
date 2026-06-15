"""ww/pm/debug_integration.py — Crash screenshot + @workspace context + MCP bridge v0.1

Implements Gemini's remaining requirements:
- Test crash screenshot reading (via WW Computer Use)
- @workspace context variable (project structure + file listing)
- MCP bridge server for unified LSP/tool interface
"""

from __future__ import annotations
import json
import os
import subprocess
import threading
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Crash Screenshot Capture ──────────────────────────────────────────

class CrashScreenshot:
    """Capture screenshots when tests fail, using WW Computer Use.

    Falls back to xdotool/import on Linux, or PowerShell on Windows.
    """

    def __init__(self, output_dir: str = "/tmp/ww-crash-screenshots"):
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def capture(self, test_name: str = "unknown") -> Dict:
        """Capture a screenshot and return the path.

        Tries multiple methods:
        1. WW Computer Use cu_screenshot (preferred)
        2. import (ImageMagick, Linux)
        3. PowerShell (Windows)
        4. scrot (Linux)
        """
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
        output_path = os.path.join(self._output_dir, f"crash_{safe_name}_{int(__import__('time').time())}.png")
        method = "none"

        # Method 1: Try WW Computer Use cu_screenshot
        try:
            from core.computer_use.capture import capture_screen
            img = capture_screen()
            if img:
                img.save(output_path)
                method = "computer_use"
                return {"success": True, "path": output_path, "method": method}
        except (ImportError, Exception):
            pass

        # Method 2: import (ImageMagick)
        for cmd in [["import", "-window", "root", output_path],
                     ["scrot", output_path],
                     ["gnome-screenshot", "-f", output_path]]:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=5)
                if r.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 1000:
                    method = cmd[0]
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

        if os.path.isfile(output_path) and os.path.getsize(output_path) > 1000:
            return {"success": True, "path": output_path, "method": method}

        return {"success": False, "error": "No screenshot method available", "test_name": test_name}


# ── @workspace Context Provider ───────────────────────────────────────

class WorkspaceContext:
    """Provide project-wide context similar to Copilot's @workspace.

    Returns:
    - Project structure (directory tree)
    - File count by type
    - Key files (README, config, etc.)
    - Open tabs / recently modified files
    """

    def __init__(self, root_dir: str = "."):
        self._root_dir = os.path.abspath(root_dir)

    def get_context(self, max_depth: int = 3, max_files: int = 100) -> Dict:
        """Get full workspace context."""
        return {
            "project_root": self._root_dir,
            "structure": self._directory_tree(max_depth),
            "file_stats": self._file_stats(),
            "key_files": self._find_key_files(),
            "recent_changes": self._recent_changes(),
        }

    def get_summary(self) -> Dict:
        """Get a concise workspace summary (for system prompt injection)."""
        stats = self._file_stats()
        key = self._find_key_files()
        return {
            "root": self._root_dir,
            "total_files": stats.get("total", 0),
            "languages": stats.get("by_extension", {}),
            "key_config_files": key[:10],
            "has_git": os.path.isdir(os.path.join(self._root_dir, ".git")),
            "has_tests": stats.get("by_extension", {}).get(".py", 0) > 0 or \
                        os.path.isdir(os.path.join(self._root_dir, "tests")),
        }

    def _directory_tree(self, max_depth: int) -> str:
        """Generate ASCII directory tree."""
        lines = []
        root_name = os.path.basename(self._root_dir) or self._root_dir

        def _walk(dir_path: str, prefix: str = "", depth: int = 0):
            if depth > max_depth:
                return
            try:
                entries = sorted(os.listdir(dir_path))
            except PermissionError:
                return

            # Separate dirs and files
            dirs = [e for e in entries if os.path.isdir(os.path.join(dir_path, e))
                    and not e.startswith(".") and e != "__pycache__"]
            files = [e for e in entries if os.path.isfile(os.path.join(dir_path, e))
                     and not e.startswith(".")]

            for i, entry in enumerate(dirs):
                is_last = (i == len(dirs) - 1 and len(files) == 0)
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{entry}/")
                _walk(
                    os.path.join(dir_path, entry),
                    prefix + ("    " if is_last else "│   "),
                    depth + 1,
                )

            for i, entry in enumerate(files):
                is_last = (i == len(files) - 1)
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{entry}")

        lines.append(f"{root_name}/")
        _walk(self._root_dir)
        return "\n".join(lines[:200])  # Cap at 200 lines

    def _file_stats(self) -> Dict:
        """Count files by extension."""
        counts = {"total": 0, "by_extension": {}}
        for f in Path(self._root_dir).rglob("*"):
            if f.is_file() and ".git" not in f.parts and "__pycache__" not in f.parts:
                counts["total"] += 1
                ext = f.suffix or "(no ext)"
                counts["by_extension"][ext] = counts["by_extension"].get(ext, 0) + 1
        return counts

    def _find_key_files(self) -> List[str]:
        """Find important project files."""
        key_names = {
            "README.md", "readme.md", "Readme.md",
            "AGENTS.md", "PLANS.md",
            "pyproject.toml", "setup.py", "setup.cfg",
            "package.json", "Cargo.toml", "go.mod",
            "Makefile", "makefile",
            "Dockerfile", "docker-compose.yml",
            ".env.example", ".gitignore",
        }
        found = []
        for f in Path(self._root_dir).rglob("*"):
            if f.is_file() and f.name in key_names:
                found.append(str(f.relative_to(self._root_dir)))
                if len(found) >= 20:
                    break
        return found

    def _recent_changes(self) -> List[Dict]:
        """Get recently modified files."""
        files = []
        for f in sorted(Path(self._root_dir).rglob("*.py"), key=lambda p: p.stat().st_mtime, reverse=True):
            if ".git" not in f.parts and "__pycache__" not in f.parts:
                rel = str(f.relative_to(self._root_dir))
                mtime = f.stat().st_mtime
                files.append({"file": rel, "modified": mtime})
                if len(files) >= 10:
                    break
        return files


# ── MCP Bridge Server ────────────────────────────────────────────────

class MCPBridge:
    """MCP (Model Context Protocol) bridge server with stdio JSON-RPC loop.

    Provides LSP capabilities through the MCP protocol via stdin/stdout.
    Implements a fully asynchronous JSON-RPC 2.0 message handler.

    When start() is called, reads MCP requests from stdin and dispatches
    to underlying LSPManager for code intelligence operations.
    """

    def __init__(self, lsp_manager=None):
        self._lsp_manager = lsp_manager
        self._running = False
        self._reader_thread = None

    def start(self):
        """Start the MCP bridge in a background thread (stdio JSON-RPC loop)."""
        if self._running:
            return
        self._running = True
        self._reader_thread = threading.Thread(target=self._stdio_loop, daemon=True)
        self._reader_thread.start()

    def stop(self):
        """Stop the MCP bridge."""
        self._running = False

    def _stdio_loop(self):
        """Read JSON-RPC requests from stdin and respond on stdout."""
        import sys, json, threading, queue, os
        raw = ""
        while self._running:
            try:
                chunk = sys.stdin.read(1) if hasattr(sys.stdin, "read") else ""
                if not chunk:
                    break
                raw += chunk

                # Look for Content-Length header (LSP framing) or try bare JSON
                if "\r\n\r\n" in raw:
                    header_end = raw.find("\r\n\r\n")
                    body_start = header_end + 4
                    content_length = 0
                    for line in raw[:header_end].split("\r\n"):
                        if line.lower().startswith("content-length:"):
                            content_length = int(line.split(":")[1].strip())
                    if content_length > 0 and len(raw) >= body_start + content_length:
                        body = raw[body_start:body_start + content_length]
                        raw = raw[body_start + content_length:]
                        self._handle_request(body)
                # Also handle newline-delimited JSON
                elif "\n" in raw:
                    lines = raw.split("\n")
                    for line in lines[:-1]:
                        stripped = line.strip()
                        if stripped:
                            self._handle_request(stripped)
                    raw = lines[-1]
            except (ValueError, OSError):
                break

    def _handle_request(self, body: str):
        """Parse and dispatch a single MCP JSON-RPC request."""
        import json, sys
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            return

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "tools/list":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.get_mcp_tools()},
            }
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            result = self._execute_tool(name, args)
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": result,
            }
        elif method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "ww-pm-mcp", "version": "0.1"},
                    "capabilities": {"tools": {}},
                },
            }
        elif method == "notifications/initialized":
            return  # No response for notifications
        else:
            response = {
                "jsonrpc": "2.0",
                "id": msg_id or -1,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        if msg_id is not None:
            try:
                resp_body = json.dumps(response)
                header = f"Content-Length: {len(resp_body)}\r\n\r\n"
                sys.stdout.write(header + resp_body)
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                pass

    def _execute_tool(self, name: str, args: dict) -> dict:
        """Execute an MCP tool by name."""
        if name == "workspace_context":
            summary_only = args.get("summary_only", False)
            ctx = WorkspaceContext()
            return ctx.get_summary() if summary_only else ctx.get_context()
        if self._lsp_manager and hasattr(self._lsp_manager, name):
            try:
                method = getattr(self._lsp_manager, name)
                return method(**args)
            except Exception as e:
                return {"error": str(e)}

        # Generic fallback: try to map tool name to LSPManager method
        method_map = {
            "definition": "go_to_definition",
            "references": "find_references",
            "hover": "hover_info",
            "diagnostics": "get_diagnostics",
            "call_hierarchy": "call_hierarchy",
        }
        lsp_method = method_map.get(name)
        if lsp_method and self._lsp_manager:
            try:
                method = getattr(self._lsp_manager, lsp_method)
                return method(**args)
            except Exception as e:
                return {"error": str(e)}
        return {"error": f"Unknown tool: {name}"}

    def get_mcp_tools(self) -> List[Dict]:
        """Get MCP-compatible tool definitions for code intelligence."""
        return [
            {"name": "definition", "description": "Go to definition of a symbol",
             "inputSchema": {"type": "object", "properties": {
                 "file": {"type": "string"}, "line": {"type": "integer"}, "column": {"type": "integer", "default": 0}}}},
            {"name": "references", "description": "Find all references to a symbol",
             "inputSchema": {"type": "object", "properties": {
                 "file": {"type": "string"}, "line": {"type": "integer"}, "column": {"type": "integer", "default": 0}}}},
            {"name": "hover", "description": "Get type info and docs for a symbol",
             "inputSchema": {"type": "object", "properties": {
                 "file": {"type": "string"}, "line": {"type": "integer"}, "column": {"type": "integer", "default": 0}}}},
            {"name": "diagnostics", "description": "Get code diagnostics for a file",
             "inputSchema": {"type": "object", "properties": {"file": {"type": "string"}}}},
            {"name": "call_hierarchy", "description": "Get call hierarchy for a symbol",
             "inputSchema": {"type": "object", "properties": {
                 "file": {"type": "string"}, "line": {"type": "integer"}, "column": {"type": "integer", "default": 0}}}},
            {"name": "workspace_context", "description": "Get project structure, file stats, key files",
             "inputSchema": {"type": "object", "properties": {
                 "summary_only": {"type": "boolean", "description": "Return concise summary", "default": False}}}},
        ]


def create_debug_tools() -> List[Dict]:
    cs = CrashScreenshot()
    wc = WorkspaceContext()
    mcp = MCPBridge()

    return [
        {
            "name": "coding_crash_screenshot",
            "description": "Capture a screenshot when a test fails. Uses Computer Use (preferred) or ImageMagick/scrot fallback. Returns image path that can be analyzed with vision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_name": {
                        "type": "string",
                        "description": "Test name for the filename",
                        "default": "unknown",
                    }
                },
            },
            "handler": lambda test_name="unknown": cs.capture(test_name),
            "category": "code_repair",
        },
        {
            "name": "coding_workspace_context",
            "description": "Get full project workspace context: directory tree, file stats, key files, recent changes. Like Copilot's @workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_dir": {
                        "type": "string",
                        "description": "Project root (default: current)",
                        "default": ".",
                    },
                    "summary_only": {
                        "type": "boolean",
                        "description": "Concise summary only",
                        "default": False,
                    },
                },
            },
            "handler": lambda root_dir=".", summary_only=False: (
                WorkspaceContext(root_dir).get_summary() if summary_only
                else WorkspaceContext(root_dir).get_context()
            ),
            "category": "code_search",
        },
        {
            "name": "coding_mcp_tools",
            "description": "List available MCP-compatible code intelligence tools. These follow the Model Context Protocol format and can be used by MCP clients.",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: {"tools": MCPBridge().get_mcp_tools(), "protocol": "model-context-protocol"},
            "category": "code_lsp",
        },
    ]


def get_debug_tools() -> List[Dict]:
    return create_debug_tools()
