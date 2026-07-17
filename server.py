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
import hmac
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List


def _env_truthy(name: str, default: bool = False) -> bool:
    """Parse common env bool forms. Empty/unset uses default.

    True: 1, true, yes, on (case-insensitive)
    False: 0, false, no, off, empty string when key is present as ''
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = str(raw).strip().lower()
    if val in ("",):
        return False
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    # Unknown non-empty value: treat as true (legacy) but log once via caller
    return True

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
from p2p.network import GlobalP2PNetwork
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
    # Optional channel identity for multi-platform continuity tests
    platform: Optional[str] = None  # http | terminal | telegram | ...
    user_id: Optional[str] = None
    chat_id: Optional[str] = None
    entity_id: Optional[str] = None

class AutonomousRequest(BaseModel):
    interval: int = Field(300, ge=30, le=86400)
    max_spirals_per_cycle: int = Field(3, ge=1, le=10)
    model: Optional[str] = None

class CodeRequest(BaseModel):
    code: str = Field(..., min_length=1)
    context: Optional[Dict] = None

class MemoryRequest(BaseModel):
    action: str = Field(
        "recall",
        pattern="^(recall|search|snapshot|sleep|probe|store|update|delete)$",
    )
    query: str = ""
    content: str = ""
    entities: List[str] = []
    limit: int = 10
    memory_id: str = ""
    confirm: str = ""
    # Optional cognitive entity scope for search/recall (Gate 0.2 isolation)
    entity_id: str = ""

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

        # ── Entity continuity (P0: Persistent Cognitive Entity) ──
        from core.entity_state import EntityStateManager
        from wavegate.identity import IdentityResolver
        self.entity_mgr = EntityStateManager(config=self.config)
        self.identity_resolver = IdentityResolver()

        # Autonomous Loop Control
        self._autonomous_thread: Optional[threading.Thread] = None
        self._autonomous_running = False
        self._last_result: Optional[Dict] = None

        # Serialize runs against the shared Worldwave instance (state/conversation/llm).
        # TaskQueue may have multiple workers; without this, concurrent Telegram/HTTP
        # requests race on phase state and model swaps.
        self._run_lock = threading.RLock()
        # Per-session queues: same chat is strictly ordered; different chats still
        # serialize on _run_lock because there is one shared engine.
        self._session_locks: Dict[str, threading.RLock] = {}
        self._session_locks_guard = threading.Lock()
        self._lock_waits = 0
        self._lock_runs = 0

        # History Record
        self._task_history: List[Dict] = []

        # Async Task Queue (proper tracking, not fire-and-forget)
        from core.task_queue import TaskQueue
        self.task_queue = TaskQueue(max_workers=3)

        # Contacts module (distributed multi-agent address book)
        self.contacts = ContactManager()

        # P2P network (global gossip/blockchain layer)
        self.p2p: Optional[GlobalP2PNetwork] = None

        # Credentials manager (encrypted API key/secret storage)
        self.credentials = CredentialStore()

        # Initialization
        self._init_ww()
        self._scheduler_goal = None  # Used for scheduler callback

        # Telegram gateway (built-in Worldwave, not external patch).
        # Adapters are REGISTERED only here; started once in FastAPI startup.
        try:
            from gateway import GatewayManager
            self.gateway = GatewayManager()
            self._init_gateway()
            logger.info("Gateway adapters registered (start deferred to startup)")
        except Exception as e:
            logger.warning("Gateway init skipped: %s", e)
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
            entity_state_mgr=self.entity_mgr,
            identity_resolver=self.identity_resolver,
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
        """Register gateway adapters (do not start pollers yet)."""
        from gateway.bridge import TelegramGateway  # lazy import for scope
        token = (os.environ.get("TELEGRAM_WW_TOKEN") or "").strip()
        workspace_raw = (os.environ.get("TELEGRAM_WW_WORKSPACE") or "").strip()
        # Token alone is enough for DM mode. Workspace only filters group chats.
        if token:
            workspace_id = None
            if workspace_raw:
                try:
                    workspace_id = int(workspace_raw)
                except ValueError:
                    logger.warning(
                        "Invalid TELEGRAM_WW_WORKSPACE=%r — ignoring (DM mode only)",
                        workspace_raw,
                    )
            try:
                tg = TelegramGateway(
                    token=token,
                    workspace_id=workspace_id,
                    poll_interval=2.0,
                    task_handler=self._gateway_task_handler,
                )
                # start=False: FastAPI startup calls start_all() once
                self.gateway.register(tg, start=False)
                if workspace_id is not None:
                    logger.info(
                        "Telegram gateway registered (DM + group workspace=%s)",
                        workspace_id,
                    )
                else:
                    logger.info(
                        "Telegram gateway registered (DM-only mode; "
                        "set TELEGRAM_WW_WORKSPACE to also accept that group)"
                    )
            except Exception as e:
                logger.warning("Telegram gateway init failed: %s", e)
        else:
            logger.info(
                "Telegram gateway not configured "
                "(set TELEGRAM_WW_TOKEN for DMs; TELEGRAM_WW_WORKSPACE optional for groups)"
            )

        # Discord gateway (deprecated — use gateway/adapters/ instead)
        discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if discord_token:
            try:
                from gateway.adapters.discord import DiscordAdapter
                dg = DiscordAdapter(
                    token=discord_token,
                    task_handler=self._gateway_task_handler,
                )
                self.gateway.register(dg, start=False)
                logger.info("Discord gateway registered")
            except ImportError:
                logger.info("Discord adapter not yet available (gateway/adapters/discord.py)")
            except Exception as e:
                logger.warning("Discord gateway init failed: %s", e)

        # Webhook gateway (optional)
        try:
            from gateway.adapters.webhook import WebhookAdapter
            wh = WebhookAdapter(on_message=self._gateway_task_handler)
            self.gateway.register(wh, start=False)
            logger.info("Webhook gateway registered")
        except ImportError:
            logger.info("Webhook adapter not yet available (gateway/adapters/webhook.py)")
        except Exception as e:
            logger.warning("Webhook gateway init failed: %s", e)

    # ── P2P network helpers ──

    def _load_consent(self) -> dict:
        """Load user consent from ~/.worldwave/consent.json."""
        consent_path = os.path.join(os.path.expanduser("~"), ".worldwave", "consent.json")
        try:
            with open(consent_path) as f:
                data = json.load(f)
                return data.get("consent", {})
        except Exception:
            return {}

    def _node_id(self) -> str:
        """Persistent node ID for this WW installation."""
        node_id_path = os.path.join(self.persist_dir, "node_id.txt")
        try:
            with open(node_id_path) as f:
                return f.read().strip()
        except Exception:
            import uuid
            nid = uuid.uuid4().hex[:12]
            with open(node_id_path, "w") as f:
                f.write(nid)
            return nid

    def _init_p2p(self) -> Optional[GlobalP2PNetwork]:
        """Return the primary P2P network instance.

        Prefer Subconscious P2P (has gossip, federation, DHT).
        Falls back to standalone P2P only if Subconscious is unavailable.
        """
        # Prefer Subconscious P2P (primary, has full feature set)
        try:
            if self.ww and self.ww.subconscious and self.ww.subconscious.p2p:
                logger.info("P2P: using Subconscious P2P (gossip+federation+DHT)")
                return self.ww.subconscious.p2p
        except Exception:
            pass

        # Fallback: standalone P2P (lightweight, no gossip)
        consent = self._load_consent()
        if not consent.get("p2p_network", False):
            logger.info("P2P network not started (no consent)")
            return None

        from p2p.network import GlobalP2PNetwork
        p2p_port = int(os.environ.get("WW_P2P_PORT", "19833"))
        dht_port = int(os.environ.get("WW_P2P_DHT_PORT", "19834"))

        p2p = GlobalP2PNetwork(
            node_id=self._node_id(),
            listen_port=p2p_port,
            dht_port=dht_port,
            version=f"ww-v{WW_VERSION}",
            public_mode=False,
        )
        p2p.start()
        return p2p

    def _gateway_task_handler(self, command: str, context: dict) -> str:
        """Handle a task from the gateway (e.g. @mention / DM).

        Resolves platform identity → entity_id so multi-user chats do not
        collapse onto the default entity / shared conversation window.
        """
        logger.info("Gateway task: %s", (command or "")[:100])
        if not self.ww:
            return "WW not initialized"
        try:
            context = context or {}
            image_path = context.get("photo_path", "") or context.get("image_path", "")
            platform = (context.get("platform") or "telegram").strip() or "telegram"
            user_id = (
                str(context.get("user_id") or "").strip()
                or str(context.get("chat_id") or "").strip()
                or "default"
            )
            chat_id = str(context.get("chat_id") or user_id)
            display_name = str(context.get("sender") or "User")

            entity_id = ""
            if self.identity_resolver:
                try:
                    entity_id = self.identity_resolver.resolve(
                        platform=platform,
                        user_id=user_id,
                        chat_id=chat_id,
                        display_name=display_name,
                    )
                    # Single-user owner: link owner Telegram to primary entity
                    # so terminal/http and Telegram share the same timeline.
                    entity_id = self.identity_resolver.ensure_owner_link(
                        platform=platform,
                        user_id=user_id,
                        chat_id=chat_id,
                        entity_id=entity_id,
                        display_name=display_name,
                    )
                except Exception as e:
                    logger.warning("identity resolve failed: %s", e)

            conversation_window = (
                context.get("session_key")
                or f"{platform}:{user_id}:{chat_id}"
            )

            result = self.run_task(
                command,
                max_spirals=3,
                image_path=image_path,
                entity_id=entity_id,
                platform=platform,
                conversation_window=conversation_window,
            )

            # Prefer server-attached clean ``response``, then shared extractor
            from core.public_reply import extract_user_response

            response_text = (
                result.get("response")
                if isinstance(result.get("response"), str)
                else ""
            )
            if not response_text or self._public_reply(response_text, fallback="") == "":
                response_text = extract_user_response(result)

            if response_text:
                return self._public_reply(str(response_text)[:1500], fallback="Done.")[:1500]
            # Never leak internal status tags to chat users
            return self._public_reply("", fallback="Done.")
        except Exception as e:
            logger.error("Gateway task error: %s", e)
            return "Something went wrong. Please try again."

    def _session_lock(self, window: str) -> threading.RLock:
        """Return an order-preserving lock for a conversation window."""
        key = window or "default"
        with self._session_locks_guard:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[key] = lock
            return lock

    @staticmethod
    def _public_reply(text: str, fallback: str = "") -> str:
        """Strip internal mechanism strings before showing them to end users."""
        from core.public_reply import public_reply

        cleaned = public_reply(text, fallback="")
        if cleaned:
            return cleaned
        # Preserve gateway fallbacks for empty/internal text
        if not text:
            return fallback
        t = str(text).strip()
        low = t.lower()
        if "traceback (most recent call last)" in low:
            return fallback or "Something went wrong. Please try again."
        return fallback or "Done."

    def _get_sandbox(self):
        """Lazy load CodeSandbox. """
        if self.sandbox is None:
            from sandbox.runner import CodeSandbox
            self.sandbox = CodeSandbox()
        return self.sandbox

    # ── Task Execution ──

    def run_task(self, goal: str, max_spirals: int = 10,
                 model: str = "", provider: str = "", image_path: str = "",
                 reasoning_effort: str = "", entity_id: str = "",
                 platform: str = "http",
                 conversation_window: str = "") -> Dict[str, Any]:
        """Execute a task under session + engine locks.

        Args:
            entity_id: Optional entity ID for cross-platform continuity.
                       If empty, uses the default entity (single-user mode).
            platform: Platform identifier for context (telegram, terminal, http, etc.)
            conversation_window: Per-chat conversation key for ContextWindow isolation.
        """
        # Resolve window early so per-session lock orders same-chat messages
        window = conversation_window or (
            f"{platform}:{entity_id}" if entity_id else (platform or "http")
        )
        session_lock = self._session_lock(window)
        with session_lock:
            acquired = self._run_lock.acquire(blocking=False)
            if not acquired:
                self._lock_waits += 1
                self._run_lock.acquire()
            try:
                self._lock_runs += 1
                # ── Entity continuity: resolve and set entity context ──
                # Never use entities[0] — order is last_active DESC and wrong
                # for multi-entity nodes. Local http/terminal always go through
                # IdentityResolver (primary entity in single-user mode).
                if not entity_id and self.identity_resolver:
                    entity_id = self.identity_resolver.resolve_local(
                        platform=platform or "http",
                        user_id="default",
                        display_name="User",
                    )
                    window = conversation_window or (
                        f"{platform}:{entity_id}" if entity_id else window
                    )

                # Gate 0.2: bind entity for the ENTIRE request on ContextVar so
                # memory inject/search/tools cannot see another entity if a
                # process-global rebind races (or sequential rebind mid-flight).
                from core.memory.entity_scope import bind_entity

                with bind_entity(entity_id or "default"):
                    if entity_id:
                        self.ww.set_entity(entity_id, platform)

                    # Notify Mascot: Start thinking
                    try:
                        from core.mascot import mascot
                        mascot.on_task_start(goal)
                    except Exception:
                        pass

                    if model or provider:
                        try:
                            llm_config = {
                                "model": model or self.config.get("model", "deepseek/deepseek-v4-flash")
                            }
                            if provider:
                                llm_config["provider"] = provider
                            self.ww.llm = self.ww.llm.__class__(llm_config)
                        except Exception as e:
                            logger.warning("Model switch failed: %s", e)

                    self._last_result = self.ww.run(
                        goal,
                        max_spirals,
                        image_path=image_path,
                        reasoning_effort=reasoning_effort,
                        conversation_window=window,
                    )
                    # Always surface entity_id + clean user-facing response for clients
                    if isinstance(self._last_result, dict):
                        from core.public_reply import extract_user_response

                        self._last_result["entity_id"] = entity_id or ""
                        # Institutional E2: every /ww/run client can prefer ``response``
                        # without re-implementing leak filters. Debug fields (summary,
                        # evaluation.reason) may still say "Reflex arc" — OK if not chat.
                        self._last_result["response"] = extract_user_response(
                            self._last_result
                        )

                    # Persist entity state after task (set_entity + record_interaction
                    # already save; re-save ensures dirty in-memory edits land on disk)
                    if entity_id and self.entity_mgr:
                        try:
                            state = self.entity_mgr.get(entity_id)
                            if platform:
                                state.last_platform = platform
                            self.entity_mgr.save(state)
                        except Exception as e:
                            logger.warning("entity state save failed: %s", e)

                    self._task_history.append({
                        "goal": goal[:100],
                        "time": datetime.now(timezone.utc).isoformat(),
                        "status": self._last_result.get("status", "?"),
                        "spirals": self._last_result.get("spirals_completed", 0),
                        "entity_id": entity_id,
                        "platform": platform,
                        "window": window,
                    })

                    try:
                        from core.mascot import mascot
                        success = self._last_result.get("status") in ("completed", "success")
                        mascot.on_task_complete(
                            success, str(self._last_result.get("result", ""))[:80]
                        )
                    except Exception:
                        pass

                return self._last_result
            finally:
                self._run_lock.release()

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
        """Current status — uses in-process MemorySystem (not deleted external server)."""
        state = self.ww.state.summary() if self.ww else {"error": "not_initialized"}

        memory_info: Dict[str, Any] = {"available": False}
        mem = None
        if self.ww and getattr(self.ww, "memory", None) is not None:
            mem = self.ww.memory
        elif getattr(self, "memory", None) is not None:
            mem = self.memory
        if mem is not None:
            try:
                stats = {}
                if hasattr(mem, "overall_status"):
                    stats = mem.overall_status() or {}
                elif hasattr(mem, "get_stats"):
                    stats = mem.get_stats() or {}
                hippo = stats.get("hippocampus") if isinstance(stats, dict) else None
                count = 0
                capacity = 100
                if isinstance(hippo, dict):
                    count = hippo.get("count", hippo.get("size", 0)) or 0
                    capacity = hippo.get("capacity", 100) or 100
                elif hasattr(mem, "hippocampus"):
                    try:
                        count = len(mem.hippocampus)
                    except Exception:
                        count = 0
                memory_info = {
                    "available": True,
                    "backend": "in_process",
                    "atoms": count,
                    "capacity": capacity,
                    "sleep_cycles": stats.get("sleep_cycles") if isinstance(stats, dict) else None,
                }
            except Exception as e:
                memory_info = {"available": True, "backend": "in_process", "error": str(e)[:120]}

        model_name = ""
        provider_name = ""
        if self.ww and getattr(self.ww, "llm", None) is not None:
            model_name = getattr(self.ww.llm, "model", "") or ""
            provider_name = getattr(self.ww.llm, "provider", "") or ""

        return {
            "version": WW_VERSION,
            "ww": {
                "session": state,
                "running": self.ww.running if self.ww else False,
                "tools_count": len(self.ww.tools.tool_names()) if self.ww else 0,
                "tools_categories": self.ww.tools.category_counts() if self.ww else {},
                "model": model_name,
                "provider": provider_name,
            },
            "autonomous": {"running": self._autonomous_running},
            "scheduler": self.scheduler.info(),
            "memory": memory_info,
            "config_profile": self.config.active_profile(),
            "skills_count": len(self.skills.list()),
            "task_history_count": len(self._task_history),
            "concurrency": {
                "engine_lock_runs": self._lock_runs,
                "engine_lock_waits": self._lock_waits,
                "session_windows": len(self._session_locks),
            },
            "security": {
                "api_key_strength": (
                    "weak" if len(str(globals().get("WW_API_KEY") or os.environ.get("WW_API_KEY") or "")) < 16
                    else "ok"
                ),
                "pairing_auto_approve": _env_truthy("WW_PAIRING_AUTO_APPROVE"),
                "skip_auto_evolution": _env_truthy("WW_SKIP_AUTO_EVOLUTION"),
                "approval_mode": os.environ.get("WW_APPROVAL_MODE", "auto"),
                "bind_host": os.environ.get("WW_HOST", "0.0.0.0"),
            },
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
# WW_API_KEY (local HTTP auth) is distinct from LLM keys in .env.
# Same source of truth as CLI: env → ~/.ww/api_key → generate+persist file.
# Do NOT write WW_API_KEY into .env (avoid duplicate/corrupt comments).
from core.ww_api_key import resolve_ww_api_key

_had_env_key = bool((os.environ.get("WW_API_KEY") or "").strip())
WW_API_KEY = resolve_ww_api_key()
if not _had_env_key:
    logger.info(
        "WW_API_KEY resolved from ~/.ww/api_key (or generated) — "
        "persisted under WW_CONFIG, not .env"
    )

if WW_API_KEY and len(WW_API_KEY) < 16:
    logger.warning(
        "WW_API_KEY is only %d chars — generate a stronger key "
        "(e.g. python -c \"import secrets;print(secrets.token_urlsafe(32))\")",
        len(WW_API_KEY),
    )

# Public unauthenticated paths only. OpenAPI/docs/redoc require a valid API key.
_API_BYPASS_PREFIXES = ("/ww/health", "/ww/webui", "/ww/mascot/state")


def _extract_api_key(request: Request) -> str:
    """Accept Authorization Bearer, X-API-Key, or ?api_key=."""
    auth = request.headers.get("Authorization", "") or ""
    if auth.startswith("Bearer ") and auth[7:].strip():
        return auth[7:].strip()
    for h in ("X-API-Key", "X-Api-Key", "x-api-key"):
        v = request.headers.get(h)
        if v and str(v).strip():
            return str(v).strip()
    q = request.query_params.get("api_key")
    return (q or "").strip()


def _api_key_matches(provided: str, expected: str) -> bool:
    """Constant-time API key compare (handles length mismatch safely)."""
    if not provided or not expected:
        return False
    try:
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


@app.middleware("http")
async def api_auth_middleware(request: Request, call_next):
    """API Key verification middleware.

    Accepts Authorization Bearer, X-API-Key header, or ?api_key= query.
    """
    from fastapi.responses import JSONResponse

    if not WW_API_KEY:
        return JSONResponse(status_code=500, content={"error": "Server misconfigured"})

    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in _API_BYPASS_PREFIXES):
        return await call_next(request)

    provided = _extract_api_key(request)
    if _api_key_matches(provided, WW_API_KEY):
        return await call_next(request)

    return JSONResponse(
            status_code=401,
            content={
                "error": "Unauthorized",
                "message": "Valid API key required. Pass X-API-Key header, Authorization Bearer token, or api_key query.",
            },
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
        # Session-local spiral counter (process lifetime engine state) — not lifetime total
        result["session_spiral"] = ww.state.current_spiral
        result["current_spiral"] = ww.state.current_spiral  # back-compat
        evo = (result.get("evolution") or {}).get("metrics") or {}
        result["lifetime_spirals"] = evo.get("total_spirals", 0)
        result["lifetime_tasks"] = evo.get("total_tasks", 0)
        result["running"] = ww.running
        result["steps_completed"] = getattr(ww, "_steps_completed", 0)
        result["steps_total"] = getattr(ww, "_steps_total", 0)
        result["tool_count"] = len(ww.tools.tool_names())
        # Prefer live LLM client fields over deleted internal _models list
        model_name = getattr(ww.llm, "model", "") or ""
        providers = []
        try:
            if hasattr(ww.llm, "available_providers"):
                providers = list(ww.llm.available_providers() or [])
        except Exception:
            providers = []
        result["model"] = model_name
        result["model_count"] = 1 if model_name else 0
        result["available_providers"] = providers
        result["tool_categories"] = ww.tools.category_counts()

        # Memory System (in-process)
        if ww.memory:
            try:
                if hasattr(ww.memory, "overall_status"):
                    stats = ww.memory.overall_status()
                elif hasattr(ww.memory, "get_stats"):
                    stats = ww.memory.get_stats()
                else:
                    stats = {}
                hippo = stats.get("hippocampus", {}) if isinstance(stats, dict) else {}
                if isinstance(hippo, dict):
                    result["hippocampus_used"] = hippo.get("count", hippo.get("size", 0))
                    result["hippocampus_capacity"] = hippo.get("capacity", 100)
                else:
                    try:
                        result["hippocampus_used"] = len(ww.memory.hippocampus)
                    except Exception:
                        result["hippocampus_used"] = 0
                    result["hippocampus_capacity"] = 100
                result["memory_count"] = result.get("hippocampus_used", 0)
                result["last_sleep"] = stats.get("sleep_cycles", "-") if isinstance(stats, dict) else "-"
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
            "POST /ww/chat/new",
            "POST /ww/chat/true",
            "POST /ww/chat/stop",
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
            # Tracing & Observability (v0.9)
            "GET  /ww/trace/metrics",
            "GET  /ww/trace/recent",
            "GET  /ww/trace/current",
            # Autonomous Scheduler (v0.9)
            "GET  /ww/autonomous/status",
            "POST /ww/autonomous/add",
            "POST /ww/autonomous/remove",
            "POST /ww/autonomous/toggle",
            # User Model (v0.9)
            "GET  /ww/user-model/{entity_id}",
            "GET  /ww/user-model/stats",
            # Approval Gating (v0.9)
            "GET  /ww/approval/policies",
            "GET  /ww/approval/pending",
            "POST /ww/approval/approve",
            "POST /ww/approval/deny",
            # Skill Evolution (v0.9)
            "GET  /ww/skill-evolution/stats",
            "POST /ww/skill-evolution/extract",
            "GET  /ww/skill-evolution/auto-skills",
            # Orchestration (v0.9)
            "GET  /ww/orchestration/status",
        ],
    }


# ── Task Endpoints ──

@app.post("/ww/run")
def run(req: TaskRequest):
    platform = (req.platform or "http").strip() or "http"
    entity_id = (req.entity_id or "").strip()
    conversation_window = ""
    if not entity_id and server.identity_resolver and (req.user_id or platform != "http"):
        try:
            uid = (req.user_id or "default").strip() or "default"
            cid = (req.chat_id or uid).strip() or uid
            entity_id = server.identity_resolver.resolve(
                platform=platform,
                user_id=uid,
                chat_id=cid,
                display_name="User",
            )
            entity_id = server.identity_resolver.ensure_owner_link(
                platform=platform,
                user_id=uid,
                chat_id=cid,
                entity_id=entity_id,
                display_name="User",
            )
            conversation_window = f"{platform}:{uid}:{cid}"
        except Exception:
            entity_id = entity_id or ""
    return server.run_task(
        req.goal,
        req.max_spirals,
        req.model or "",
        req.provider or "",
        req.image_path or "",
        req.reasoning_effort or "",
        entity_id=entity_id,
        platform=platform,
        conversation_window=conversation_window,
    )


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
    """Return the active model."""
    if server.ww:
        return {"model": server.ww.model, "provider": server.ww.llm._provider if hasattr(server.ww.llm, '_provider') else "deepseek"}
    import os as _os
    return {"model": _os.environ.get("WW_MODEL", "N/A"), "provider": _os.environ.get("WW_PROVIDER", "N/A")}

@app.post("/ww/model")
def model_switch(data: dict):
    """Switch the active model on the fly."""
    model = data.get("model", "")
    if not model:
        return {"error": "model name required"}
    if not server.ww:
        return {"error": "WW not initialized"}
    result = server.ww.switch_model(model)
    return result


# ── Chat core endpoints (/new, /true, /stop) ──

@app.post("/ww/chat/new")
def chat_new(data: dict = None):
    """New session + WM cleanup (non-core only; promote via on_wm_evict).

    Does **not** wipe LTM atoms wholesale. Core WM keys and preference-linked
    keys are retained.
    """
    body = data if isinstance(data, dict) else {}
    entity_id = (body.get("entity_id") or "").strip()
    if not entity_id:
        try:
            if getattr(server, "identity_resolver", None) is not None:
                entity_id = server.identity_resolver.get_primary_entity_id() or ""
        except Exception:
            entity_id = ""
    if not entity_id and getattr(server, "entity_mgr", None) is not None:
        try:
            active = server.entity_mgr.list_active()
            entity_id = active[0] if active else ""
        except Exception:
            entity_id = ""
    if not entity_id:
        entity_id = "default"

    counts = {"wm_cleared": 0, "promoted": 0, "kept_core": 0}
    if getattr(server, "entity_mgr", None) is not None:
        try:
            counts = server.entity_mgr.clear_session_working_memory(entity_id)
        except Exception as e:
            return {"status": "error", "error": str(e)[:200], "entity_id": entity_id}

    # Soft clear conversation buffer (not LTM)
    if server.ww is not None:
        try:
            conv = getattr(server.ww, "conversation", None)
            if conv is not None and hasattr(conv, "clear"):
                conv.clear()
        except Exception:
            pass
        try:
            server.ww.running = False
        except Exception:
            pass

    return {
        "status": "ok",
        "entity_id": entity_id,
        "wm_cleared": int(counts.get("wm_cleared", 0)),
        "promoted": int(counts.get("promoted", 0)),
        "kept_core": int(counts.get("kept_core", 0)),
    }


@app.post("/ww/chat/true")
def chat_true(data: dict = None):
    """Force next tool evaluation past basal-ganglia block once (/true).

    Does not bypass approval gating for unsafe tools.
    """
    if not server.ww:
        return {"error": "WW not initialized"}
    server.ww.force_next_tool_once = True
    last = getattr(server.ww, "_last_blocked", None)
    return {
        "status": "ok",
        "force_next_tool_once": True,
        "last_blocked": last,
        "message": "Next tool will skip safety-system block once",
    }


@app.post("/ww/chat/stop")
def chat_stop(data: dict = None):
    """Stop autonomous loop and/or clear busy/running signal."""
    if getattr(server, "_autonomous_running", False):
        return server.stop_autonomous()
    if server.ww is not None:
        try:
            server.ww.stop()
        except Exception:
            server.ww.running = False
        return {"status": "stopped", "running": False}
    return {"status": "ok", "message": "no active run"}


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
    """Memory Operations (Backward Compatible: recall/search/snapshot/sleep/probe/store + update/delete)"""
    from core.memory.entity_scope import bind_entity

    # Scope search/recall to request entity when provided (Gate 0.2)
    eid = (req.entity_id or "").strip()
    if req.action == "store":
        mid = server.memory.store_text(
            content=req.content,
            entities=req.entities,
            source="api",
        )
        return {"memory_id": mid, "status": "stored"}
    elif req.action == "search":
        with bind_entity(eid or "default"):
            if eid and getattr(server.memory, "vnext", None) is not None:
                try:
                    server.memory.vnext.set_entity(eid)
                except Exception:
                    pass
            results = server.memory.search(req.query, limit=req.limit, entity_id=eid)
        return {"results": [a.to_dict() for a in results], "entity_id": eid or ""}
    elif req.action == "snapshot":
        return server.memory.snapshot(limit=req.limit)
    elif req.action == "sleep":
        result = server.memory.consolidate()
        return {"consolidation": result}
    elif req.action == "probe":
        results = server.memory.recall_engine.probe_entity(req.query)
        out = []
        for a in results or []:
            out.append(a.to_dict() if hasattr(a, "to_dict") else a)
        return {"results": out}
    elif req.action == "update":
        mid = (req.memory_id or req.query or "").strip()
        if not mid:
            return {"error": "memory_id required"}
        if not (req.content or "").strip():
            return {"error": "content required"}
        atom = server.memory.hippocampus.get(mid)
        if atom is None:
            return {"error": "not_found", "memory_id": mid}
        ok = server.memory.hippocampus.update(mid, content=req.content)
        return {
            "status": "updated" if ok else "failed",
            "updated": bool(ok),
            "memory_id": mid,
        }
    elif req.action == "delete":
        mid = (req.memory_id or req.query or "").strip()
        if not mid:
            return {"error": "memory_id required"}
        atom = server.memory.hippocampus.get(mid)
        if atom is None:
            return {"error": "not_found", "memory_id": mid}
        # Never delete is_core without explicit confirm phrase
        if getattr(atom, "is_core", False):
            phrase = (req.confirm or "").strip().lower()
            if phrase not in ("confirm", "delete core", "yes delete core"):
                return {
                    "error": "is_core",
                    "memory_id": mid,
                    "message": "Core memory requires confirm phrase",
                }
        ok = server.memory.hippocampus.remove(mid)
        return {
            "status": "deleted" if ok else "failed",
            "deleted": bool(ok),
            "memory_id": mid,
        }
    else:  # recall (default)
        # MemorySystem.recall returns a dict with results list of
        # {atom, salience, hops} — not a list of MemoryAtom.
        payload = server.memory.recall(req.query, top_k=req.limit)
        if isinstance(payload, dict):
            return payload
        return {"results": payload, "total": len(payload) if payload else 0}

@app.get("/ww/memory/stats")
def memory_stats():
    """Memory System Statistics (includes entity Working Memory capacity when available)."""
    out = server.memory.get_stats()
    if not isinstance(out, dict):
        out = {"stats": out}
    if getattr(server, "entity_mgr", None) is not None:
        out["working_memory_capacity"] = server.entity_mgr.working_memory_capacity
    return out

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
    payload = server.memory.recall(query, top_k=limit)
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("count", payload.get("total", len(payload.get("results") or [])))
        return payload
    return {"results": payload, "count": len(payload) if payload else 0}

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
    except Exception:
        return {"success": False, "error": "MQTT publish failed"}


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


# ── Observability & Tracing (v0.9) ──

@app.get("/ww/trace/metrics")
def trace_metrics():
    """Get spiral tracing performance metrics (p50/p95, bottlenecks, error summary)."""
    if not server.ww:
        return {"error": "WW not initialized"}
    return server.ww.tracer.metrics()


@app.get("/ww/trace/recent")
def trace_recent(limit: int = 20):
    """Get recent spiral traces."""
    if not server.ww:
        return {"traces": []}
    return {"traces": server.ww.tracer.get_recent(limit)}


@app.get("/ww/trace/current")
def trace_current():
    """Get currently active spiral trace."""
    if not server.ww:
        return {"trace": None}
    return {"trace": server.ww.tracer.get_current()}


# ── Autonomous Scheduler (v0.9) ──

@app.get("/ww/autonomous/status")
def autonomous_status():
    """Get autonomous scheduler heartbeat stats and task list."""
    if not server.ww:
        return {"error": "WW not initialized"}
    return {
        "stats": server.ww.autonomous_scheduler.stats(),
        "tasks": server.ww.autonomous_scheduler.list_tasks(),
    }


@app.post("/ww/autonomous/start")
def autonomous_start():
    """Start the autonomous heartbeat loop."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    server.ww.autonomous_scheduler.enabled = True
    server.ww.autonomous_scheduler.start()
    server._autonomous_running = True
    return {"status": "started", "interval": server.ww.autonomous_scheduler.heartbeat_interval}


