"""
ww/server.py — Worldwave API Server v0.3

Provides HTTP API for the full WW lifecycle:
- Single task execution (sync/async)
- Scheduler (cron task management)
- Autonomous loop (timed spirals)
- Multi-LLM support (auto routing/degradation)
- Memory operations (recall/store/sleep/search)
- HITL approval/rejection
- Observability dashboard
"""

from __future__ import annotations
import os

# Load .env file before any other imports so API keys are available
try:
    from dotenv import load_dotenv
    for _env_candidate in [
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        os.path.expanduser("~/.ww/.env"),
    ]:
        if os.path.exists(_env_candidate):
            load_dotenv(_env_candidate)
            break
except ImportError:
    pass
import sys
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from pydantic import BaseModel, Field
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.loop import Worldwave
from core.config import ConfigManager
from core.scheduler import Scheduler, ScheduledTask
from core.memory import MemorySystem
from tools.registry import default_registry
from tools.skill_manager import SkillManager
# GatewayManager + TelegramGateway are lazy-loaded in _init_gateway()
# (they may not exist on fresh install — gateway is optional)
from contacts import ContactManager, register_api_routes
from core.credentials import CredentialStore
from core.credentials import get_credential_manager as get_credential_manager_v2
from coding import register_tools as register_coding_tools
# Wavegate Agent gRPC server — imported lazily (grpcio is optional)

logger = logging.getLogger("ww")

# Ensure logging output is visible — without basicConfig, all log.*() calls are silently discarded.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)

# Version — read from version.txt once
WW_VERSION = "0.5.0-dev"
try:
    _vp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt")
    if os.path.exists(_vp):
        with open(_vp) as _f:
            _v = _f.read().strip()
            if _v:
                WW_VERSION = _v
except Exception:
    pass

# ── Pydantic Models ──

class TaskRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    max_spirals: int = Field(10, ge=1, le=50)
    model: Optional[str] = None
    provider: Optional[str] = None
    image_path: Optional[str] = None
    reasoning_effort: Optional[str] = None  # DeepSeek: low/medium/high/xhigh

class AutonomousRequest(BaseModel):
    interval: int = Field(300, ge=30, le=86400)
    max_spirals_per_cycle: int = Field(3, ge=1, le=10)
    model: Optional[str] = None

class CodeRequest(BaseModel):
    code: str = Field(..., min_length=1)
    context: Optional[Dict] = None

class MemoryRequest(BaseModel):
    action: str = Field("recall", pattern="^(recall|search|snapshot|sleep|probe|store)$")
    query: str = ""
    content: str = ""
    entities: List[str] = []
    limit: int = 10

class GatewayMessage(BaseModel):
    platform: str = Field(..., pattern="^(telegram|discord|slack|mqtt|custom)$")
    channel: str = ""
    message: str = Field(..., min_length=1)
    user: str = ""

class TelegramMessage(BaseModel):
    chat_id: str = Field("-1003841986648", description="Telegram chat ID, default Working Space")
    message: str = Field(..., min_length=1)
    parse_mode: str = "Markdown"
    disable_preview: bool = True

class TelegramPhoto(BaseModel):
    chat_id: str = Field("-1003841986648", description="Telegram chat ID")
    photo_url: str = Field(..., description="Image URL or local path")
    caption: str = ""
    parse_mode: str = "Markdown"

class TelegramFile(BaseModel):
    chat_id: str = Field("-1003841986648", description="Telegram chat ID")
    file_path: str = Field(..., description="Absolute path of local file")
    caption: str = ""

class ScheduleRequest(BaseModel):
    name: str = ""
    goal: str = Field(..., min_length=1)
    schedule: str = Field(..., description="cron expr or 'every Nm' or ISO time")
    max_spirals: int = 3

class SkillRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    category: str = "general"
    steps: List[str] = []
    pitfalls: List[str] = []
    body: str = ""

class ConfigRequest(BaseModel):
    key: str = Field(..., min_length=1)
    value: Any = None


