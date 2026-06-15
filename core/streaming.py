"""
ww/core/streaming.py — Block Streaming v0.1

Implements Claude Code-style block streaming:
- Completed assistant blocks sent as soon as they finish
- Soft chunking: prefers paragraph breaks, then newlines, then sentence boundaries
- Coalesce: merges streamed chunks to reduce single-line spam
- Block boundary control: text_end or message_end
"""

from __future__ import annotations
import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Dict, List, Optional


class BlockBoundary(Enum):
    TEXT_END = "text_end"        # Send when text content finishes (before tool calls)
    MESSAGE_END = "message_end"  # Send when entire message finishes (after tool results)


@dataclass
class StreamConfig:
    """Configuration for block streaming."""
    enabled: bool = True
    chunk_min: int = 800
    chunk_max: int = 1200
    boundary: BlockBoundary = BlockBoundary.TEXT_END
    coalesce_enabled: bool = True
    coalesce_idle_ms: int = 300  # Wait for idle before sending coalesced chunk
    verbose_tools: bool = True   # Emit tool start notifications


@dataclass
class StreamBlock:
    """A single streamed block."""
    type: str  # "text", "tool_start", "tool_result", "error", "done"
    content: str
    tool_name: Optional[str] = None
    tool_id: Optional[str] = None
    is_final: bool = False
    metadata: Dict = field(default_factory=dict)


class TextChunker:
    """Intelligently chunk text at natural boundaries."""
    
    @staticmethod
    def chunk(text: str, min_size: int = 800, max_size: int = 1200) -> List[str]:
        """Split text into chunks at natural boundaries."""
        if len(text) <= max_size:
            return [text] if text else []
        
        chunks = []
        remaining = text
        
        while len(remaining) > max_size:
            # Try paragraph break within window
            window = remaining[min_size:max_size]
            para_match = list(re.finditer(r'\n\s*\n', window))
            if para_match:
                split_at = min_size + para_match[-1].start()
                chunks.append(remaining[:split_at])
                remaining = remaining[split_at:].lstrip()
                continue
            
            # Try newline
            nl_match = list(re.finditer(r'\n', window))
            if nl_match:
                split_at = min_size + nl_match[-1].start()
                chunks.append(remaining[:split_at])
                remaining = remaining[split_at:].lstrip()
                continue
            
            # Try sentence boundary (。.!? followed by space/newline)
            sent_match = list(re.finditer(r'[。.!?]\s', window))
            if sent_match:
                split_at = min_size + sent_match[-1].end()
                chunks.append(remaining[:split_at])
                remaining = remaining[split_at:]
                continue
            
            # Hard break at max_size
            chunks.append(remaining[:max_size])
            remaining = remaining[max_size:]
        
        if remaining:
            chunks.append(remaining)
        
        return chunks


class BlockStreamer:
    """Manages streaming output with chunking and coalescing."""
    
    def __init__(self, config: StreamConfig = None):
        self.config = config or StreamConfig()
        self._chunker = TextChunker()
        self._buffer: List[StreamBlock] = []
        self._last_send_time = 0
        self._coalesce_timer: Optional[asyncio.Task] = None
        self._subscribers: List[callable] = []
        
    def subscribe(self, callback: callable):
        """Subscribe to stream blocks. callback(StreamBlock) is called for each block."""
        self._subscribers.append(callback)
        
    def unsubscribe(self, callback: callable):
        """Remove a subscriber."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)
            
    async def _notify(self, block: StreamBlock):
        """Notify all subscribers."""
        for cb in self._subscribers:
            try:
                result = cb(block)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
                
    async def emit_text(self, text: str):
        """Emit text content, chunked at natural boundaries."""
        if not self.config.enabled or not text:
            return
            
        chunks = self._chunker.chunk(text, self.config.chunk_min, self.config.chunk_max)
        for chunk in chunks:
            block = StreamBlock(type="text", content=chunk)
            if self.config.coalesce_enabled:
                self._buffer.append(block)
                await self._maybe_flush()
            else:
                await self._notify(block)
                
    async def emit_tool_start(self, tool_name: str, tool_id: str = None):
        """Emit a tool invocation start notification."""
        if self.config.verbose_tools:
            block = StreamBlock(
                type="tool_start",
                content=f"Calling {tool_name}...",
                tool_name=tool_name,
                tool_id=tool_id,
            )
            await self._notify(block)
            
    async def emit_tool_result(self, tool_name: str, result: str, tool_id: str = None):
        """Emit a tool result."""
        block = StreamBlock(
            type="tool_result",
            content=result[:2000],  # Truncate long results
            tool_name=tool_name,
            tool_id=tool_id,
        )
        await self._notify(block)
        
    async def emit_error(self, message: str):
        """Emit an error block."""
        block = StreamBlock(type="error", content=message)
        await self._notify(block)
        
    async def emit_done(self, metadata: Dict = None):
        """Signal completion."""
        block = StreamBlock(type="done", content="", is_final=True, metadata=metadata or {})
        # Flush any remaining buffer
        await self._flush_now()
        await self._notify(block)
        
    async def _maybe_flush(self):
        """Flush buffer after coalesce idle time."""
        now = time.time()
        if now - self._last_send_time >= self.config.coalesce_idle_ms / 1000:
            await self._flush_now()
        else:
            # Schedule delayed flush
            if self._coalesce_timer and not self._coalesce_timer.done():
                self._coalesce_timer.cancel()
            self._coalesce_timer = asyncio.create_task(self._delayed_flush())
            
    async def _delayed_flush(self):
        """Wait for coalesce idle period, then flush."""
        await asyncio.sleep(self.config.coalesce_idle_ms / 1000)
        await self._flush_now()
        
    async def _flush_now(self):
        """Immediately send all buffered blocks coalesced into one."""
        if not self._buffer:
            return
        combined = "\n".join(b.content for b in self._buffer)
        block = StreamBlock(type="text", content=combined)
        self._buffer.clear()
        self._last_send_time = time.time()
        await self._notify(block)


# SSE (Server-Sent Events) helper for web streaming
async def sse_event_stream(streamer: BlockStreamer) -> AsyncIterator[str]:
    """Convert BlockStreamer output to SSE format for HTTP streaming."""
    queue = asyncio.Queue()
    
    async def collector(block: StreamBlock):
        await queue.put(block)
    
    streamer.subscribe(collector)
    
    try:
        while True:
            block = await queue.get()
            event_data = {
                "type": block.type,
                "content": block.content,
                "tool_name": block.tool_name,
                "is_final": block.is_final,
                "metadata": block.metadata,
            }
            import json
            yield f"data: {json.dumps(event_data)}\n\n"
            if block.is_final:
                break
    finally:
        streamer.unsubscribe(collector)
