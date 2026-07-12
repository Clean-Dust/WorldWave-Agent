"""
core/tracing.py — Spiral Tracing & Observability Engine

Provides production-grade observability for the spiral cognitive loop:
- Per-phase timing and token usage tracking
- Decision path visualization (which tools called, in what order)
- Performance metrics aggregation (spiral success rate, latency p50/p95)
- Error correlation and root cause hints
- JSON export for dashboard consumption

Config:
  WW_TRACING_ENABLED = "true" (default: true)
  WW_TRACING_MAX_TRACES = 500 (default: 500)
  WW_TRACING_EXPORT_DIR = "~/.ww/traces/" (default)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("ww.tracing")

TRACE_DIR = os.path.expanduser("~/.ww/traces")
TRACE_DB = os.path.join(TRACE_DIR, "traces.jsonl")


@dataclass
class PhaseTrace:
    """Trace of a single spiral phase execution."""
    phase: str               # perceive, recall, plan, act, evaluate, learn, gate
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    duration_ms: float = 0.0
    tokens_used: int = 0
    success: bool = True
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def complete(self, success: bool = True, tokens: int = 0, error: str = "",
                 **meta):
        self.ended_at = time.time()
        self.duration_ms = (self.ended_at - self.started_at) * 1000
        self.success = success
        self.tokens_used = tokens
        self.error = error
        self.metadata.update(meta)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "duration_ms": round(self.duration_ms, 1),
            "tokens_used": self.tokens_used,
            "success": self.success,
            "error": self.error[:200],
            "metadata": self.metadata,
        }


@dataclass
class SpiralTrace:
    """Full trace of a single spiral loop execution."""
    trace_id: str
    spiral_number: int
    session_id: str = ""
    goal: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    duration_ms: float = 0.0
    phases: List[PhaseTrace] = field(default_factory=list)
    total_tokens: int = 0
    success: bool = False
    actions_count: int = 0
    tools_called: List[str] = field(default_factory=list)
    error: str = ""
    entity_id: str = ""
    complexity_score: float = 0.0
    reflex_used: bool = False

    def add_phase(self, phase: PhaseTrace):
        self.phases.append(phase)

    def complete(self, success: bool = False, error: str = ""):
        self.ended_at = time.time()
        self.duration_ms = (self.ended_at - self.started_at) * 1000
        self.success = success
        self.error = error
        self.total_tokens = sum(p.tokens_used for p in self.phases)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "spiral_number": self.spiral_number,
            "session_id": self.session_id,
            "goal": self.goal[:200],
            "duration_ms": round(self.duration_ms, 1),
            "total_tokens": self.total_tokens,
            "success": self.success,
            "error": self.error[:200],
            "actions_count": self.actions_count,
            "tools_called": self.tools_called,
            "phases": [p.to_dict() for p in self.phases],
            "entity_id": self.entity_id,
            "complexity_score": round(self.complexity_score, 3),
            "reflex_used": self.reflex_used,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(),
        }

    def phase_timeline(self) -> List[Dict]:
        """Return timeline view for dashboard visualization."""
        timeline = []
        for p in self.phases:
            timeline.append({
                "phase": p.phase,
                "duration_ms": round(p.duration_ms, 1),
                "success": p.success,
                "tokens": p.tokens_used,
            })
        return timeline

    def bottleneck_phase(self) -> Optional[str]:
        """Return the slowest phase name."""
        if not self.phases:
            return None
        slowest = max(self.phases, key=lambda p: p.duration_ms)
        return slowest.phase


@dataclass
class SessionTrace:
    """Aggregated trace for a full session (multiple spirals)."""
    session_id: str
    goal: str = ""
    spirals: List[SpiralTrace] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0

    def add_spiral(self, trace: SpiralTrace):
        self.spirals.append(trace)

    def complete(self):
        self.ended_at = time.time()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "goal": self.goal[:200],
            "spirals_count": len(self.spirals),
            "total_duration_ms": round((self.ended_at - self.started_at) * 1000, 1),
            "total_tokens": sum(s.total_tokens for s in self.spirals),
            "success": all(s.success for s in self.spirals) if self.spirals else False,
            "spirals": [s.to_dict() for s in self.spirals],
        }


class TraceCollector:
    """Collects and persists spiral execution traces.

    Single instance per Worldwave process.
    Exports to JSONL for easy import into dashboards / log aggregators.
    """

    def __init__(self, enabled: bool = True, max_traces: int = 500):
        self.enabled = enabled
        self.max_traces = max_traces
        self._traces: List[SpiralTrace] = []
        self._current_trace: Optional[SpiralTrace] = None
        self._current_phase: Optional[PhaseTrace] = None
        self._total_traces = 0
        self._export_interval = 50  # Export every N traces
        os.makedirs(TRACE_DIR, exist_ok=True)

    # ── Trace Lifecycle ───────────────────────────────────────

    def start_spiral(self, spiral_number: int, session_id: str = "",
                     goal: str = "", entity_id: str = "",
                     complexity: float = 0.0, reflex: bool = False) -> str:
        """Begin tracing a new spiral. Returns trace_id."""
        if not self.enabled:
            return ""
        trace_id = uuid.uuid4().hex[:12]
        self._current_trace = SpiralTrace(
            trace_id=trace_id,
            spiral_number=spiral_number,
            session_id=session_id,
            goal=goal,
            entity_id=entity_id,
            complexity_score=complexity,
            reflex_used=reflex,
        )
        return trace_id

    def start_phase(self, phase: str):
        """Begin tracing a phase within the current spiral."""
        if not self.enabled or not self._current_trace:
            return
        self._current_phase = PhaseTrace(phase=phase)

    def end_phase(self, success: bool = True, tokens: int = 0,
                  error: str = "", **meta):
        """Complete the current phase trace."""
        if not self.enabled or not self._current_phase:
            return
        self._current_phase.complete(
            success=success, tokens=tokens, error=error, **meta
        )
        if self._current_trace:
            self._current_trace.add_phase(self._current_phase)
            if not success:
                self._current_trace.error = error or f"Phase {self._current_phase.phase} failed"
        self._current_phase = None

    def record_action(self, tool_name: str, success: bool):
        """Record a tool execution within the current spiral."""
        if not self.enabled or not self._current_trace:
            return
        self._current_trace.actions_count += 1
        self._current_trace.tools_called.append(tool_name)

    def end_spiral(self, success: bool = False, error: str = ""):
        """Complete the current spiral trace."""
        if not self.enabled or not self._current_trace:
            return
        # End any dangling phase
        if self._current_phase:
            self.end_phase()

        self._current_trace.complete(success=success, error=error)
        self._traces.append(self._current_trace)
        self._total_traces += 1
        self._current_trace = None

        # Prune old traces
        if len(self._traces) > self.max_traces:
            self._traces = self._traces[-self.max_traces:]

        # Periodic export
        if self._total_traces % self._export_interval == 0:
            self._export_recent()

    # ── Export ────────────────────────────────────────────────

    def _export_recent(self):
        """Export recent traces to JSONL file."""
        try:
            recent = self._traces[-self._export_interval:]
            with open(TRACE_DB, "a") as f:
                for t in recent:
                    f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning(f"Trace export failed: {e}")

    def export_all(self) -> str:
        """Export all in-memory traces and return file path."""
        if not self._traces:
            return ""
        export_path = os.path.join(
            TRACE_DIR,
            f"trace-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        )
        try:
            with open(export_path, "w") as f:
                for t in self._traces:
                    f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
            log.info(f"Exported {len(self._traces)} traces to {export_path}")
            return export_path
        except Exception as e:
            log.error(f"Export all failed: {e}")
            return ""

    # ── Query / Analysis ──────────────────────────────────────

    def get_recent(self, limit: int = 20) -> List[Dict]:
        """Return recent trace summaries."""
        return [t.to_dict() for t in self._traces[-limit:]]

    def get_current(self) -> Optional[Dict]:
        """Return the currently active trace."""
        if not self._current_trace:
            return None
        return {
            "trace_id": self._current_trace.trace_id,
            "spiral_number": self._current_trace.spiral_number,
            "goal": self._current_trace.goal[:200],
            "elapsed_ms": round((time.time() - self._current_trace.started_at) * 1000, 1),
            "phases_completed": len(self._current_trace.phases),
            "current_phase": self._current_phase.phase if self._current_phase else "none",
        }

    def metrics(self) -> Dict:
        """Compute aggregate performance metrics."""
        if not self._traces:
            return {"samples": 0}

        recent = self._traces[-100:]  # Last 100 traces
        durations = sorted([t.duration_ms for t in recent if t.duration_ms > 0])
        tokens = [t.total_tokens for t in recent if t.total_tokens > 0]
        successes = [t for t in recent if t.success]

        # Phase-level aggregation
        phase_stats: Dict[str, List[float]] = {}
        for t in recent:
            for p in t.phases:
                if p.phase not in phase_stats:
                    phase_stats[p.phase] = []
                phase_stats[p.phase].append(p.duration_ms)

        phase_metrics = {}
        for phase, durs in phase_stats.items():
            sd = sorted(durs)
            phase_metrics[phase] = {
                "count": len(sd),
                "avg_ms": round(sum(sd) / len(sd), 1) if sd else 0,
                "p50_ms": round(sd[len(sd)//2], 1) if sd else 0,
                "p95_ms": round(sd[int(len(sd)*0.95)], 1) if len(sd) > 1 else 0,
            }

        # Bottleneck analysis
        bottlenecks = {}
        for t in recent:
            bp = t.bottleneck_phase()
            if bp:
                bottlenecks[bp] = bottlenecks.get(bp, 0) + 1

        return {
            "samples": len(recent),
            "total_traces": self._total_traces,
            "success_rate": f"{len(successes)/len(recent):.1%}" if recent else "N/A",
            "avg_spiral_ms": round(sum(durations)/len(durations), 1) if durations else 0,
            "p50_ms": round(durations[len(durations)//2], 1) if durations else 0,
            "p95_ms": round(durations[int(len(durations)*0.95)], 1) if len(durations) > 1 else 0,
            "avg_tokens": round(sum(tokens)/len(tokens), 0) if tokens else 0,
            "phase_metrics": phase_metrics,
            "bottlenecks": sorted(bottlenecks.items(), key=lambda x: x[1], reverse=True),
            "reflex_rate": f"{sum(1 for t in recent if t.reflex_used)/len(recent):.1%}",
        }

    def error_summary(self) -> List[Dict]:
        """Summarize recent errors for root cause analysis."""
        failed = [t for t in self._traces[-100:] if not t.success]
        if not failed:
            return []

        error_counts: Dict[str, int] = {}
        error_phases: Dict[str, List[str]] = {}
        for t in failed:
            err = t.error[:50] or "unknown"
            error_counts[err] = error_counts.get(err, 0) + 1
            for p in t.phases:
                if not p.success:
                    key = p.phase
                    if key not in error_phases:
                        error_phases[key] = []
                    error_phases[key].append(err)

        return [
            {
                "error": err,
                "count": cnt,
                "phases": ", ".join([p for p, errs in error_phases.items() if any(err in e for e in errs)]),
            }
            for err, cnt in sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        ]

    def stats(self) -> Dict:
        """Full observability stats."""
        return {
            "metrics": self.metrics(),
            "errors": self.error_summary(),
            "active_trace": self.get_current(),
        }