class WorldwaveServer:
    """WW Server v0.3 — Scheduler + Skills + Config Integration."""

    def __init__(self):
        self.persist_dir = os.path.join(os.path.expanduser("~"), ".ww_data")
        os.makedirs(self.persist_dir, exist_ok=True)

        # Core System
        self.ww: Optional[Worldwave] = None
        self.config = ConfigManager()
        self.scheduler = Scheduler()
        self.skills = SkillManager()
        self.sandbox = None  # CodeSandbox — lazy import

        # Built-in Memory System (replaces external v2)
        self.memory = MemorySystem()

        # Autonomous Loop Control
        self._autonomous_thread: Optional[threading.Thread] = None
        self._autonomous_running = False
        self._last_result: Optional[Dict] = None

        # History Record
        self._task_history: List[Dict] = []

        # Async Task Queue (proper tracking, not fire-and-forget)
        from core.task_queue import TaskQueue
        self.task_queue = TaskQueue(max_workers=3)

        # Contacts module (distributed multi-agent address book)
        self.contacts = ContactManager()

        # Credentials manager (encrypted API key/secret storage)
        self.credentials = CredentialStore()

        # Initialization
        self._init_ww()
        self._scheduler_goal = None  # Used for scheduler callback

        # Telegram gateway (built-in Worldwave, not external patch)
        try:
            from gateway import GatewayManager
            self.gateway = GatewayManager()
            self._init_gateway()
            print("[WW] Gateway init: OK", flush=True)
        except Exception as e:
            print(f"[WW] Gateway init skipped: {e}", flush=True)
            self.gateway = None

        # Agent gRPC server (Wavegate control plane interface)
        self.agent_grpc_server = None
        self.agent_grpc_service = None

    def _init_ww(self):
        """Create or rebuild WW instance (with built-in memory system + subconscious routing)."""
        model = self.config.get("model", "deepseek/deepseek-v4-flash")
        tools = default_registry()
        # Register contacts tools into the same registry WW uses
        self.contacts.register_tools(tools)
        # Register credentials tools
        if hasattr(self.credentials, 'register_tools'):
            self.credentials.register_tools(tools)
        # Register WW-PM tools (defensive ACI, shell, planning)
        count = register_coding_tools(tools)
        logger.info("WW-PM tools registered: %d tools", count)
        # Register MCP tools (bridge: MCP → ToolRegistry)
        self._register_mcp_tools(tools)
        self.ww = Worldwave(
            model=model,
            persist_dir=self.persist_dir,
            memory_system=self.memory,
            tools=tools,
        )

    def _register_mcp_tools(self, registry):
        """Register MCP tools into the agent's ToolRegistry.

        Called during _init_ww() to bridge MCP server tools
        into the WW agent's tool set.  Re-syncs whenever new
        MCP servers connect.
        """
        try:
            from core.mcp import register_mcp_tools, get_mcp_manager
            mgr = get_mcp_manager()
            if mgr.tools:
                count = register_mcp_tools(registry, mgr)
                logger.info("MCP bridge: %d tools wired into agent", count)
        except Exception as e:
            logger.warning("MCP bridge skipped: %s", e)

    def _init_gateway(self):
        """Initialize Telegram gateway (WW built-in)."""
        from gateway.bridge import TelegramGateway  # lazy import for scope
        print("[WW] _init_gateway() called", flush=True)
        token = os.environ.get("TELEGRAM_WW_TOKEN", "")
        workspace_raw = os.environ.get("TELEGRAM_WW_WORKSPACE", "")
        if token and workspace_raw:
            try:
                tg = TelegramGateway(
                    token=token,
                    workspace_id=int(workspace_raw),
                    poll_interval=2.0,
                    task_handler=self._gateway_task_handler,
                )
                self.gateway.register(tg)
                logger.info("Telegram gateway registered (workspace=%s)", workspace_raw)
            except Exception as e:
                logger.warning("Telegram gateway init failed: %s", e)
        else:
            logger.info("Telegram gateway not configured")

        # Discord gateway (deprecated — use gateway/adapters/ instead)
        discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if discord_token:
            try:
                from gateway.adapters.discord import DiscordAdapter
                dg = DiscordAdapter(
                    token=discord_token,
                    task_handler=self._gateway_task_handler,
                )
                self.gateway.register(dg)
                logger.info("Discord gateway registered")
            except ImportError:
                logger.info("Discord adapter not yet available (gateway/adapters/discord.py)")
            except Exception as e:
                logger.warning("Discord gateway init failed: %s", e)

        # Webhook gateway (deprecated — use gateway/adapters/ instead)
        try:
            from gateway.adapters.webhook import WebhookAdapter
            wh = WebhookAdapter(task_handler=self._gateway_task_handler)
            self.gateway.register(wh)
            logger.info("Webhook gateway registered")
        except ImportError:
            logger.info("Webhook adapter not yet available (gateway/adapters/webhook.py)")
        except Exception as e:
            logger.warning("Webhook gateway init failed: %s", e)

    def _gateway_task_handler(self, command: str, context: dict) -> str:
        """Handle a task from the gateway (e.g. @mention)."""
        logger.info("Gateway task: %s", command[:100])
        if not self.ww:
            return "WW not initialized"
        try:
            image_path = context.get("photo_path", "") or context.get("image_path", "")
            result = self.run_task(command, max_spirals=3, image_path=image_path)
            status = result.get("status", "?")
            spirals = result.get("spirals_completed", 0)
            
            # Extract the actual response from the last spiral's respond action
            response_text = ""
            spiral_results = result.get("results", [])
            if spiral_results:
                last_spiral = spiral_results[-1]
                actions = last_spiral.get("actions", [])
                for a in reversed(actions):
                    if a.get("tool") == "respond":
                        response_text = a.get("result", {}).get("output", "")
                        break
                # Fallback: check evaluation summary
                if not response_text:
                    eval_result = last_spiral.get("evaluation", {})
                    response_text = eval_result.get("summary", "") or eval_result.get("reason", "")
            
            if response_text:
                return response_text[:1500]
            # Fallback to status if no response text extracted
            return f"[{status}] {command[:200]}"
        except Exception as e:
            logger.error("Gateway task error: %s", e)
            return f"error: {str(e)[:200]}"

    def _get_sandbox(self):
        """Lazy load CodeSandbox. """
        if self.sandbox is None:
            from sandbox.runner import CodeSandbox
            self.sandbox = CodeSandbox()
        return self.sandbox

    # ── Task Execution ──

    def run_task(self, goal: str, max_spirals: int = 10,
                 model: str = "", provider: str = "", image_path: str = "",
                 reasoning_effort: str = "") -> Dict[str, Any]:
        """Execute a task."""
        # Notify Mascot: Start thinking
        try:
            from core.mascot import mascot
            mascot.on_task_start(goal)
        except Exception:
            pass

        if model or provider:
            try:
                llm_config = {"model": model or self.config.get("model", "deepseek/deepseek-v4-flash")}
                if provider:
                    llm_config["provider"] = provider
                self.ww.llm = self.ww.llm.__class__(llm_config)
            except Exception as e:
                logger.warning(f"Model switch failed: {e}")

        self._last_result = self.ww.run(goal, max_spirals, image_path=image_path,
                                        reasoning_effort=reasoning_effort)
        self._task_history.append({
            "goal": goal[:100],
            "time": datetime.now(timezone.utc).isoformat(),
            "status": self._last_result.get("status", "?"),
            "spirals": self._last_result.get("spirals_completed", 0),
        })

        # Notify Mascot: Task completed
        try:
            from core.mascot import mascot
            success = self._last_result.get("status") in ("completed", "success")
            mascot.on_task_complete(success, str(self._last_result.get("result", ""))[:80])
        except Exception:
            pass

        return self._last_result

    def run_in_background(self, goal: str, max_spirals: int = 10) -> Dict:
        """Execute task via async task queue with tracking.

        Returns task_id immediately. Use GET /ww/task/{task_id}
        to check status or retrieve results.
        """
        def _run() -> dict:
            return self.run_task(goal, max_spirals)

        task_id = self.task_queue.submit(goal, _run, max_spirals)
        return {
            "status": "queued",
            "task_id": task_id,
            "goal": goal[:100],
            "hint": f"GET /ww/task/{task_id} for status/result",
        }

    # ── Autonomous Loop ──

    def start_autonomous(self, interval: int = 300, max_spirals: int = 3) -> Dict:
        """Start autonomous loop (background thread)."""
        if self._autonomous_running:
            return {"status": "already_running"}

        self._autonomous_running = True

        def _loop():
            while self._autonomous_running:
                goal = "Evaluate current system status and report any anomalies or important changes"
                self.run_task(goal, max_spirals)
                for _ in range(max_spirals - 1):
                    if not self._autonomous_running:
                        break
                    time.sleep(interval)
                    goal = "Continue exploring areas of interest based on previous observations"
                    self.run_task(goal, max_spirals=2)
                if self._autonomous_running:
                    time.sleep(interval)

        self._autonomous_thread = threading.Thread(target=_loop, daemon=True)
        self._autonomous_thread.start()
        return {"status": "started", "interval": interval}

    def stop_autonomous(self) -> Dict:
        """Stop autonomous loop."""
        self._autonomous_running = False
        if self.ww:
            self.ww.stop()
        return {"status": "stopped"}

    # ── Scheduler ──

    def start_scheduler(self):
        """Start scheduler (background thread)."""
        self._scheduler_goal = None

        def _run_task(task: ScheduledTask) -> str:
            result = self.run_task(task.goal, task.max_spirals)
            status = result.get("status", "?")
            spirals = result.get("spirals_completed", 0)
            return f"status={status}, spirals={spirals}"

        self.scheduler.start(run_callback=_run_task)
        return {"status": "scheduler_started"}

    def stop_scheduler(self) -> Dict:
        """Stop scheduler."""
        self.scheduler.stop()
        return {"status": "scheduler_stopped"}

    # ── Status ──

    def status(self) -> Dict[str, Any]:
        """Current status."""
        state = self.ww.state.summary() if self.ww else {"error": "not_initialized"}

        memory_ok = False
        try:
            import urllib.request
            req = urllib.request.Request(self.config.get("memory_url", "") + "/v2/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                memory_ok = resp.status == 200
        except:
            pass

        return {
            "version": WW_VERSION,
            "ww": {
                "session": state,
                "running": self.ww.running if self.ww else False,
                "tools_count": len(self.ww.tools.tool_names()) if self.ww else 0,
                "tools_categories": self.ww.tools.category_counts() if self.ww else {},
            },
            "autonomous": {"running": self._autonomous_running},
            "scheduler": self.scheduler.info(),
            "memory": {"available": memory_ok},
            "config_profile": self.config.active_profile(),
            "skills_count": len(self.skills.list()),
            "task_history_count": len(self._task_history),
            "evolution": {
                "metrics": self.ww.metrics.summary() if self.ww else {},
                "available": True,
            },
        }

    def task_history(self, limit: int = 10) -> List[Dict]:
        return self._task_history[-limit:]


# ── FastAPI Application ──

app = FastAPI(
    title="Worldwave API v0.3",
    version=WW_VERSION,
    description="LLM-driven autonomous spiral cognitive framework — Scheduler + Skills + Config Integration",
)

# ── API Security Middleware ──
if os.environ.get("WW_API_KEY"):
    WW_API_KEY = os.environ["WW_API_KEY"]
else:
    # Secure-by-default: Auto-generate random key IN-MEMORY only.
    # NEVER write to .env — that would create duplicate keys and corrupt config.
    import secrets
    _auto_key = secrets.token_urlsafe(32)
    WW_API_KEY = _auto_key
    logger.warning("=" * 50)
    logger.warning("⚠️  WW_API_KEY not set! Auto-generated random key (in-memory only, not persisted).")
    logger.warning("🔑  WW_API_KEY=%s", _auto_key)
    logger.warning("    Set WW_API_KEY in your .env to make it permanent.")
    logger.warning("    Authenticate via Authorization: Bearer <key> or ?api_key=<key>")
    logger.warning("=" * 50)

_API_BYPASS_PATHS = {"/ww/health", "/docs", "/openapi.json", "/redoc", "/ww/webui", "/ww/webui/"}

@app.middleware("http")
async def api_auth_middleware(request: Request, call_next):
    """API Key verification middleware.
    
    WW_API_KEY environment variable must be set (auto-generated or manually set).
    All non-whitelist endpoints must include Authorization: Bearer <key> header
    or ?api_key=<key> query parameter.
    """
    if not WW_API_KEY:
        # Theoretically should not reach here (auto-generated during construction)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"error": "Server misconfigured"})

    path = request.url.path
    if any(path.startswith(p) for p in _API_BYPASS_PATHS):
        return await call_next(request)

    # Check header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == WW_API_KEY:
        return await call_next(request)

    # Check query param
    if request.query_params.get("api_key") == WW_API_KEY:
        return await call_next(request)

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=401,
        content={"error": "Unauthorized", "message": "Valid API key required. Set WW_API_KEY env var or pass ?api_key=<key>"},
    )

