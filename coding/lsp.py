"""ww/pm/lsp.py — Language Server Protocol integration for WW-PM v0.1

Implements Gemini's WW-PM Subsystem 3.1.3 and Section 4.1:
- LSP client over stdio JSON-RPC 2.0
- Pyright, tsserver, gopls support
- Semantic code intelligence: goToDefinition, findReferences, hover, call hierarchy
- Real-time diagnostics for the defensive editor

Architecture:
  LSPClient — base JSON-RPC client over stdio
  PyrightClient, TSServerClient — language-specific subclasses
  LSPManager — manages multiple LSP servers, caches connections
"""

from __future__ import annotations
import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ww.lsp")


# ── JSON-RPC 2.0 Protocol ─────────────────────────────────────────────

class JSONRPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


class LSPClient:
    """Base LSP client communicating over stdio with JSON-RPC 2.0.

    Manages the full LSP lifecycle:
    - Process startup/shutdown
    - Initialize/capability handshake
    - Request/response matching via message IDs
    - Notification sending (didOpen, didChange)
    """

    def __init__(
        self,
        command: List[str],
        root_uri: str,
        workspace_folders: List[Dict] = None,
        name: str = "ww-pm-lsp",
    ):
        self._command = command
        self._root_uri = root_uri
        self._workspace_folders = workspace_folders or [{"uri": root_uri, "name": "root"}]
        self._name = name
        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._responses: Dict[str, Any] = {}
        self._response_events: Dict[str, threading.Event] = {}
        self._notifications: queue.Queue = queue.Queue()
        self._request_id = 0
        self._capabilities: Dict = {}
        self._initialized = False
        self._shutdown = False

    def start(self) -> Dict:
        """Start the LSP server process and initialize."""
        if self._process is not None:
            return {"status": "already_running"}

        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            return {"error": f"LSP server not found: {e}"}

        # Start reader thread
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # Initialize handshake
        result = self._initialize()
        if "error" in result:
            return result

        # Send initialized notification
        self._send_notification("initialized", {})

        self._initialized = True
        return {
            "status": "started",
            "server_info": result.get("serverInfo", {}),
            "capabilities": list(self._capabilities.keys())[:20],
        }

    def stop(self):
        """Shutdown the LSP server."""
        self._shutdown = True
        try:
            self._send_request("shutdown", {})
        except Exception:
            pass
        try:
            self._send_notification("exit", {})
        except Exception:
            pass
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
        self._initialized = False

    def open_document(self, path: str, language_id: str = "python", version: int = 1) -> Dict:
        """Notify server that a document was opened."""
        uri = self._path_to_uri(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

        self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": version,
                "text": text,
            }
        })
        return {"uri": uri, "version": version}

    def change_document(self, path: str, version: int = None) -> Dict:
        """Notify server of document changes."""
        uri = self._path_to_uri(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

        self._send_notification("textDocument/didChange", {
            "textDocument": {
                "uri": uri,
                "version": version or int(time.time()),
            },
            "contentChanges": [{"text": text}],
        })
        return {"uri": uri}

    def go_to_definition(self, path: str, line: int, column: int) -> Dict:
        """Get definition location for a symbol at position."""
        uri = self._path_to_uri(path)
        return self._send_request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": column},
        })

    def find_references(self, path: str, line: int, column: int, include_decl: bool = True) -> Dict:
        """Find all references to a symbol."""
        uri = self._path_to_uri(path)
        return self._send_request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": column},
            "context": {"includeDeclaration": include_decl},
        })

    def hover(self, path: str, line: int, column: int) -> Dict:
        """Get hover info (type signature, docs) for a symbol."""
        uri = self._path_to_uri(path)
        result = self._send_request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": column},
        })

        if result and "contents" in result:
            contents = result["contents"]
            if isinstance(contents, dict):
                return {
                    "kind": contents.get("kind", ""),
                    "value": contents.get("value", ""),
                }
            elif isinstance(contents, list):
                return {"value": "\n".join(
                    c["value"] if isinstance(c, dict) else str(c) for c in contents
                )}
            return {"value": str(contents)}
        return {}

    def get_diagnostics(self, path: str) -> List[Dict]:
        """Get current diagnostics for a file (from cached notifications)."""
        uri = self._path_to_uri(path)
        # Collect all pending diagnostics from notifications
        diags = []
        while not self._notifications.empty():
            try:
                notification = self._notifications.get_nowait()
                if notification.get("method") == "textDocument/publishDiagnostics":
                    if notification["params"]["uri"] == uri:
                        diags.extend(notification["params"]["diagnostics"])
            except queue.Empty:
                break
        return diags

    def document_symbols(self, path: str) -> Dict:
        """Get all symbols in a document."""
        uri = self._path_to_uri(path)
        return self._send_request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })

    def prepare_call_hierarchy(self, path: str, line: int, column: int) -> Dict:
        """Prepare call hierarchy for a symbol."""
        uri = self._path_to_uri(path)
        result = self._send_request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": column},
        })
        if result and isinstance(result, list) and len(result) > 0:
            item = result[0]
            return {
                "name": item.get("name", ""),
                "kind": item.get("kind", 0),
                "uri": item.get("uri", ""),
                "range": item.get("range", {}),
            }
        return result or {}

    def incoming_calls(self, item: Dict) -> Dict:
        """Get callers of a call hierarchy item."""
        return self._send_request("callHierarchy/incomingCalls", {
            "item": item,
        })

    def outgoing_calls(self, item: Dict) -> Dict:
        """Get callees of a call hierarchy item."""
        return self._send_request("callHierarchy/outgoingCalls", {
            "item": item,
        })

    def _initialize(self) -> Dict:
        """Perform LSP initialize handshake."""
        result = self._send_request("initialize", {
            "processId": os.getpid(),
            "clientInfo": {"name": self._name, "version": "0.1"},
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "references": {},
                    "definition": {},
                    "documentSymbol": {},
                    "diagnostics": {},
                    "callHierarchy": {},
                },
                "workspace": {
                    "didChangeWatchedFiles": {"dynamicRegistration": False},
                },
            },
            "rootUri": self._root_uri,
            "workspaceFolders": self._workspace_folders,
        })

        if result and "capabilities" in result:
            self._capabilities = result["capabilities"]
        return result or {}

    def _send_request(self, method: str, params: Dict) -> Optional[Any]:
        """Send a JSON-RPC request and wait for response."""
        self._request_id += 1
        req_id = str(self._request_id)
        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        event = threading.Event()
        self._response_events[req_id] = event
        self._responses[req_id] = None

        self._write_message(message)
        event.wait(timeout=10)  # 10s timeout per request

        response = self._responses.pop(req_id, None)
        self._response_events.pop(req_id, None)

        if response is None:
            logger.warning("LSP request timed out: %s", method)
            return None

        if "error" in response:
            raise JSONRPCError(
                response["error"]["code"],
                response["error"]["message"],
                response["error"].get("data"),
            )

        return response.get("result")

    def _send_notification(self, method: str, params: Dict):
        """Send a JSON-RPC notification (no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._write_message(message)

    def _write_message(self, message: Dict):
        """Write a JSON-RPC message with LSP header."""
        if self._process is None or self._process.stdin is None:
            return

        body = json.dumps(message)
        header = f"Content-Length: {len(body)}\r\n\r\n"

        with self._write_lock:
            try:
                self._process.stdin.write(header + body)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                logger.error("LSP write failed: %s", e)

    def _read_loop(self):
        """Background thread: read LSP server responses."""
        if self._process is None or self._process.stdout is None:
            return

        remaining = ""
        while not self._shutdown:
            try:
                # Read raw bytes from stdout
                raw = self._process.stdout.read(1)
                if not raw:
                    break
                remaining += raw

                # Try to parse complete messages
                while True:
                    parsed, remaining, consumed = self._try_parse_message(remaining)
                    if parsed is None:
                        break
                    self._handle_message(parsed)
                    remaining = remaining[consumed:]

            except (ValueError, OSError, AttributeError):
                break

    def _try_parse_message(self, data: str) -> Tuple[Optional[Dict], str, int]:
        """Try to parse a JSON-RPC message from the data buffer."""
        # Look for Content-Length header
        if "Content-Length: " not in data:
            return None, data, 0

        # Parse header
        header_end = data.find("\r\n\r\n")
        if header_end == -1:
            return None, data, 0

        header_part = data[:header_end]
        body_start = header_end + 4

        # Extract content length
        content_length = 0
        for line in header_part.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())

        if content_length == 0:
            return None, data, 0

        # Check if we have the full body
        if len(data) < body_start + content_length:
            return None, data, 0

        body = data[body_start:body_start + content_length]
        try:
            message = json.loads(body)
            return message, data, body_start + content_length
        except json.JSONDecodeError:
            return None, data, body_start + content_length

    def _handle_message(self, message: Dict):
        """Route incoming messages (responses or notifications)."""
        if "id" in message:
            # Response to a request
            req_id = str(message["id"])
            if req_id in self._response_events:
                self._responses[req_id] = message
                self._response_events[req_id].set()
        else:
            # Notification
            self._notifications.put(message)

    def _path_to_uri(self, path: str) -> str:
        """Convert file path to file:// URI."""
        abs_path = os.path.abspath(path)
        return f"file://{abs_path}"

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._initialized

    @property
    def capabilities(self) -> Dict:
        return dict(self._capabilities)


