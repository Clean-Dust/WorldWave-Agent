"""
ww/core/acp.py — Agent Client Protocol (ACP) Server v0.1

Implements the ACP protocol for IDE integration (VS Code, JetBrains).
ACP is a simple JSON-based protocol over stdio:
- Agent discovery: IDE discovers the agent's capabilities
- Tool invocation: IDE calls agent tools
- Streaming: agent streams output back to IDE
"""

from __future__ import annotations
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("ww.acp")

ACP_VERSION = "1.0"


@dataclass
class ACPCapability:
    """A capability the agent exposes to the IDE."""
    name: str
    description: str
    type: str  # "tool", "command", "completion"
    parameters: Dict = field(default_factory=dict)


class ACPServer:
    """ACP server that runs over stdio for IDE integration."""
    
    def __init__(self):
        self._capabilities: Dict[str, ACPCapability] = {}
        self._running = False
        
    def register_capability(self, cap: ACPCapability):
        """Register an agent capability."""
        self._capabilities[cap.name] = cap
        
    def register_tools_as_capabilities(self):
        """Auto-register all WW tools as ACP capabilities."""
        try:
            from tools.registry import ToolRegistry
            reg = ToolRegistry()
            for tool in reg.list_all():
                self.register_capability(ACPCapability(
                    name=tool.name,
                    description=tool.description,
                    type="tool",
                    parameters=tool.parameters or {},
                ))
        except Exception as e:
            logger.warning(f"Failed to register tools: {e}")
            
    async def start(self):
        """Start the ACP server over stdio."""
        self._running = True
        logger.info(f"ACP server v{ACP_VERSION} starting on stdio...")
        
        # Send capabilities on startup
        await self._send({
            "type": "ready",
            "version": ACP_VERSION,
            "capabilities": [
                {"name": c.name, "description": c.description, "type": c.type, "parameters": c.parameters}
                for c in self._capabilities.values()
            ],
        })
        
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                    
                request = json.loads(line.strip())
                response = await self._handle_message(request)
                
                if response is not None:
                    await self._send(response)
                    
            except EOFError:
                break
            except json.JSONDecodeError:
                continue
            except Exception as e:
                await self._send({"type": "error", "message": str(e)})
                
    async def _handle_message(self, msg: Dict) -> Optional[Dict]:
        """Handle an incoming ACP message."""
        msg_type = msg.get("type", "")
        msg_id = msg.get("id")
        
        if msg_type == "ping":
            return {"type": "pong", "id": msg_id}
            
        elif msg_type == "capabilities":
            return {
                "type": "capabilities",
                "id": msg_id,
                "capabilities": [
                    {"name": c.name, "description": c.description, "type": c.type}
                    for c in self._capabilities.values()
                ],
            }
            
        elif msg_type == "invoke":
            tool_name = msg.get("tool")
            params = msg.get("params", {})
            
            if tool_name in self._capabilities:
                try:
                    from tools.registry import ToolRegistry
                    reg = ToolRegistry()
                    result = reg.call(tool_name, params)
                    return {
                        "type": "result",
                        "id": msg_id,
                        "tool": tool_name,
                        "content": str(result),
                    }
                except Exception as e:
                    return {
                        "type": "error",
                        "id": msg_id,
                        "tool": tool_name,
                        "message": str(e),
                    }
            else:
                return {
                    "type": "error",
                    "id": msg_id,
                    "message": f"Unknown tool: {tool_name}",
                }
                
        elif msg_type == "shutdown":
            self._running = False
            return {"type": "shutdown", "id": msg_id}
            
        return {"type": "error", "id": msg_id, "message": f"Unknown message type: {msg_type}"}
        
    async def _send(self, msg: Dict):
        """Send a message to the IDE."""
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()
        
    def stop(self):
        """Stop the ACP server."""
        self._running = False


# Singleton
_acp_server: Optional[ACPServer] = None


def get_acp_server() -> ACPServer:
    global _acp_server
    if _acp_server is None:
        _acp_server = ACPServer()
        _acp_server.register_tools_as_capabilities()
    return _acp_server