server = WorldwaveServer()

# Register subconscious API routes (module-level, after app definition)
try:
    from core.subconscious.api import register_routes as _reg_sub
    _reg_sub(app, server.ww.subconscious)
    logger.info("Subconscious API routes registered")
except Exception as _e:
    logger.debug("Subconscious API not available: %s", _e)

# Register contacts API routes
try:
    register_api_routes(app, server.contacts)
    logger.info("Contacts API routes registered")
except Exception as _e:
    logger.debug("Contacts API not available: %s", _e)


# Register Dashboard routes
from core.dashboard import create_dashboard_router
app.include_router(create_dashboard_router())


# ── System Endpoints ──

@app.get("/ww/health")
def health():
    return {
        "status": "ok",
        "version": WW_VERSION,
        "server": "Worldwave",
        "autonomous": server._autonomous_running,
    }


@app.get("/ww/status")
def get_status():
    """Enhanced status endpoint (Dashboard specific)."""
    result = server.status()
    ww = server.ww

    # Add Dashboard-specific fields
    if ww:
        result["current_phase"] = ww.state.current_phase
        result["current_spiral"] = ww.state.current_spiral
        result["running"] = ww.running
        result["steps_completed"] = getattr(ww, "_steps_completed", 0)
        result["steps_total"] = getattr(ww, "_steps_total", 0)
        result["tool_count"] = len(ww.tools.tool_names())
        result["model_count"] = len(ww.llm._models) if hasattr(ww.llm, "_models") and ww.llm._models else 0
        result["tool_categories"] = ww.tools.category_counts()

        # Memory System
        if ww.memory:
            try:
                stats = ww.memory.statistics() if hasattr(ww.memory, "statistics") else ww.memory.stats()
                result["hippocampus_used"] = stats.get("hippocampus_count", stats.get("atoms", 0))
                result["hippocampus_capacity"] = stats.get("hippocampus_capacity", stats.get("capacity", 100))
                result["memory_count"] = stats.get("total_count", stats.get("atoms", 0))
                result["last_sleep"] = stats.get("last_consolidation", stats.get("last_sleep", "-"))
            except Exception:
                pass

        # Token Estimation
        if hasattr(ww, "_tool_history"):
            result["tool_calls_last"] = len(ww._tool_history)

    try:
        result["hostname"] = os.uname().nodename
    except AttributeError:
        import platform
        result["hostname"] = platform.node()
    return result


@app.get("/ww")
def root():
    return {
        "name": "Worldwave",
        "version": WW_VERSION,
        "endpoints": [
            "GET  /ww/health",
            "GET  /ww/status",
            "POST /ww/run",
            "POST /ww/run/background",
            "POST /ww/autonomous/start",
            "POST /ww/autonomous/stop",
            "GET  /ww/history",
            "POST /ww/memory",
            "POST /ww/code",
            "POST /ww/gateway/send",
            "GET  /ww/gateway/list",
            "POST /ww/webhook/receive",
            "GET  /ww/tools",
            "GET  /ww/version",
            "GET  /ww/credentials/services",
            "POST /ww/credentials/get",
            "POST /ww/credentials/set",
            # Scheduler
            "POST /ww/scheduler/start",
            "POST /ww/scheduler/stop",
            "POST /ww/scheduler/add",
            "GET  /ww/scheduler/list",
            "POST /ww/scheduler/remove",
            # Skills
            "GET  /ww/skills/list",
            "POST /ww/skills/load",
            "POST /ww/skills/save",
            "POST /ww/skills/delete",
            # Config
            "GET  /ww/config/list",
            "POST /ww/config/get",
            "POST /ww/config/set",
            # Profile
            "POST /ww/profile/create",
            "GET  /ww/profile/list",
            "POST /ww/profile/activate",
            # Evolution
            "GET  /ww/evolution/metrics",
            "GET  /ww/evolution/history",
            "POST /ww/evolution/cycle",
            "GET  /ww/evolution/summary",
            "GET  /ww/evolution/self-review",
            "GET  /ww/evolution/goals",
        ],
    }


# ── Task Endpoints ──

@app.post("/ww/run")
def run(req: TaskRequest):
    return server.run_task(req.goal, req.max_spirals, req.model or "", req.provider or "",
                           req.image_path or "", req.reasoning_effort or "")


@app.post("/ww/run/background")
def run_bg(req: TaskRequest):
    return server.run_in_background(req.goal, req.max_spirals)


@app.get("/ww/history")
def history(limit: int = 10):
    return server.task_history(limit)


# ── Task Queue Endpoints ──

@app.get("/ww/tasks")
def list_tasks(status: str = "", limit: int = 20):
    """List async tasks with optional status filter."""
    return {
        "tasks": server.task_queue.list_tasks(
            status=status or None, limit=limit
        ),
        "active": server.task_queue.active_count,
        "total_tracked": server.task_queue.total_tracked,
    }


@app.get("/ww/task/{task_id}")
def task_status(task_id: str):
    """Get task status and result."""
    st = server.task_queue.status(task_id)
    if st is None:
        raise HTTPException(404, f"Task {task_id} not found")
    return st


@app.get("/ww/task/{task_id}/result")
def task_result(task_id: str):
    """Get task result (blocks until complete)."""
    res = server.task_queue.result(task_id)
    if res is None:
        raise HTTPException(404, f"Task {task_id} not found")
    return res


@app.get("/ww/model")
def model_info():
    """Return the active model configuration (self-detection, not hardcoded)."""
    import os as _os
    model = _os.environ.get("WW_MODEL", "deepseek/deepseek-v4-flash")
    provider = _os.environ.get("WW_PROVIDER", "deepseek")
    vision = _os.environ.get("AUXILIARY_VISION_MODEL", "not set")
    return {
        "model": model,
        "provider": provider,
        "vision_model": vision,
    }