@app.post("/ww/autonomous/stop")
def autonomous_stop():
    """Stop the autonomous heartbeat loop."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    server.ww.autonomous_scheduler.stop()
    server._autonomous_running = False
    return {"status": "stopped"}


@app.post("/ww/autonomous/add")
def autonomous_add(name: str, goal: str, schedule: str = "1h", priority: int = 5):
    """Add a scheduled task with natural-language schedule."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    task_id = server.ww.autonomous_scheduler.add_task(
        name=name, goal=goal, schedule=schedule, priority=priority
    )
    return {"task_id": task_id, "schedule": schedule}


@app.post("/ww/autonomous/remove")
def autonomous_remove(task_id: str):
    """Remove a scheduled task."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    ok = server.ww.autonomous_scheduler.remove_task(task_id)
    return {"removed": ok}


@app.post("/ww/autonomous/toggle")
def autonomous_toggle(task_id: str, enabled: bool = None):
    """Enable/disable a scheduled task."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    ok = server.ww.autonomous_scheduler.toggle_task(task_id, enabled)
    return {"toggled": ok}


# ── User Model (v0.9) ──

@app.get("/ww/user-model/{entity_id}")
def user_model_get(entity_id: str):
    """Get dynamic user model for an entity."""
    if not server.ww:
        return {"error": "WW not initialized"}
    model = server.ww.user_models.get(entity_id)
    return {
        "entity_id": entity_id,
        "style": model.style.to_dict(),
        "implicit_goals": [g.to_dict() for g in model.get_active_goals()],
        "expertise": {k: v.to_dict() for k, v in model.expertise.items()},
        "active_hours": model.get_active_hours(),
        "likely_available": model.is_likely_available(),
    }


