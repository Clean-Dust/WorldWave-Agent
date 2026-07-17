"""
ww/core/state.py — Worldwave statemanagement 

Implements similar to LangGraph checkpointing but more lightweight:
- Each spiral phase auto checkpoints
- Can recover from last checkpoint on interruption
- supports Human-in-the-loop interrupt/resume
- All state JSON is serializable (convenient for persistence and transmission)
"""

from __future__ import annotations
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class Checkpoint:
    """a checkpoint snapshot."""
    id: str
    spiral_number: int
    phase: str  # perceive, recall, plan, act, evaluate, learn
    timestamp: str
    context: Dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    interrupt_reason: str = ""
    resume_data: Optional[Dict[str, Any]] = None


@dataclass
class SpiralState:
    """
    a spiral loop completestate.
    
    each spiral = one complete perceive→memory→plan→line action→evaluate→learn.
    corresponds to human a "thought segment".
    """
    spiral_number: int
    perception: Dict[str, Any] = field(default_factory=dict)
    recall: Dict[str, Any] = field(default_factory=dict)
    plan: Dict[str, Any] = field(default_factory=dict)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    evaluation: Dict[str, Any] = field(default_factory=dict)
    learning: Dict[str, Any] = field(default_factory=dict)
    
    # metadata
    id: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    
    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()