# ── Autonomous Loop Endpoint ──

@app.post("/ww/autonomous/start")
def start_auto(req: AutonomousRequest):
    return server.start_autonomous(req.interval, req.max_spirals_per_cycle)


@app.post("/ww/autonomous/stop")
def stop_auto():
    return server.stop_autonomous()


# ── Memory Endpoint (Built-in MemorySystem) ──

@app.post("/ww/memory")
def memory_op(req: MemoryRequest):
    """Memory Operations (Backward Compatible: recall/search/snapshot/sleep/probe/store)"""
    if req.action == "store":
        mid = server.memory.store_text(
            content=req.content,
            entities=req.entities,
            source="api",
        )
        return {"memory_id": mid, "status": "stored"}
    elif req.action == "search":
        results = server.memory.search(req.query, limit=req.limit)
        return {"results": [a.to_dict() for a in results]}
    elif req.action == "snapshot":
        return server.memory.snapshot(limit=req.limit)
    elif req.action == "sleep":
        result = server.memory.consolidate()
        return {"consolidation": result}
    elif req.action == "probe":
        results = server.memory.recall_engine.probe_entity(req.query)
        return {"results": [a.to_dict() for a in results]}
    else:  # recall (default)
        results = server.memory.recall(req.query, limit=req.limit)
        return {"results": [a.to_dict() for a in results]}

@app.get("/ww/memory/stats")
def memory_stats():
    """Memory System Statistics"""
    return server.memory.get_stats()

@app.get("/ww/memory/recent")
def memory_recent(limit: int = 10):
    """Recent Memories"""
    results = server.memory.store.get_recent(limit)
    return {"results": [a.to_dict() for a in results], "count": len(results)}

@app.get("/ww/memory/top")
def memory_top(limit: int = 10):
    """Top Scoring Memories"""
    results = server.memory.store.get_top_scored(limit)
    return {"results": [a.to_dict() for a in results], "count": len(results)}

@app.get("/ww/memory/get/{memory_id}")
def memory_get(memory_id: str):
    """Get Single Memory"""
    atom = server.memory.store.get(memory_id)
    if atom is None:
        return {"error": "not_found"}
    return atom.to_dict()

@app.post("/ww/memory/recall")
def memory_recall_json(data: dict):
    """Recall (with Spreading Activation)"""
    query = data.get("query", "")
    limit = data.get("limit", 10)
    results = server.memory.recall(query, limit=limit)
    return {"results": [a.to_dict() for a in results], "count": len(results)}

@app.post("/ww/memory/diffuse")
def memory_diffuse(data: dict):
    """Spreading Activation"""
    seed_id = data.get("seed_id", "")
    max_hops = data.get("max_hops", 3)
    results = server.memory.recall_engine.diffuse(seed_id, max_hops)
    return {"results": [a.to_dict() for a in results]}

@app.get("/ww/memory/entity/{entity}")
def memory_entity(entity: str):
    """Retrieve Entity-Related Memories"""
    results = server.memory.recall_engine.probe_entity(entity)
    return {"entity": entity, "results": [a.to_dict() for a in results]}

@app.post("/ww/memory/sleep")
def memory_sleep():
    """Trigger Sleep Consolidation"""
    result = server.memory.consolidate()
    return {"consolidation": result}


# ── Code Execution Endpoint ──

@app.post("/ww/code")
def run_code(req: CodeRequest):
    sb = server._get_sandbox()
    result = sb.run_code(req.code, req.context)
    return result.to_dict() if hasattr(result, 'to_dict') else {"output": str(result)}


# ── Gateway Endpoint ──