@app.get("/ww/user-model/stats")
def user_model_stats():
    """Get user model manager statistics."""
    if not server.ww:
        return {"error": "WW not initialized"}
    return server.ww.user_models.stats()


# ── Approval Gating (v0.9) ──

@app.get("/ww/approval/policies")
def approval_policies():
    """List all active approval policies."""
    if not server.ww:
        return {"policies": []}
    return {"policies": server.ww.approval_gating.list_policies()}


@app.get("/ww/approval/pending")
def approval_pending():
    """List pending approval requests."""
    if not server.ww:
        return {"pending": []}
    return {"pending": server.ww.approval_gating.get_pending()}


@app.post("/ww/approval/approve")
def approval_approve(approval_id: str):
    """Approve a pending action."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    ok = server.ww.approval_gating.approve(approval_id)
    return {"approved": ok}


@app.post("/ww/approval/deny")
def approval_deny(approval_id: str):
    """Deny a pending action."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    ok = server.ww.approval_gating.deny(approval_id)
    return {"denied": ok}


# ── Skill Evolution (v0.9) ──

@app.get("/ww/skill-evolution/stats")
def skill_evolution_stats():
    """Get skill evolution engine statistics."""
    if not server.ww:
        return {"error": "WW not initialized"}
    return server.ww.skill_evolution.stats()