# ── LSP Manager ───────────────────────────────────────────────────────

LSP_LANGUAGE_CONFIGS = {
    "python": {
        "command": ["pyright-langserver", "--stdio"],
        "language_id": "python",
    },
    "typescript": {
        "command": ["typescript-language-server", "--stdio"],
        "language_id": "typescript",
    },
    "javascript": {
        "command": ["typescript-language-server", "--stdio"],
        "language_id": "javascript",
    },
    "go": {
        "command": ["gopls"],
        "language_id": "go",
    },
}

# Alternative: use node-based pyright
FALLBACK_COMMANDS = {
    "python": [
        ["basedpyright-langserver", "--stdio"],
        ["node", "/usr/lib/node_modules/pyright/langserver.js", "--stdio"],
        ["npx", "pyright", "--langserver", "--stdio"],
    ],
}


class LSPManager:
    """Manages multiple LSP server instances by language.

    Caches connections, auto-starts servers on first use,
    and provides unified tool interface.
    """

    def __init__(self, root_dir: str = "."):
        self._root_dir = os.path.abspath(root_dir)
        self._root_uri = f"file://{self._root_dir}"
        self._clients: Dict[str, LSPClient] = {}
        self._open_documents: Dict[str, str] = {}  # path -> language

    def start_for_language(self, language: str) -> Dict:
        """Start an LSP server for a specific language."""
        if language in self._clients:
            client = self._clients[language]
            if client.is_running:
                return {"status": "already_running", "language": language}

        config = LSP_LANGUAGE_CONFIGS.get(language)
        if config is None:
            # Try fallback commands
            fallbacks = FALLBACK_COMMANDS.get(language, [])
            for cmd in fallbacks:
                client = LSPClient(cmd, self._root_uri, name=f"ww-lsp-{language}")
                result = client.start()
                if "error" not in result:
                    self._clients[language] = client
                    return {"status": "started", "language": language, "command": cmd[0]}
            return {"error": f"No LSP server available for {language}"}

        client = LSPClient(config["command"], self._root_uri, name=f"ww-lsp-{language}")
        result = client.start()
        if "error" in result:
            return {"error": f"Failed to start LSP for {language}: {result['error']}"}

        self._clients[language] = client
        return {"status": "started", "language": language, "command": config["command"][0]}

    def open_file(self, path: str) -> Dict:
        """Open a file with the appropriate LSP server."""
        language = self._detect_language(path)
        if language is None:
            return {"error": f"Unsupported language: {path}"}

        # Start server if needed
        if language not in self._clients:
            result = self.start_for_language(language)
            if "error" in result:
                return result

        client = self._clients[language]
        config = LSP_LANGUAGE_CONFIGS.get(language, {})
        result = client.open_document(path, config.get("language_id", language))
        self._open_documents[path] = language
        return {
            "status": "opened",
            "language": language,
            "uri": result.get("uri", ""),
        }

    def go_to_definition(self, path: str, line: int, column: int = 0) -> Dict:
        """Find definition of symbol at position."""
        language = self._detect_language(path)
        if language not in self._clients:
            return {"error": "LSP not started for this language"}

        self._ensure_open(path, language)
        client = self._clients[language]
        result = client.go_to_definition(path, line, column)

        if result and isinstance(result, list) and len(result) > 0:
            loc = result[0]
            if isinstance(loc, dict):
                uri = loc.get("uri", "")
                range_data = loc.get("range", {})
                start = range_data.get("start", {})
                return {
                    "uri": uri,
                    "file": uri.replace("file://", ""),
                    "line": start.get("line", 0) + 1,
                    "column": start.get("character", 0),
                }
        return {"error": "Definition not found"}

    def find_references(self, path: str, line: int, column: int = 0) -> Dict:
        """Find all references to symbol at position."""
        language = self._detect_language(path)
        if language not in self._clients:
            return {"error": "LSP not started"}

        self._ensure_open(path, language)
        client = self._clients[language]
        result = client.find_references(path, line, column)

        references = []
        if result and isinstance(result, list):
            for ref in result:
                if isinstance(ref, dict):
                    uri = ref.get("uri", "")
                    r = ref.get("range", {})
                    start = r.get("start", {})
                    references.append({
                        "file": uri.replace("file://", ""),
                        "line": start.get("line", 0) + 1,
                        "column": start.get("character", 0),
                    })

        return {"references": references, "count": len(references)}

    def hover_info(self, path: str, line: int, column: int = 0) -> Dict:
        """Get type signature and docs for symbol."""
        language = self._detect_language(path)
        if language not in self._clients:
            return {"error": "LSP not started"}

        self._ensure_open(path, language)
        client = self._clients[language]
        return client.hover(path, line, column)

    def call_hierarchy(self, path: str, line: int, column: int = 0) -> Dict:
        """Get call hierarchy for a symbol (who calls it, who it calls)."""
        language = self._detect_language(path)
        if language not in self._clients:
            return {"error": "LSP not started"}

        self._ensure_open(path, language)
        client = self._clients[language]
        result = client.prepare_call_hierarchy(path, line, column)
        if not result or "name" not in result:
            return {"error": "No call hierarchy data available"}

        # Get incoming (callers) and outgoing (callees)
        item = {
            "name": result["name"],
            "kind": result.get("kind", 0),
            "uri": result.get("uri", ""),
            "range": result.get("range", {}),
        }
        incoming = client.incoming_calls(item)
        outgoing = client.outgoing_calls(item)

        callers = []
        if incoming and isinstance(incoming, list):
            for call in incoming:
                frm = call.get("from", {})
                frm_range = frm.get("range", {})
                callers.append({
                    "name": frm.get("name", "?"),
                    "uri": frm.get("uri", ""),
                    "line": frm_range.get("start", {}).get("line", 0) + 1,
                })

        callees = []
        if outgoing and isinstance(outgoing, list):
            for call in outgoing:
                to = call.get("to", {})
                to_range = to.get("range", {})
                callees.append({
                    "name": to.get("name", "?"),
                    "uri": to.get("uri", ""),
                    "line": to_range.get("start", {}).get("line", 0) + 1,
                    "from_ranges": call.get("fromRanges", []),
                })

        return {
            "symbol": result["name"],
            "callers": callers[:20],
            "caller_count": len(callers),
            "callees": callees[:20],
            "callee_count": len(callees),
        }

    def get_diagnostics(self, path: str) -> Dict:
        """Get real-time diagnostics for a file."""
        language = self._open_documents.get(path)
        if language not in self._clients:
            return {"diagnostics": [], "count": 0}

        client = self._clients[language]
        diags = client.get_diagnostics(path)
        return {"diagnostics": diags, "count": len(diags)}

    def stop_all(self):
        """Stop all LSP servers."""
        for lang, client in self._clients.items():
            try:
                client.stop()
            except Exception as e:
                logger.warning("Error stopping LSP %s: %s", lang, e)
        self._clients.clear()
        self._open_documents.clear()

    def get_status(self) -> Dict:
        """Get status of all LSP connections."""
        status = {}
        for lang, client in self._clients.items():
            status[lang] = {
                "running": client.is_running,
                "capabilities": list(client.capabilities.keys())[:10] if client.capabilities else [],
            }
        return {
            "servers": status,
            "open_documents": len(self._open_documents),
            "languages_available": list(LSP_LANGUAGE_CONFIGS.keys()),
        }

    def _detect_language(self, path: str) -> Optional[str]:
        """Detect language from file extension."""
        ext = os.path.splitext(path)[1].lower()
        mapping = {
            ".py": "python",
            ".pyi": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
            ".go": "go",
        }
        return mapping.get(ext)

    def _ensure_open(self, path: str, language: str):
        """Ensure a file is opened with the LSP server."""
        if path not in self._open_documents:
            client = self._clients.get(language)
            if client:
                config = LSP_LANGUAGE_CONFIGS.get(language, {})
                client.open_document(path, config.get("language_id", language))
                self._open_documents[path] = language