@app.post("/ww/gateway/send")
def gateway_send(req: GatewayMessage):
    import subprocess
    try:
        payload = json.dumps({
            "channel": req.channel,
            "message": req.message,
            "user": req.user,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        mqtt_host = os.environ.get("WW_MQTT_HOST", "localhost")
        result = subprocess.run(
            ["mosquitto_pub", "-h", mqtt_host,
             "-t", f"ww/{req.platform}", "-m", payload],
            capture_output=True, text=True, timeout=5,
        )
        return {"success": result.returncode == 0}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Telegram Endpoint (Direct Bot API, No MQTT) ──

@app.post("/ww/telegram/send")
def telegram_send(req: TelegramMessage):
    """Send Text Message to Telegram (Direct Bot API)."""
    from tools.telegram import TelegramPublisher
    pub = TelegramPublisher()
    if not pub.is_configured():
        raise HTTPException(503, "TELEGRAM_WW_TOKEN not configured")
    result = pub.send_message(req.chat_id, req.message, req.parse_mode, req.disable_preview)
    return {"success": result.get("ok", False), **result.get("result", {})}


@app.post("/ww/telegram/photo")
def telegram_photo(req: TelegramPhoto):
    """Send Image to Telegram."""
    from tools.telegram import TelegramPublisher
    pub = TelegramPublisher()
    if not pub.is_configured():
        raise HTTPException(503, "TELEGRAM_WW_TOKEN not configured")
    result = pub.send_photo(req.chat_id, req.photo_url, req.caption, req.parse_mode)
    return {"success": result.get("ok", False), **result.get("result", {})}


@app.post("/ww/telegram/file")
def telegram_file(req: TelegramFile):
    """Upload Local File to Telegram."""
    from tools.telegram import TelegramPublisher
    pub = TelegramPublisher()
    if not pub.is_configured():
        raise HTTPException(503, "TELEGRAM_WW_TOKEN not configured")
    result = pub.send_file(req.chat_id, req.file_path, req.caption)
    return {"success": result.get("ok", False), **result.get("result", {})}


@app.get("/ww/telegram/verify")
def telegram_verify():
    """Verify Telegram Bot Connection Status."""
    from tools.telegram import TelegramPublisher
    pub = TelegramPublisher()
    if not pub.is_configured():
        raise HTTPException(503, "TELEGRAM_WW_TOKEN not configured")
    result = pub.verify()
    return result




@app.get("/ww/gateway/list")
def gateway_list():
    """List all registered gateways"""
    return {"gateways": server.gateway.list_gateways()}


# --- tools endpoint ---

@app.get("/ww/tools")
def tools_list():
    """List all registered tools."""
    if not server.ww:
        return {"tools": []}
    tools_list = []
    for name, tool in server.ww.tools._tools.items():
        tools_list.append({
            "name": name,
            "description": getattr(tool, "description", "")[:100],
            "category": getattr(tool, "category", "general"),
        })
    return {"tools": tools_list, "count": len(tools_list)}


# --- version endpoint ---

@app.get("/ww/version")
def version():
    return {
        "version": WW_VERSION,
        "features": [
            "Spiral cognition loop",
            "Multi-provider LLM transport",
            "Memory v2",
            "Context compression",
            "Subagent delegation",
            "CLI",
            "Multi-platform gateway",
            "Self-evolution engine",
            "Code self-healing",
            "Scheduler",
            "Subconscious v1/v2/v3",
        ],
    }

# ── Webhook Receive Endpoint ──

@app.post("/ww/webhook/receive")
async def webhook_receive(request: Request):
    """Receive incoming webhook"""
    try:
        payload = await request.json()
        headers = dict(request.headers)
        from gateway.adapters.webhook import WebhookAdapter
        gw = WebhookAdapter()
        result = gw.receive(payload, headers)
        if result is not None:
            return {"status": "received", "message": str(result)[:200]}
        raise HTTPException(403, "Webhook rejected")
    except Exception as e:
        raise HTTPException(400, str(e))


# ── Credential Endpoint ──

@app.get("/ww/credentials/services")
def credential_services():
    """List credential services"""
    from core.credentials import get_credential_store
    store = get_credential_store()
    return {"services": store.list_services(), "count": len(store.list_services())}


@app.post("/ww/credentials/get")
def credential_get(service: str = "", key: str = ""):
    """Get a credential"""
    from core.credentials import get_credential_store, mask_secret
    store = get_credential_store()
    val = store.get(service, key)
    if val:
        return {"found": True, "value": mask_secret(val)}
    return {"found": False, "value": ""}


@app.post("/ww/credentials/set")
def credential_set(service: str = "", key: str = "", value: str = ""):
    """Store a credential"""
    from core.credentials import get_credential_store
    store = get_credential_store()
    store.set(service, key, value)
    return {"success": True}

# ── Scheduler Endpoint ──

@app.post("/ww/scheduler/start")
def scheduler_start():
    return server.start_scheduler()


@app.post("/ww/scheduler/stop")
def scheduler_stop():
    return server.stop_scheduler()


@app.post("/ww/scheduler/add")
def scheduler_add(req: ScheduleRequest):
    task = server.scheduler.add(
        name=req.name or "task-" + datetime.now(timezone.utc).strftime("%H%M%S"),
        goal=req.goal,
        schedule=req.schedule,
        max_spirals=req.max_spirals,
    )
    return task.to_dict()


@app.get("/ww/scheduler/list")
def scheduler_list():
    return {"tasks": server.scheduler.list()}


@app.post("/ww/scheduler/remove")
def scheduler_remove(task_id: str):
    ok = server.scheduler.remove(task_id)
    return {"success": ok}


# ── Skills Endpoint ──

@app.get("/ww/skills/list")
def skills_list():
    return {"skills": server.skills.list()}


@app.post("/ww/skills/load")
def skills_load(name: str):
    skill = server.skills.load(name)
    if not skill:
        raise HTTPException(404, "skill not found")
    return skill.to_dict()


@app.post("/ww/skills/save")
def skills_save(req: SkillRequest):
    from tools.skill_manager import Skill
    skill = Skill(
        name=req.name,
        description=req.description,
        category=req.category,
        steps=req.steps,
        pitfalls=req.pitfalls,
        body=req.body,
    )
    ok = server.skills.save(skill)
    return {"success": ok, "name": req.name}


@app.post("/ww/skills/delete")
def skills_delete(name: str):
    ok = server.skills.delete(name)
    return {"success": ok}


# ── Configuration Endpoint ──

@app.get("/ww/config/list")
def config_list():
    return {"config": server.config.list()}


@app.post("/ww/config/get")
def config_get(key: str):
    val = server.config.get(key)
    if val is None:
        raise HTTPException(404, "key not found")
    return {"key": key, "value": val}


@app.post("/ww/config/set")
def config_set(req: ConfigRequest):
    server.config.set(req.key, req.value)
    return {"success": True, "key": req.key, "value": req.value}


# ── Profile Endpoint ──

@app.get("/ww/profile/list")
def profile_list():
    return {"profiles": server.config.profile_list(), "active": server.config.active_profile()}


@app.post("/ww/profile/create")
def profile_create(name: str, data: str = "{}"):
    profile_data = json.loads(data) if isinstance(data, str) else data
    ok = server.config.profile_set(name, profile_data)
    return {"success": ok, "name": name}


@app.post("/ww/profile/activate")
def profile_activate(name: str):
    ok = server.config.profile_activate(name)
    if ok:
        server._init_ww()
    return {"success": ok, "active_profile": name}


# ── Self-Evolution Endpoint ──

@app.get("/ww/evolution/metrics")
def evolution_metrics():
    """Get Performance Metrics Summary."""
    if not server.ww:
        return {"metrics": {"total_tasks": 0}}
    return {"metrics": server.ww.metrics.summary()}


@app.get("/ww/evolution/history")
def evolution_history(limit: int = 10):
    """Get Evolution History."""
    if not server.ww:
        return {"history": []}
    return {"history": server.ww.evolution.get_history(limit)}


@app.post("/ww/evolution/cycle")
def evolution_cycle():
    """Manually Trigger a Full Evolution Cycle."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    result = server.ww.evolution.full_cycle()
    return result


@app.get("/ww/evolution/summary")
def evolution_summary():
    """Get Human-Readable Evolution Summary."""
    if not server.ww:
        return {"summary": "WW not initialized"}
    return {"summary": server.ww.evolution.get_evolution_summary()}


@app.get("/ww/evolution/self-review")
def evolution_self_review():
    """WW Self-Review Code, Find Improvement Points."""
    code_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    result = server.ww.evolution.auditor.self_review(code_dir) if server.ww else "WW not initialized"
    return {"review": result}


@app.get("/ww/evolution/goals")
def evolution_goals():
    """Generate Improvement Goals Based on Audit Results."""
    if not server.ww:
        return {"goals": []}
    return {"goals": server.ww.evolution.generate_improvement_goals()}


# ── Mascot Endpoint ──
import asyncio
import os
_MASCOT_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core", "mascot", "mascot.html")

@app.get("/ww/mascot")
def mascot_page():
    """Fat Shark Mascot HTML Page."""
    if not os.path.exists(_MASCOT_HTML_PATH):
        return HTMLResponse("<h1>Mascot not found</h1>", status_code=404)
    with open(_MASCOT_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html)

@app.get("/ww/mascot/events")
async def mascot_events(request: Request):
    """SSE Real-Time Status Stream."""
    from core.mascot import mascot

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        def on_state(data: dict):
            try:
                fut = asyncio.run_coroutine_threadsafe(queue.put(data), asyncio.get_event_loop())
                fut.result(timeout=1)
            except Exception:
                pass

        mascot.subscribe(on_state)
        try:
            # Send Current Status
            initial = mascot.get_state()
            yield f"data: {json.dumps(initial)}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    if data is None:
                        break
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            mascot.unsubscribe(on_state)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )

@app.get("/ww/mascot/state")
def mascot_get_state():
    """Read Current Mascot Status."""
    from core.mascot import mascot
    return mascot.get_state()

@app.post("/ww/mascot/state")
def mascot_set_state(req: dict):
    """Manually Set Mascot Status."""
    from core.mascot import mascot
    state = req.get("state", "idle")
    message = req.get("message")
    mascot.set_state(state, message)
    return {"status": "ok", "state": mascot.get_state()}


# ── Logging Endpoint ──

@app.get("/ww/logs")
def get_logs(level: str = "", source: str = "",
             session_id: str = "", limit: int = 50):
    """Query WW Structured Logs."""
    from core.logger import get_logger
    log = get_logger()
    # Reload File to Ensure Sync with Background Task
    try:
        log._load()
    except Exception:
        pass
    entries = log.query(level=level, source=source,
                         session_id=session_id, limit=min(limit, 200))
    return {"entries": entries, "count": len(entries)}


@app.get("/ww/logs/summary")
def log_summary():
    """Log Quick Overview."""
    from core.logger import get_logger
    log = get_logger()
    try:
        log._load()
    except Exception:
        pass
    return log.summary()


# ── Checkpoint / Resume Endpoint ──

@app.get("/ww/sessions")
def list_sessions(status: str = "", limit: int = 20):
    """List All Sessions (Supports Status Filtering)."""
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    sessions = db.list_sessions(limit=limit, status=status)
    return {"sessions": sessions, "count": len(sessions)}

@app.get("/ww/sessions/{session_id}")
def get_session(session_id: str):
    """Get Session Details."""
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    s = db.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session": s}

@app.get("/ww/sessions/{session_id}/checkpoints")
def list_checkpoints(session_id: str, limit: int = 50):
    """List All Checkpoints of a Session."""
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    cps = db.get_checkpoints(session_id, limit=limit)
    return {"checkpoints": cps, "count": len(cps)}

@app.get("/ww/checkpoint/{cp_id}")
def get_checkpoint(cp_id: str):
    """Get Specific Checkpoint."""
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    cp = db.get_checkpoint(cp_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    return {"checkpoint": cp}

@app.get("/ww/resume/{session_id}")
def get_resume_point(session_id: str):
    """Get Session Last Interruption Point (for Resume)."""
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    cp = db.get_last_interrupted(session_id)
    if not cp:
        return {"resumable": False, "message": "No interrupted checkpoint found"}
    return {"resumable": True, "checkpoint": cp}

# Async Suspended Approval Endpoint (Called After User Replies via Gateway)
_SUSPEND_TIMEOUT_SECONDS = 86400  # 24 Hours

@app.post("/ww/approve/{checkpoint_id}")
def approve_suspended(checkpoint_id: str):
    """Approve a Suspended Tool Call (Async HITL).

    Time check: Suspension exceeded 24 Hourly automaticreject, avoid execution errors due to environment changes. 
    Return validate_first=True ask caller to verify environment first Hook (Look Phase) . 
    """
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    cp = db.get_checkpoint(checkpoint_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    if not cp.get("is_interrupted", False):
        raise HTTPException(status_code=400, detail="Checkpoint is not interrupted")

    # Timeout Check
    now = time.time()
    suspended_at = cp.get("timestamp", 0)
    elapsed = now - suspended_at
    if elapsed > _SUSPEND_TIMEOUT_SECONDS:
        # auto-reject and archive
        db.save_checkpoint(
            session_id=cp["session_id"],
            spiral_number=cp.get("spiral_number", 0),
            phase=cp.get("phase", ""),
            scratchpad=cp.get("scratchpad", "") + f"\n[AUTO-DENIED] Suspension exceeded {elapsed/3600:.1f}h (Limit {_SUSPEND_TIMEOUT_SECONDS/3600}h) ",
            interrupted=False,
            resume_data={"approved": False, "auto_denied": True,
                         "reason": f"suspended {elapsed/3600:.1f}h exceeds timeout",
                         "rejected_at": now},
        )
        return {"status": "timeout_denied", "checkpoint_id": checkpoint_id,
                "message": f"Suspend {elapsed/3600:.1f}h Exceed {_SUSPEND_TIMEOUT_SECONDS/3600}h Limit, autoreject"}

    # mark that environment verification is needed (validate_first=True Promptcallerfirst  Look then  Act) 
    db.mark_resolved(checkpoint_id)
    db.save_checkpoint(
        session_id=cp["session_id"],
        spiral_number=cp["spiral_number"],
        phase=cp["phase"],
        scratchpad=cp.get("scratchpad", "") + "\n[APPROVED]",
        context_snapshot=cp.get("context_snapshot", {}),
        interrupted=False,
        resume_data={"approved": True, "approved_at": now,
                     "validate_first": True},  # ask caller to verify environment first Hook
    )
    return {"status": "approved", "checkpoint_id": checkpoint_id,
            "validate_first": True,
            "message": "approvalSuccess, suggest running environment validation first Hook (Look Phase) confirm context is valid"}

@app.post("/ww/reject/{checkpoint_id}")
def reject_suspended(checkpoint_id: str):
    """reject a suspended tool call. """
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    cp = db.get_checkpoint(checkpoint_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    db.save_checkpoint(
        session_id=cp["session_id"],
        spiral_number=cp["spiral_number"],
        phase=cp["phase"],
        scratchpad=cp.get("scratchpad", "") + "\n[REJECTED]",
        interrupted=False,
        resume_data={"approved": False, "rejected_at": __import__("time").time()},
    )
    return {"status": "rejected", "checkpoint_id": checkpoint_id}

@app.delete("/ww/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete session  and All checkpoint. """
    from core.checkpoint import CheckpointDB
    db = CheckpointDB()
    db.delete_session(session_id)
    return {"status": "deleted"}


# ── Dashboard SSE Endpoint ──

_sse_clients = set()
_sse_last_state = {}

@app.get("/ww/dashboard/stream")
async def dashboard_sse(request: Request):
    """Server-Sent Events stream  — Replace 3 Second polling. 
    
    Only when LEARN Spiral phase change、Tool invoked、or only push when memory system updates. 
    """
    from fastapi.responses import StreamingResponse
    import asyncio

    async def event_stream():
        client_id = id(asyncio.current_task())
        _sse_clients.add(client_id)
        try:
            # push full state immediately
            yield f"data: {json.dumps(_build_dashboard_snapshot())}\n\n"
            idle_cycles = 0
            while True:
                await asyncio.sleep(1.5)
                
                # Zombie Connection Detect
                try:
                    if await request.is_disconnected():
                        break
                except Exception:
                    pass  # non- asgi Environment (As tested) May not be supported
                
                new_state = _build_dashboard_snapshot()
                if new_state != _sse_last_state:
                    _sse_last_state = new_state
                    yield f"data: {json.dumps(new_state)}\n\n"
                    idle_cycles = 0
                elif int(time.time()) % 30 == 0:
                    yield ": keepalive\n\n"
                    idle_cycles += 1
                
                # Safety guardrail: Exceed 600  times idle (~15Minute) No change → Disconnect
                if idle_cycles > 600:
                    logger.debug("SSE client %s: idle timeout, disconnecting", client_id)
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            _sse_clients.discard(client_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _build_dashboard_snapshot() -> dict:
    """Create Dashboard Snapshot (Lightweight version /ww/status   SSE Version) . """
    try:
        status = server.status()
        ww = server.ww
        result = {
            "phase": ww.state.current_phase if ww else "idle",
            "spiral": ww.state.current_spiral if ww else 0,
            "running": ww.running if ww else False,
            "steps_completed": getattr(ww, "_steps_completed", 0),
            "steps_total": getattr(ww, "_steps_total", 0),
            "tool_count": len(ww.tools.tool_names()) if ww else 0,
            "hostname": os.uname().nodename,
        }
        # Memory SystemSummary
        if ww and ww.memory:
            try:
                stats = ww.memory.statistics() if hasattr(ww.memory, "statistics") else ww.memory.stats()
                result["hippocampus_used"] = stats.get("hippocampus_count", stats.get("atoms", 0))
                result["hippocampus_capacity"] = stats.get("hippocampus_capacity", stats.get("capacity", 100))
            except Exception:
                pass
        result["ts"] = time.time()
        return result
    except Exception:
        return {"phase": "error", "spiral": 0, "running": False, "ts": time.time()}


# ═══════════════════════════════════════════════════════════════════
# New Routes — WebUI, MCP, Hooks, Commands, Streaming, Voice, ACP
# ═══════════════════════════════════════════════════════════════════

# ── WebUI ──

@app.get("/ww/webui/")
@app.get("/ww/webui/index.html")
def webui_index():
    """Worldwave WebChat UI."""
    webui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "index.html")
    if not os.path.exists(webui_path):
        return HTMLResponse("<h1>WebUI not found. Run: mkdir -p webui</h1>", status_code=404)
    with open(webui_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/ww/webui/manifest.json")
def webui_manifest():
    """PWA Manifest."""
    manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "manifest.json")
    if not os.path.exists(manifest_path):
        raise HTTPException(404, "Manifest not found")
    with open(manifest_path) as f:
        return json.loads(f.read())


@app.get("/ww/webui/src/app.js")
def webui_app_js():
    """WebUI client JavaScript."""
    js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "src", "app.js")
    if not os.path.exists(js_path):
        raise HTTPException(404, "app.js not found")
    with open(js_path) as f:
        return Response(content=f.read(), media_type="application/javascript")

# ── MCP Endpoints ──

@app.get("/ww/mcp/servers")
def mcp_list_servers():
    """List configured MCP servers."""
    from core.mcp import get_mcp_manager
    mgr = get_mcp_manager()
    return {"servers": list(mgr._servers.keys()), "tools_count": len(mgr.tools)}

@app.post("/ww/mcp/connect")
def mcp_connect(req: dict):
    """Connect to an MCP server."""
    from core.mcp import get_mcp_manager, MCPServerConfig
    mgr = get_mcp_manager()
    config = MCPServerConfig(
        name=req.get("name", "unnamed"),
        transport=req.get("transport", "stdio"),
        command=req.get("command"),
        args=req.get("args", []),
        url=req.get("url"),
        api_key=req.get("api_key"),
    )
    mgr.add_server(config)
    import asyncio

    async def _connect_and_register():
        ok = await mgr.connect_server(config.name)
        if ok:
            # Re-sync MCP tools into the running agent's ToolRegistry
            try:
                from core.mcp import register_mcp_tools
                ww = server.ww
                if ww and hasattr(ww, 'tools'):
                    count = register_mcp_tools(ww.tools, mgr)
                    logger.info("MCP bridge: %d new tools from %s", count, config.name)
            except Exception as e:
                logger.warning("MCP bridge re-sync failed: %s", e)

    asyncio.create_task(_connect_and_register())
    return {"status": "connecting", "server": config.name}

@app.post("/ww/mcp/call")
async def mcp_call_tool(req: dict):
    """Call a tool on an MCP server."""
    from core.mcp import get_mcp_manager
    mgr = get_mcp_manager()
    result = await mgr.call_tool(
        server_name=req.get("server", ""),
        tool_name=req.get("tool", ""),
        arguments=req.get("arguments", {}),
    )
    return {"result": result}

@app.get("/ww/mcp/tools")
def mcp_list_tools(query: str = ""):
    """List/search MCP tools (MCP Tool Search)."""
    from core.mcp import get_mcp_manager
    mgr = get_mcp_manager()
    if query:
        tools = mgr.search_tools(query)
    else:
        tools = list(mgr.tools.values())
    return {"tools": [{"name": t.name, "description": t.description, "server": t.server_name} for t in tools]}

# ── Hooks Endpoints ──

@app.get("/ww/hooks")
def hooks_list():
    """List registered hooks."""
    from core.hooks import get_hook_registry
    reg = get_hook_registry()
    result = {}
    for event in reg._hooks:
        py_hooks = len(reg._hooks[event])
        script_hooks = len(reg._script_hooks[event])
        if py_hooks + script_hooks > 0:
            result[event.value] = {"python": py_hooks, "scripts": script_hooks}
    return {"hooks": result, "enabled": reg.enabled}

@app.post("/ww/hooks/enable")
def hooks_enable(req: dict):
    """Enable/disable the hooks system."""
    from core.hooks import get_hook_registry
    reg = get_hook_registry()
    reg.enabled = req.get("enabled", True)
    return {"enabled": reg.enabled}

@app.post("/ww/hooks/load")
def hooks_load_dir(req: dict):
    """Load hooks from a directory."""
    from core.hooks import get_hook_registry
    reg = get_hook_registry()
    directory = req.get("directory", "")
    if directory:
        reg.load_from_directory(directory)
    return {"status": "loaded"}

# ── Slash Commands Endpoint ──

@app.get("/ww/commands")
def commands_list():
    """List all slash commands."""
    from core.commands import get_command_registry
    reg = get_command_registry()
    cmds = reg.list_all()
    return {"commands": [{"name": c.name, "description": c.description, "aliases": c.aliases} for c in cmds]}

@app.post("/ww/commands/execute")
async def commands_execute(req: dict):
    """Execute a slash command."""
    from core.commands import get_command_registry
    reg = get_command_registry()
    name = req.get("command", "").lstrip("/")
    args = req.get("args", "")
    context = req.get("context", {})
    result = await reg.execute(name, args, context)
    return {"result": result}

# ── Streaming Endpoints ──

@app.post("/ww/stream")
async def stream_response(req: dict):
    """Stream a response using block streaming (phase-level)."""
    from core.streaming import BlockStreamer, StreamConfig, sse_event_stream
    config = StreamConfig(enabled=True)
    streamer = BlockStreamer(config)

    async def event_generator():
        import asyncio as aio
        goal = req.get("goal", "")
        if goal and server.ww:
            task = aio.create_task(_run_and_stream(server.ww, goal, streamer))
        async for sse_event in sse_event_stream(streamer):
            yield sse_event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "Access-Control-Allow-Origin": "*"},
    )