@app.post("/ww/skill-evolution/extract")
def skill_evolution_extract(goal: str):
    """Force skill extraction for a goal (debug/testing)."""
    if not server.ww:
        raise HTTPException(400, "WW not initialized")
    result = server.ww.skill_evolution.force_extract(goal)
    return {"skill": result}


@app.get("/ww/skill-evolution/auto-skills")
def skill_evolution_auto_skills():
    """List auto-generated skill names."""
    if not server.ww:
        return {"skills": []}
    return {"skills": server.ww.skill_evolution.list_auto_skills()}


# ── Orchestration (v0.9) ──

@app.get("/ww/orchestration/status")
def orchestration_status():
    """Get delegation/orchestration statistics."""
    if not server.ww:
        return {"error": "WW not initialized"}
    return {"delegator": server.ww.delegator.stats()}


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

        except Exception:
            yield _sse({"error": "stream processing failed", "done": True})

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


# ── Start (lifespan — replaces deprecated on_event) ──

def _bootstrap_runtime():
    """Background services after HTTP server is ready."""
    import threading

    # Propagate main LLM model info for Computer Use vision auto-detection
    cfg = ConfigManager()
    model = cfg.get("model") or os.environ.get("MODEL", "")
    provider = cfg.get("provider") or os.environ.get("PROVIDER", "")
    if model:
        os.environ.setdefault("WW_MAIN_MODEL", model)
    if provider:
        os.environ.setdefault("WW_MAIN_PROVIDER", provider)

    # Surface insecure runtime flags early
    if _env_truthy("WW_PAIRING_AUTO_APPROVE"):
        logger.warning("WW_PAIRING_AUTO_APPROVE is set — DM pairing whitelist is bypassed")
    if _env_truthy("WW_SKIP_AUTO_EVOLUTION"):
        logger.info("WW_SKIP_AUTO_EVOLUTION set — auto evolution scheduler disabled")
    if os.environ.get("WW_APPROVAL_MODE", "auto").lower() == "auto":
        logger.info("Tool approval_mode=auto (set WW_APPROVAL_MODE=hitl for confirmation)")

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
            logger.warning("Contacts start failed: %s", e)

        # Start P2P network (global gossip + tracker bootstrap)
        try:
            server.p2p = server._init_p2p()
            if server.p2p:
                logger.info(
                    "P2P network started: node=%s mode=%s peers=%d",
                    server.p2p.node_id[:12],
                    "public" if server.p2p.public_mode else "private",
                    server.p2p.peer_count(),
                )
        except Exception as e:
            logger.warning("P2P network start failed: %s", e)

        time.sleep(3)  # allow WW to settle
        try:
            if not server.ww:
                return
            server.start_scheduler()
            if os.environ.get("WW_SKIP_AUTO_EVOLUTION"):
                logger.info("Auto-evolution skipped (WW_SKIP_AUTO_EVOLUTION set)")
                return
            existing = server.ww.scheduler.list()
            has_evo = any(t.get("name") == "auto-evolution" for t in existing)
            if not has_evo:
                server.ww.scheduler.add(
                    name="auto-evolution",
                    goal=(
                        "Self-audit and evolution: check system state, code issues, "
                        "execute evolution cycle. Auto-fix improvement points when found."
                    ),
                    schedule="0 * * * *",
                )
        except Exception as e:
            logger.warning("Auto-scheduler init failed: %s", e)

    threading.Thread(target=_start_scheduler, daemon=True).start()

    # Start Mascot
    try:
        from core.mascot import mascot as mascot_instance
        mascot_instance.start()
        logger.info("Mascot fat shark mascot started")
    except Exception as e:
        logger.warning("Mascot init failed: %s", e)

    # Windows: auto-start system tray
    def _launch_tray():
        time.sleep(5)
        try:
            import subprocess
            script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "core", "mascot", "launcher.ps1",
            )
            if not os.path.exists(script):
                return

            if os.name == "nt":
                subprocess.Popen([
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-WindowStyle", "Hidden", "-File", script, "-Tray",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
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


def _shutdown_runtime():
    try:
        if server.gateway:
            server.gateway.stop_all()
            logger.info("Gateway adapters stopped")
    except Exception as e:
        logger.warning("Gateway stop failed: %s", e)


from contextlib import asynccontextmanager


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    _bootstrap_runtime()
    try:
        yield
    finally:
        _shutdown_runtime()


# Prefer lifespan over deprecated on_event("startup")
app.router.lifespan_context = _app_lifespan


# ── Entity Identity Endpoints (P0: Persistent Cognitive Entity) ──

@app.get("/ww/identity/entities")
def identity_list():
    """List all known entities."""
    return {"entities": server.identity_resolver.get_all_entities()}

@app.get("/ww/identity/entity/{entity_id}")
def identity_get(entity_id: str):
    """Get entity details including platform links."""
    entity = server.identity_resolver.get_entity(entity_id)
    if not entity:
        raise HTTPException(404, f"Entity {entity_id} not found")
    links = server.identity_resolver.get_platform_ids(entity_id)
    return {"entity": entity, "platform_links": links}

@app.post("/ww/identity/link")
def identity_link(req: dict):
    """Link a platform identity to an entity."""
    entity_id = req.get("entity_id", "")
    platform = req.get("platform", "")
    user_id = req.get("user_id", "")
    chat_id = req.get("chat_id", "")
    if not entity_id or not platform or not user_id:
        raise HTTPException(400, "entity_id, platform, user_id required")
    server.identity_resolver.link(entity_id, platform, user_id, chat_id)
    return {"status": "linked"}

@app.get("/ww/identity/state/{entity_id}")
def entity_state_get(entity_id: str):
    """Get entity persistent state (working memory, preferences, context).

    Includes working_memory_size, working_memory_capacity, wm_evicted_total
    for Working Memory (bounded entity RAM) observability.
    """
    state = server.entity_mgr.get(entity_id)
    out = state.to_dict()
    out.update(server.entity_mgr.get_wm_status(entity_id))
    return out

@app.post("/ww/identity/state/{entity_id}")
def entity_state_set(entity_id: str, req: dict):
    """Set entity working memory or preferences.

    Working memory writes go through capacity enforcement (evict on overflow).
    """
    if "working_memory" in req:
        for k, v in req["working_memory"].items():
            server.entity_mgr.set_working_memory(entity_id, str(k), str(v))
    state = server.entity_mgr.get(entity_id)
    if "preferences" in req:
        for k, v in req["preferences"].items():
            state.preferences[k] = v
        server.entity_mgr.save(state)
    out = state.to_dict()
    out.update(server.entity_mgr.get_wm_status(entity_id))
    return out


if __name__ == "__main__":
    port = int(os.environ.get("WW_PORT", 9300))
    host = os.environ.get("WW_HOST", "0.0.0.0").strip() or "0.0.0.0"
    logger.info("Worldwave v0.3 API @ http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