# ── Tool definitions ──────────────────────────────────────────────────

_manager: LSPManager = None


def get_manager() -> LSPManager:
    global _manager
    if _manager is None:
        _manager = LSPManager()
    return _manager


def create_lsp_tools(manager: LSPManager) -> List[Dict]:
    return [
        {
            "name": "coding_lsp_start",
            "description": "Start an LSP server for code intelligence. Supports: python (pyright), typescript, javascript, go (gopls). Provides IDE-level symbol resolution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "typescript", "javascript", "go"],
                        "description": "Programming language to start LSP for",
                    }
                },
                "required": ["language"],
            },
            "handler": manager.start_for_language,
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_definition",
            "description": "Find the definition of a symbol at the given file position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "File path"},
                    "line": {"type": "integer", "description": "Line number (1-indexed)"},
                    "column": {"type": "integer", "description": "Column (0-indexed)", "default": 0},
                },
                "required": ["file", "line"],
            },
            "handler": lambda file, line, column=0: manager.go_to_definition(file, line, column),
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_references",
            "description": "Find all references to a symbol across the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "File path"},
                    "line": {"type": "integer", "description": "Line number (1-indexed)"},
                    "column": {"type": "integer", "description": "Column", "default": 0},
                },
                "required": ["file", "line"],
            },
            "handler": lambda file, line, column=0: manager.find_references(file, line, column),
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_hover",
            "description": "Get type signature, documentation, and hover info for a symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "File path"},
                    "line": {"type": "integer", "description": "Line number"},
                    "column": {"type": "integer", "description": "Column", "default": 0},
                },
                "required": ["file", "line"],
            },
            "handler": lambda file, line, column=0: manager.hover_info(file, line, column),
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_diagnostics",
            "description": "Get real-time code diagnostics (errors, warnings) for a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "File path"},
                },
                "required": ["file"],
            },
            "handler": manager.get_diagnostics,
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_status",
            "description": "List active LSP server connections and open documents.",
            "parameters": {"type": "object", "properties": {}},
            "handler": manager.get_status,
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_open",
            "description": "Open a file with an LSP server so it gets real-time diagnostics and code intelligence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "File path to open"},
                },
                "required": ["file"],
            },
            "handler": manager.open_file,
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_call_hierarchy",
            "description": "Get call hierarchy for a symbol: shows who calls this function (incoming) and what it calls (outgoing). Requires an LSP server (coding_lsp_start).",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "File path"},
                    "line": {"type": "integer", "description": "Line number (1-indexed)"},
                    "column": {"type": "integer", "description": "Column", "default": 0},
                },
                "required": ["file", "line"],
            },
            "handler": lambda file, line, column=0: manager.call_hierarchy(file, line, column),
            "category": "code_lsp",
        },
        {
            "name": "coding_lsp_symbols",
            "description": "List all symbols (functions, classes, variables) in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "File path"},
                },
                "required": ["file"],
            },
            "handler": lambda file: _safe_lsp_symbols(manager, file),
            "category": "code_lsp",
        },
    ]



def _safe_lsp_symbols(manager: "LSPManager", file: str) -> Dict:
    """Safely get document symbols with error handling."""
    lang = manager._detect_language(file)
    if not lang:
        return {"error": "Unknown language for file"}
    manager._ensure_open(file, lang)
    client = manager._clients.get(lang)
    if not client:
        return {"error": f"No LSP client for {lang}"}
    return client.document_symbols(file)


def get_lsp_tools() -> List[Dict]:
    return create_lsp_tools(get_manager())