class StateManager:
    """
    statemanagement — WW's memory+disk state.
    
    feature: 
    1. trace when at which spiral, which phase
    2. auto checkpoint (end of each phase)
    3. break/recovery
    4. complete spiral history
    """
    
    def __init__(self, persist_dir: str = ""):
        self.persist_dir = persist_dir or os.path.join(
            os.path.dirname(__file__), "..", "data"
        )
        os.makedirs(self.persist_dir, exist_ok=True)
        
        # Runtime state
        self.session_id = uuid.uuid4().hex[:12]
        self.current_spiral = 0
        self.current_phase = "idle"
        self.spirals: List[SpiralState] = []
        self.current: Optional[SpiralState] = None
        
        # Checkpoint chain
        self.checkpoints: List[Checkpoint] = []
        
        # Global context (persistent across spirals)
        self.global_context: Dict[str, Any] = {
            "session_started": datetime.now(timezone.utc).isoformat(),
            "total_spirals": 0,
            "interrupts": [],
        }
        
        # Load checkpoint (if restarting)
        self._load_last_session()
    
    def begin_spiral(self) -> SpiralState:
        """start a new spiral."""
        self.current_spiral += 1
        self.current = SpiralState(spiral_number=self.current_spiral)
        self.current_phase = "perceive"
        self.spirals.append(self.current)
        self.global_context["total_spirals"] = self.current_spiral
        
        self._checkpoint("perceive_begin")
        return self.current
    
    def set_phase(self, phase: str):
        """switch to spiral phase, auto checkpoint."""
        phase_map = {
            "perceive": "recall",
            "recall": "plan",
            "plan": "act",
            "act": "evaluate",
            "evaluate": "learn",
            "learn": "completed",
        }
        if phase in phase_map:
            self._checkpoint(phase)
            self.current_phase = phase_map.get(phase, "completed")
    
    def complete_spiral(self):
        """complete when spiral."""
        if self.current:
            self.current.completed_at = datetime.now(timezone.utc).isoformat()
            self.current_phase = "idle"
            self._checkpoint("spiral_complete")
            self._save_session()
    
    def interrupt(self, reason: str, resume_data: Optional[Dict] = None):
        """
         break when process (Human-in-the-loop).
        
        WW stops here, wait for external input to resume.
        """
        cp = Checkpoint(
            id=uuid.uuid4().hex[:8],
            spiral_number=self.current_spiral,
            phase=self.current_phase,
            timestamp=datetime.now(timezone.utc).isoformat(),
            context=self._build_context(),
            interrupted=True,
            interrupt_reason=reason,
            resume_data=resume_data,
        )
        self.checkpoints.append(cp)
        self.global_context["interrupts"].append({
            "checkpoint_id": cp.id,
            "reason": reason,
            "phase": self.current_phase,
            "spiral": self.current_spiral,
        })
        # Cap interrupt history so metrics never balloon across long sessions
        hist = self.global_context.get("interrupts") or []
        if len(hist) > 50:
            self.global_context["interrupts"] = hist[-50:]
        self._save_session()
        return cp

    def clear_interrupts(self) -> int:
        """Clear active interrupt flags so a new task can proceed.

        Returns number of interrupts cleared. History is retained for metrics
        but no longer blocks ``get_last_checkpoint()``.
        """
        n = 0
        for cp in self.checkpoints:
            if cp.interrupted:
                cp.interrupted = False
                n += 1
        return n

    def prepare_for_run(self, conversation_window: str = "") -> str:
        """Prepare process-global state for a new ``/ww/run`` task.

        Root-cause fix (Gate 0.6): a shared StateManager previously kept
        rewind/interrupt checkpoints forever. Subsequent runs on any
        conversation hit ``get_last_checkpoint()`` and returned
        status=completed with empty results and a metrics-dict summary.

        Rules:
        - Each task call clears active interrupts (HTTP runs are not
          mid-spiral HITL resumes unless explicitly resumed).
        - When ``conversation_window`` changes, mint a fresh session_id so
          one poisoned chat cannot stick another window to the same id.
        - Same window: still clear interrupts; keep session_id for continuity
          only when there is no active interrupt poison.
        """
        window = (conversation_window or "").strip()
        prev_window = str(self.global_context.get("conversation_window") or "")
        had_active = any(c.interrupted for c in self.checkpoints)
        self.clear_interrupts()

        rotate = False
        if window and window != prev_window:
            rotate = True
        elif had_active:
            # Stale rewind/interrupt must not stick session_id across tasks
            rotate = True

        if rotate:
            self.session_id = uuid.uuid4().hex[:12]
            self.current_spiral = 0
            self.current_phase = "idle"
            self.current = None
            # Drop checkpoint chain (history already logged); keep spirals light
            self.checkpoints = []
            # Preserve total_spirals count only as soft metric; reset per-session
            self.global_context = {
                "session_started": datetime.now(timezone.utc).isoformat(),
                "total_spirals": 0,
                "interrupts": [],
                "conversation_window": window,
            }
        else:
            self.global_context["conversation_window"] = window or prev_window

        return self.session_id
    
    def resume(self, checkpoint_id: str, input_data: Dict[str, Any]):
        """from breakpoint recovery."""
        for cp in self.checkpoints:
            if cp.id == checkpoint_id:
                cp.interrupted = False
                # Reload context
                if self.current and input_data:
                    self.current.__dict__.update(input_data)
                self._save_session()
                return True
        return False
    
    def get_last_checkpoint(self) -> Optional[Checkpoint]:
        """get the latest breakpoint (for recovery)."""
        for cp in reversed(self.checkpoints):
            if cp.interrupted:
                return cp
        return None
    
    def get_spiral(self, n: int) -> Optional[SpiralState]:
        """get specific spiral state."""
        for s in self.spirals:
            if s.spiral_number == n:
                return s
        return None
    
    def summary(self) -> Dict[str, Any]:
        """when  statesummary. """
        return {
            "session_id": self.session_id,
            "current_spiral": self.current_spiral,
            "current_phase": self.current_phase,
            "total_checkpoints": len(self.checkpoints),
            "total_spirals": len(self.spirals),
            "active_interrupts": sum(1 for c in self.checkpoints if c.interrupted),
            "interrupt_history": self.global_context.get("interrupts", []),
        }
    
    def _checkpoint(self, phase_label: str):
        """auto-create checkpoint. """
        cp = Checkpoint(
            id=uuid.uuid4().hex[:8],
            spiral_number=self.current_spiral,
            phase=self.current_phase,
            timestamp=datetime.now(timezone.utc).isoformat(),
            context=self._build_context(),
        )
        self.checkpoints.append(cp)
        self._save_session()
    
    def _build_context(self) -> Dict[str, Any]:
        """create when context snapshot."""
        return {
            "session_id": self.session_id,
            "spiral": self.current_spiral,
            "phase": self.current_phase,
            "spiral_history_summary": [
                {
                    "n": s.spiral_number,
                    "plan": s.plan.get("goal", "")[:50] if s.plan else "",
                    "actions_count": len(s.actions),
                    "completed": bool(s.completed_at),
                }
                for s in self.spirals[-5:]  # only keep the latest 5 summaries
            ],
        }
    
    def _save_session(self):
        """persist to disk."""
        path = os.path.join(self.persist_dir, f"session_{self.session_id}.json")
        data = {
            "session_id": self.session_id,
            "current_spiral": self.current_spiral,
            "current_phase": self.current_phase,
            "global_context": self.global_context,
            "checkpoints": [asdict(c) for c in self.checkpoints],
            "spirals": [
                asdict(s) for s in self.spirals[-10:]  # keep the latest 10 spirals
            ],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    
    def _load_last_session(self):
        """try to load the latest session.

        Gate 0.6: never adopt a global latest session that has active
        interrupts or belongs to a different conversation_window. Process
        restarts start clean unless a healthy same-window session exists.
        """
        if not os.path.isdir(self.persist_dir):
            return
        sessions = sorted(
            [f for f in os.listdir(self.persist_dir) if f.startswith("session_")],
            reverse=True,
        )
        if not sessions:
            return
        path = os.path.join(self.persist_dir, sessions[0])
        try:
            with open(path) as f:
                data = json.load(f)
            cps = data.get("checkpoints", [])
            # Discard interrupted / poisoned sessions
            if cps and any(c.get("interrupted") for c in cps):
                try:
                    os.remove(path)
                except OSError:
                    pass
                return
            # Discard sessions that still list interrupt history with high count
            # (legacy poison from pre-0.6 rewinds that left interrupted=False
            # but left the process stuck via other paths)
            gctx = data.get("global_context") or {}
            interrupts = gctx.get("interrupts") or []
            if len(interrupts) >= 10:
                try:
                    os.remove(path)
                except OSError:
                    pass
                return
            self.session_id = data.get("session_id", self.session_id)
            self.current_spiral = data.get("current_spiral", 0)
            self.global_context = gctx if gctx else self.global_context
            # Do not load complete spirals (avoid memory bloat)
            # Only keep non-interrupted checkpoints
            if cps:
                self.checkpoints = [
                    Checkpoint(**c) for c in cps if not c.get("interrupted")
                ]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