@app.post("/ww/stream/tokens")
async def stream_tokens(req: dict):
    """Token-level SSE streaming — sends each token as it is generated.

    POST body: {"goal": "..."}
    SSE events: {"token": "chunk"}, {"tool_call": {...}}, {"done": true}
    """
    goal = req.get("goal", "")
    if not goal or not server.ww:
        return StreamingResponse(
            _single_event({"error": "No goal or WW not initialized"}),
            media_type="text/event-stream",
        )

    async def _token_stream():
        ww = server.ww
        # Run through the spiral loop, intercepting LLM calls
        try:
            yield _sse({"status": "started", "goal": goal[:200]})

            # Use the existing spiral loop but with streaming LLM
            # For now, stream the Act phase — the most visible phase
            perception = ww._llm_perceive(goal)
            yield _sse({"phase": "perceive", "done": True})

            recall = ww._llm_recall(perception, goal)
            yield _sse({"phase": "recall", "done": True})

            plan = ww._llm_plan(perception, recall, goal)
            yield _sse({"phase": "plan", "done": True})

            # Stream the Act phase token-by-token
            yield _sse({"phase": "act", "streaming": True})
            full_content = ""
            try:
                for chunk, finish in ww.llm.chat_stream(
                    messages=ww.conversation.get_messages(),
                    phase="act",
                ):
                    if chunk:
                        full_content += chunk
                        yield _sse({"token": chunk})
                    if finish:
                        yield _sse({"finish": finish})
            except RuntimeError:
                # Fall back to non-streaming
                resp = ww.llm.chat(
                    messages=ww.conversation.get_messages(),
                    phase="act",
                )
                full_content = resp
                yield _sse({"token": resp})

            # Parse and execute tool calls from streamed content
            ww.conversation.add_assistant(full_content)

            # Evaluate
            evaluation = ww._llm_evaluate(plan, [], goal)
            yield _sse({"phase": "evaluate", "done": True})

            # Learn
            learn_result = ww._llm_learn(ww.state.current_spiral, goal)
            yield _sse({"phase": "learn", "done": True})

            yield _sse({"done": True, "status": "completed"})

        except Exception as e:
            yield _sse({"error": str(e), "done": True})

    return StreamingResponse(
        _token_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "Access-Control-Allow-Origin": "*"},
    )


