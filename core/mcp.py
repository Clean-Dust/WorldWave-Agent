"""
ww/core/mcp.py — Model Context Protocol v0.1

First-class MCP support (client + server).
- MCPClient: connects to external MCP servers (stdio or HTTP)
- MCPServer: exposes WW tools as MCP server for IDE integration
- MCPManager: orchestrates multiple servers, lazy tool loading

Supports MCP spec 2024-11-05.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ww.mcp")

# ── JSON-RPC types ──────────────────────────────────────────────

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass
class MCPServerConfig:
    """Configuration for connecting to an MCP server."""
    name: str
    transport: str = "stdio"  # stdio or http
    command: Optional[str] = None  # For stdio: e.g. "npx @modelcontextprotocol/server-filesystem"
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None  # For HTTP transport
    api_key: Optional[str] = None
    enabled: bool = True


@dataclass
class MCPTool:
    """A tool exposed by an MCP server."""
    name: str
    description: str
    parameters: Dict
    server_name: str
    server_config: MCPServerConfig


# ── MCP Client ──────────────────────────────────────────────────

class MCPClient:
    """Connects to external MCP servers and exposes their tools."""
    
    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._process: Optional[asyncio.subprocess.Process] = None
        self._tools: Dict[str, MCPTool] = {}
        self._connected = False
        self._request_id = 0
        
    async def connect(self) -> bool:
        """Initialize connection and handshake."""
        if self.config.transport == "stdio":
            return await self._connect_stdio()
        elif self.config.transport == "http":
            return await self._connect_http()
        return False
        
    async def _connect_stdio(self) -> bool:
        """Connect via stdio subprocess."""
        if not self.config.command:
            logger.error(f"MCP server {self.config.name}: no command specified")
            return False
            
        try:
            env = os.environ.copy()
            env.update(self.config.env)
            
            self._process = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            
            # Initialize
            result = await self._send_request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "clientInfo": {"name": "worldwave", "version": "1.0"},
                "capabilities": {},
            })
            
            if result and "serverInfo" in result:
                # Send initialized notification
                await self._send_notification("notifications/initialized", {})
                # List tools
                tools_result = await self._send_request("tools/list", {})
                if tools_result and "tools" in tools_result:
                    for t in tools_result["tools"]:
                        self._tools[t["name"]] = MCPTool(
                            name=t["name"],
                            description=t.get("description", ""),
                            parameters=t.get("inputSchema", {}),
                            server_name=self.config.name,
                            server_config=self.config,
                        )
                self._connected = True
                logger.info(f"MCP connected to {self.config.name} ({len(self._tools)} tools)")
                return True
                
        except Exception as e:
            logger.error(f"MCP stdio connect failed for {self.config.name}: {e}")
            return False
            
    async def _connect_http(self) -> bool:
        """Connect via HTTP/SSE."""
        if not self.config.url:
            return False
        try:
            import httpx
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
                
            async with httpx.AsyncClient() as client:
                # Initialize
                resp = await client.post(
                    f"{self.config.url}/initialize",
                    json={
                        "jsonrpc": JSONRPC_VERSION,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": MCP_PROTOCOL_VERSION,
                            "clientInfo": {"name": "worldwave", "version": "1.0"},
                            "capabilities": {},
                        },
                        "id": self._next_id(),
                    },
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # List tools
                    resp2 = await client.post(
                        f"{self.config.url}/tools/list",
                        json={"jsonrpc": JSONRPC_VERSION, "method": "tools/list", "id": self._next_id()},
                        headers=headers,
                        timeout=30,
                    )
                    if resp2.status_code == 200:
                        tools_data = resp2.json()
                        for t in tools_data.get("result", {}).get("tools", []):
                            self._tools[t["name"]] = MCPTool(
                                name=t["name"],
                                description=t.get("description", ""),
                                parameters=t.get("inputSchema", {}),
                                server_name=self.config.name,
                                server_config=self.config,
                            )
                    self._connected = True
                    return True
        except Exception as e:
            logger.error(f"MCP HTTP connect failed for {self.config.name}: {e}")
        return False
        
    async def call_tool(self, tool_name: str, arguments: Dict) -> Any:
        """Call a tool on the MCP server."""
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return result
        
    async def _send_request(self, method: str, params: Dict) -> Optional[Dict]:
        """Send JSON-RPC request and get result."""
        if self.config.transport == "stdio" and self._process:
            request = {
                "jsonrpc": JSONRPC_VERSION,
                "method": method,
                "params": params,
                "id": self._next_id(),
            }
            self._process.stdin.write((json.dumps(request) + "\n").encode())
            await self._process.stdin.drain()
            
            line = await asyncio.wait_for(self._process.stdout.readline(), timeout=30)
            if line:
                response = json.loads(line.decode())
                if "error" in response:
                    logger.error(f"MCP error: {response['error']}")
                    return None
                return response.get("result")
        elif self.config.transport == "http" and self.config.url:
            import httpx
            async with httpx.AsyncClient() as client:
                headers = {"Content-Type": "application/json"}
                if self.config.api_key:
                    headers["Authorization"] = f"Bearer {self.config.api_key}"
                resp = await client.post(
                    self.config.url,
                    json={
                        "jsonrpc": JSONRPC_VERSION,
                        "method": method,
                        "params": params,
                        "id": self._next_id(),
                    },
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("result")
        return None
        
    async def _send_notification(self, method: str, params: Dict):
        """Send a JSON-RPC notification (no response expected)."""
        if self._process:
            notif = {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}
            self._process.stdin.write((json.dumps(notif) + "\n").encode())
            await self._process.stdin.drain()
            
    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id
        
    async def disconnect(self):
        """Clean shutdown."""
        if self._process:
            try:
                self._process.stdin.close()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                self._process.kill()
            self._process = None
            self._connected = False
            
    @property
    def tools(self) -> Dict[str, MCPTool]:
        return dict(self._tools)
        
    @property
    def is_connected(self) -> bool:
        return self._connected


# ── MCP Manager ─────────────────────────────────────────────────

class MCPManager:
    """Manages multiple MCP server connections with lazy tool loading."""
    
    def __init__(self):
        self._servers: Dict[str, MCPClient] = {}
        self._all_tools: Dict[str, MCPTool] = {}
        self._configs: List[MCPServerConfig] = []
        
    def configure(self, configs: List[MCPServerConfig]):
        """Set server configurations (does not connect yet)."""
        self._configs = configs
        
    def add_server(self, config: MCPServerConfig):
        """Add a single server config."""
        self._configs.append(config)
        
    def remove_server(self, name: str):
        """Remove a server config."""
        self._configs = [c for c in self._configs if c.name != name]
        if name in self._servers:
            asyncio.create_task(self._servers[name].disconnect())
            del self._servers[name]
            
    async def connect_all(self):
        """Connect to all configured servers."""
        for config in self._configs:
            if not config.enabled:
                continue
            client = MCPClient(config)
            if await client.connect():
                self._servers[config.name] = client
                self._all_tools.update(client.tools)
                
    async def connect_server(self, name: str) -> bool:
        """Connect to a specific server by name."""
        for config in self._configs:
            if config.name == name:
                client = MCPClient(config)
                if await client.connect():
                    self._servers[name] = client
                    self._all_tools.update(client.tools)
                    return True
        return False
        
    def search_tools(self, query: str) -> List[MCPTool]:
        """Lazy search across MCP tools (for Claude Code-style MCP Tool Search)."""
        query_lower = query.lower()
        results = []
        for name, tool in self._all_tools.items():
            if query_lower in name.lower() or query_lower in tool.description.lower():
                results.append(tool)
        return results
        
    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict) -> Any:
        """Call a tool on a specific server."""
        client = self._servers.get(server_name)
        if client:
            return await client.call_tool(tool_name, arguments)
        return None
        
    @property
    def tools(self) -> Dict[str, MCPTool]:
        return dict(self._all_tools)
        
    @property
    def server_count(self) -> int:
        return len(self._servers)
        
    async def shutdown(self):
        """Disconnect all servers."""
        for client in self._servers.values():
            await client.disconnect()
        self._servers.clear()
        self._all_tools.clear()


# ── MCP Server (expose WW as MCP) ───────────────────────────────

class WWMCPServer:
    """Expose Worldwave tools as an MCP server for IDE integration."""
    
    def __init__(self):
        self._tools_cache: Optional[List[Dict]] = None
        
    def _get_ww_tools(self) -> List[Dict]:
        """Get all WW tools as MCP-compatible schemas."""
        if self._tools_cache is not None:
            return self._tools_cache
            
        tools = []
        try:
            from tools.registry import ToolRegistry
            reg = ToolRegistry()
            for tool in reg.list_all():
                tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": {
                        "type": "object",
                        "properties": tool.parameters or {},
                        "required": tool.required_params if hasattr(tool, 'required_params') else [],
                    },
                })
        except Exception:
            pass
            
        self._tools_cache = tools
        return tools
        
    async def run_stdio(self):
        """Run MCP server over stdio (for IDE integration)."""
        logger.info("WW MCP server starting over stdio...")
        
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                    
                request = json.loads(line.strip())
                response = await self._handle_request(request)
                
                if response is not None:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
                    
            except EOFError:
                break
            except Exception as e:
                logger.error(f"MCP server error: {e}")
                error_response = {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": request.get("id") if 'request' in dir() else None,
                    "error": {"code": -32603, "message": str(e)},
                }
                sys.stdout.write(json.dumps(error_response) + "\n")
                sys.stdout.flush()
                
    async def _handle_request(self, request: Dict) -> Optional[Dict]:
        """Handle a single JSON-RPC request."""
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})
        
        if method == "initialize":
            return {
                "jsonrpc": JSONRPC_VERSION,
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {"name": "worldwave", "version": "1.0"},
                    "capabilities": {"tools": {}},
                },
            }
        elif method == "tools/list":
            return {
                "jsonrpc": JSONRPC_VERSION,
                "id": req_id,
                "result": {"tools": self._get_ww_tools()},
            }
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            try:
                from tools.registry import ToolRegistry
                reg = ToolRegistry()
                result = reg.call(tool_name, arguments)
                return {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": str(result)}]},
                }
            except Exception as e:
                return {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True},
                }
        elif method == "notifications/initialized":
            return None  # No response needed
            
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


# Singleton
_mcp_manager: Optional[MCPManager] = None


def get_mcp_manager() -> MCPManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager


# ══════════════════════════════════════════════════════════════
# MCP → ToolRegistry Bridge v0.1
# ══════════════════════════════════════════════════════════════
# Wires MCP tools into WW's ToolRegistry so the agent can call
# them during normal task execution — not just via manual API.

def _make_mcp_handler(server_name: str, tool_name: str, mgr: MCPManager):
    """Create a sync handler for an MCP tool."""
    import asyncio

    def handler(**kwargs) -> dict:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                mgr.call_tool(server_name, tool_name, kwargs)
            )
            return {"ok": True, "result": result, "server": server_name}
        except Exception as e:
            return {"ok": False, "error": str(e), "server": server_name}
        finally:
            loop.close()

    return handler


def register_mcp_tools(registry, mgr: MCPManager = None) -> int:
    """Register all connected MCP tools into a ToolRegistry.

    Call after MCPManager.connect_all() to make MCP tools
    available to the agent during Act phase.

    Returns the number of tools registered.
    """
    from tools.registry import ToolDef, PERMISSION_SAFE

    if mgr is None:
        mgr = get_mcp_manager()

    count = 0
    for tool_name, mcp_tool in mgr.tools.items():
        # Prefix tool name with server to avoid conflicts
        full_name = f"mcp_{mcp_tool.server_name}_{tool_name}"

        # Convert JSON Schema params to ToolRegistry format
        params = _json_schema_to_tool_params(mcp_tool.parameters)

        registry.register(ToolDef(
            name=full_name,
            description=f"[MCP:{mcp_tool.server_name}] {mcp_tool.description}",
            handler=_make_mcp_handler(
                mcp_tool.server_name, tool_name, mgr
            ),
            parameters=params,
            category=f"mcp_{mcp_tool.server_name}",
            permission=PERMISSION_SAFE,
        ))
        count += 1

    logger.info(f"MCP bridge: registered {count} tools from "
                f"{len(mgr._servers)} server(s)")
    return count


def _json_schema_to_tool_params(schema: dict) -> dict:
    """Convert JSON Schema (MCP inputSchema) to ToolRegistry params format.

    JSON Schema: {"type": "object", "properties": {...}, "required": [...]}
    ToolRegistry: {"param_name": {"type": "...", "description": "..."}}
    """
    if not schema or not isinstance(schema, dict):
        return {}

    props = schema.get("properties", {})
    required = schema.get("required", [])

    params = {}
    for name, prop in props.items():
        param = {
            "type": prop.get("type", "string"),
        }
        if "description" in prop:
            param["description"] = prop["description"]
        if name in required:
            param["required"] = True
        if "default" in prop:
            param["default"] = prop["default"]
        if "enum" in prop:
            param["enum"] = prop["enum"]
        params[name] = param

    return params


# ══════════════════════════════════════════════════════════════
# Transport Optimization Layer v0.2
# ══════════════════════════════════════════════════════════════
# Connection pooling, response caching, and request batching
# for MCP STDIO and HTTP+SSE transports.

import time as _time
from collections import OrderedDict
from threading import Lock

@dataclass
class ConnectionPool:
    """Pool MCP connections to avoid repeated STDIO startup costs.

    For STDIO servers (local subprocess), keeps a pool of warm
    connections ready to use. For HTTP+SSE, maintains persistent
    HTTP sessions.
    """

    max_connections: int = 8
    idle_timeout_seconds: int = 300  # 5 min
    _connections: OrderedDict = field(default_factory=OrderedDict)
    _lock: Lock = field(default_factory=Lock)

    def acquire(self, server_uri: str) -> Optional[Any]:
        """Get a connection from the pool or create a new one."""
        with self._lock:
            # Check for idle connection
            if server_uri in self._connections:
                conn, last_used = self._connections[server_uri]
                if _time.time() - last_used < self.idle_timeout_seconds:
                    # Refresh LRU position
                    del self._connections[server_uri]
                    self._connections[server_uri] = (conn, _time.time())
                    return conn
                else:
                    # Expired — remove
                    del self._connections[server_uri]

            return None  # Caller must create new connection

    def release(self, server_uri: str, conn: Any):
        """Return a connection to the pool."""
        with self._lock:
            # Evict oldest if at capacity
            while len(self._connections) >= self.max_connections:
                self._connections.popitem(last=False)

            self._connections[server_uri] = (conn, _time.time())

    def evict_expired(self):
        """Remove all expired connections."""
        with self._lock:
            now = _time.time()
            expired = [
                uri for uri, (_, last_used) in self._connections.items()
                if now - last_used >= self.idle_timeout_seconds
            ]
            for uri in expired:
                del self._connections[uri]


@dataclass
class ResponseCache:
    """Cache MCP tool/resource responses with TTL.

    Reduces latency for repeated tool calls with same arguments
    (e.g., file reads, static data queries).
    """

    max_entries: int = 256
    default_ttl_seconds: int = 30
    _cache: OrderedDict = field(default_factory=OrderedDict)
    _lock: Lock = field(default_factory=Lock)

    def get(self, key: str) -> Optional[Any]:
        """Get cached response. Returns None if missing or expired."""
        with self._lock:
            if key in self._cache:
                value, expiry = self._cache[key]
                if _time.time() < expiry:
                    # Refresh LRU
                    del self._cache[key]
                    self._cache[key] = (value, expiry)
                    return value
                else:
                    del self._cache[key]
            return None

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None):
        """Cache a response with TTL."""
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        with self._lock:
            # Evict oldest if at capacity
            while len(self._cache) >= self.max_entries:
                self._cache.popitem(last=False)

            self._cache[key] = (value, _time.time() + ttl)

    def invalidate(self, key_prefix: str = ""):
        """Invalidate cache entries matching a prefix."""
        with self._lock:
            if not key_prefix:
                self._cache.clear()
            else:
                keys = [k for k in self._cache if k.startswith(key_prefix)]
                for k in keys:
                    del self._cache[k]

    def stats(self) -> Dict[str, int]:
        """Return cache statistics."""
        with self._lock:
            now = _time.time()
            active = sum(1 for _, (_, exp) in self._cache.items() if now < exp)
            return {"size": len(self._cache), "active": active}


@dataclass
class RequestBatcher:
    """Batch multiple MCP tool calls into a single round-trip.

    For HTTP+SSE transport, reduces latency by sending multiple
    JSON-RPC requests in one HTTP call (batch mode). STDIO transport
    benefits from sequential pipelining.

    Collect requests within a configurable window, then flush as a batch.
    """

    max_batch_size: int = 10
    batch_window_ms: int = 50  # Collect window before flush
    _pending: List[Dict[str, Any]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)
    _last_flush: float = 0.0

    def add(self, request: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Add a request to the batch. Returns batch if window expired."""
        with self._lock:
            self._pending.append(request)
            now = _time.time()

            if len(self._pending) >= self.max_batch_size:
                return self._flush_locked()

            if self._pending and now - self._last_flush > (self.batch_window_ms / 1000):
                return self._flush_locked()

            return None  # Keep collecting

    def flush(self) -> Optional[List[Dict[str, Any]]]:
        """Force-flush pending requests."""
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> Optional[List[Dict[str, Any]]]:
        if not self._pending:
            return None
        batch = list(self._pending)
        self._pending.clear()
        self._last_flush = _time.time()
        return batch


# Global instances
_conn_pool = ConnectionPool()
_cache = ResponseCache()
_batcher = RequestBatcher()


def get_connection_pool() -> ConnectionPool:
    return _conn_pool


def get_response_cache() -> ResponseCache:
    return _cache


def get_request_batcher() -> RequestBatcher:
    return _batcher