def _sse(data: dict) -> str:
    """Format a dict as an SSE event."""
    return f"data: {json.dumps(data)}\n\n"


async def _single_event(data: dict):
    """Yield a single SSE event."""
    yield _sse(data)

async def _run_and_stream(ww, goal: str, streamer):
    """Run a task and pipe output through the streamer."""
    try:
        await streamer.emit_text(f"Processing: {goal}")
        result = ww.run(goal)
        await streamer.emit_text(str(result))
    except Exception as e:
        await streamer.emit_error(str(e))
    finally:
        await streamer.emit_done()

# ── Voice / STT / TTS Endpoints ──

@app.get("/ww/voice/config")
def voice_config():
    """Get voice configuration."""
    from core.voice import get_voice_engine
    engine = get_voice_engine()
    return {
        "stt_provider": engine.config.stt_provider.value,
        "tts_provider": engine.config.tts_provider.value,
        "stt_model": engine.config.stt_model,
        "tts_voice": engine.config.tts_voice,
        "enabled": engine.config.enabled,
    }

@app.post("/ww/voice/tts")
async def voice_tts(req: dict):
    """Convert text to speech, return audio."""
    from core.voice import get_voice_engine
    engine = get_voice_engine()
    text = req.get("text", "")
    if not text:
        raise HTTPException(400, "text required")
    audio = await engine.synthesize(text)
    if not audio:
        raise HTTPException(500, "TTS failed")
    from fastapi.responses import Response
    return Response(content=audio, media_type="audio/mpeg")

@app.post("/ww/voice/stt")
async def voice_stt(req: Request):
    """Transcribe audio to text."""
    from core.voice import get_voice_engine
    engine = get_voice_engine()
    audio_data = await req.body()
    if not audio_data:
        raise HTTPException(400, "audio data required")
    text = await engine.transcribe(audio_data)
    return {"text": text}

# ── ACP Endpoint ──

@app.get("/ww/acp/status")
def acp_status():
    """ACP server status."""
    from core.acp import get_acp_server
    server = get_acp_server()
    return {"running": server._running, "capabilities": len(server._capabilities)}

# ── Credential Pools Endpoints ──

@app.get("/ww/credentials/pools")
def cred_pools_list():
    """List all credential pools with health."""
    mgr = get_credential_manager_v2()
    return mgr.health_report()

@app.post("/ww/credentials/pools/add")
def cred_pool_add(req: dict):
    """Add a key to a credential pool."""
    mgr = get_credential_manager_v2()
    provider = req.get("provider", "")
    key = req.get("key", "")
    label = req.get("label", "")
    if not provider or not key:
        raise HTTPException(400, "provider and key required")
    mgr.add_key(provider, key, label=label)
    mgr.save()
    return {"status": "added", "provider": provider}

@app.post("/ww/credentials/pools/reset")
def cred_pool_reset(req: dict):
    """Reset all keys in a pool."""
    mgr = get_credential_manager_v2()
    provider = req.get("provider", "")
    pool = mgr._pools.get(provider)
    if pool:
        pool.reset_all()
        mgr.save()
        return {"status": "reset", "provider": provider}
    raise HTTPException(404, "Pool not found")

# ── Plugin Management Endpoints ──

@app.get("/ww/plugins")
def plugins_list():
    """List all plugins."""
    from core.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    discovered = mgr.discover()
    loaded = mgr.list_plugins()
    return {"discovered": discovered, "loaded": loaded}

@app.post("/ww/plugins/load")
def plugins_load(req: dict):
    """Load a plugin."""
    from core.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    name = req.get("name", "")
    if not name:
        raise HTTPException(400, "name required")
    plugin = mgr.load(name)
    if plugin:
        return {"status": plugin.status.value, "name": name}
    raise HTTPException(404, f"Plugin not found: {name}")

@app.post("/ww/plugins/enable")
def plugins_enable(req: dict):
    """Enable a plugin."""
    from core.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    name = req.get("name", "")
    if mgr.enable(name):
        return {"status": "enabled", "name": name}
    raise HTTPException(400, f"Failed to enable: {name}")

@app.post("/ww/plugins/disable")
def plugins_disable(req: dict):
    """Disable a plugin."""
    from core.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    name = req.get("name", "")
    if mgr.disable(name):
        return {"status": "disabled", "name": name}
    raise HTTPException(400, f"Failed to disable: {name}")

@app.post("/ww/plugins/install")
def plugins_install(req: dict):
    """Install a plugin from URL/path/pip."""
    from core.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    source = req.get("source", "")
    name = req.get("name")
    if mgr.install(source, name):
        return {"status": "installed"}
    raise HTTPException(400, "Install failed")


# ── Start ──

@app.on_event("startup")
def startup():
    """scheduler auto-starts after server boot + register scheduled evolution task."""
    import threading

    # Propagate main LLM model info for Computer Use vision auto-detection
    cfg = ConfigManager()
    model = cfg.get("model") or os.environ.get("MODEL", "")
    provider = cfg.get("provider") or os.environ.get("PROVIDER", "")
    if model:
        os.environ.setdefault("WW_MAIN_MODEL", model)
    if provider:
        os.environ.setdefault("WW_MAIN_PROVIDER", provider)

    def _start_scheduler():
        # Start gateway (Background thread) 
        if server.gateway:
            try:
                server.gateway.start_all()
            except Exception as e:
                logger.warning("Gateway start failed: %s", e)

        # Start Agent gRPC server for Wavegate communication
        agent_grpc_port = int(os.environ.get("WW_AGENT_GRPC_PORT", "0"))
        try:
            from core.agent_grpc import serve_agent  # lazy import (grpcio optional)
            server.agent_grpc_server, server.agent_grpc_service = serve_agent(
                ww=server.ww, port=agent_grpc_port,
            )
            server.agent_grpc_service.set_ww(server.ww)
            logger.info("Agent gRPC server started on port %d", agent_grpc_port)
        except Exception as e:
            logger.warning("Agent gRPC server start failed: %s", e)

        # Start contacts (LAN discovery + background services)
        try:
            server.contacts.start()
            logger.info("Contacts module started")
        except Exception as e:
            logger.warning(f"Contacts start failed: {e}")

        time.sleep(3)  # etc WW Fully initialized
        try:
            if not server.ww:
                return
            # startscheduler
            result = server.start_scheduler()
            # Skip auto-evolution if env var set (for testing/debugging)
            if os.environ.get("WW_SKIP_AUTO_EVOLUTION"):
                logger.info("Auto-evolution skipped (WW_SKIP_AUTO_EVOLUTION set)")
                return
            # check if already exists auto-evolution Task
            existing = server.ww.scheduler.list()
            has_evo = any(t.get("name") == "auto-evolution" for t in existing)
            if not has_evo:
                server.ww.scheduler.add(
                    name="auto-evolution",
                    goal="Self-audit and evolution: check system state, code issues, execute evolution cycle. Auto-fix improvement points when found.",
                    schedule="0 * * * *",
                )
        except Exception as e:
            logger.warning(f"Auto-scheduler init failed: {e}")

    threading.Thread(target=_start_scheduler, daemon=True).start()

    # Start Mascot
    try:
        from core.mascot import mascot as mascot_instance
        mascot_instance.start()
        logger.info("Mascot fat shark mascot started")
    except Exception as e:
        logger.warning(f"Mascot init failed: {e}")

    # Windows: auto-start system tray
    def _launch_tray():
        time.sleep(5)
        try:
            import subprocess
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "core", "mascot", "launcher.ps1")
            if not os.path.exists(script):
                return

            if os.name == "nt":
                # Native Windows — launch PowerShell directly
                subprocess.Popen([
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-WindowStyle", "Hidden", "-File", script, "-Tray",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # WSL — use /mnt/c/ path
                ps = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                if not os.path.exists(ps):
                    return
                subprocess.Popen([
                    ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", script, "-Tray",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("WW tray launched")
        except Exception:
            pass

    threading.Thread(target=_launch_tray, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("WW_PORT", 9300))
    logger.info(f" Worldwave v0.3 API @ http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
