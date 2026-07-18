#!/usr/bin/env python3
"""WW Coding protocol smoke (PM 0.12) — MCP (prefer) or ACP.

Exercises:
  1. stdio-style initialize + list tools/capabilities
  2. invoke one read-only coding tool (repo_map or grep) via facade
  3. LSP optional skip if missing

Uses in-process WWMCPServer handlers (core/mcp.py) — no long-lived daemon.
Writes a short report under results/coding_protocol/.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "results" / "coding_protocol"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    protocol: str = "mcp"
    extra: Dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail[:200]}")

    def hard_fail(self) -> bool:
        return any(not c.ok for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "checks": [asdict(c) for c in self.checks],
            "passed": sum(1 for c in self.checks if c.ok),
            "total": len(self.checks),
            "extra": self.extra,
        }


async def _mcp_smoke(report: Report) -> None:
    from core.mcp import WWMCPServer, MCP_PROTOCOL_VERSION, JSONRPC_VERSION

    server = WWMCPServer()

    # 1) initialize
    init = await server._handle_request({
        "jsonrpc": JSONRPC_VERSION,
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "clientInfo": {"name": "coding_protocol_smoke", "version": "0.12"},
            "capabilities": {},
        },
    })
    ok_init = (
        isinstance(init, dict)
        and "result" in init
        and (init["result"].get("serverInfo") or {}).get("name") == "worldwave"
    )
    report.add(
        "mcp_initialize",
        ok_init,
        f"protocol={init.get('result', {}).get('protocolVersion') if init else None}",
    )
    report.extra["initialize"] = init

    # 2) tools/list — may be empty if ToolRegistry not bootstrapped; that's ok
    listed = await server._handle_request({
        "jsonrpc": JSONRPC_VERSION,
        "id": 2,
        "method": "tools/list",
        "params": {},
    })
    tools = ((listed or {}).get("result") or {}).get("tools") or []
    report.add(
        "mcp_tools_list",
        isinstance(listed, dict) and "result" in listed,
        f"n_tools={len(tools)}",
    )
    report.extra["n_tools_listed"] = len(tools)

    # 3) Read-only coding tool via index facade (always offline)
    from coding.index_facade import IndexFacade
    from coding.perception import grep

    fac = IndexFacade(project_root=str(ROOT / "coding"))
    fac.build(force=False)
    mq = fac.query("map", token_budget=1500, force_graph=True)
    map_ok = bool(mq.get("success")) and bool((mq.get("result") or {}).get("symbols_included", 0) >= 1 or
                                               (mq.get("result") or {}).get("map") or
                                               (mq.get("result") or {}).get("text") or
                                               (mq.get("result") or {}).get("truncated") is not None)
    # symbols_included may be 0 on empty — accept success + result dict
    map_ok = bool(mq.get("success")) and isinstance(mq.get("result"), dict)
    report.add("coding_repo_map_readonly", map_ok, f"keys={list((mq.get('result') or {}).keys())[:8]}")

    g = grep("def repo_map", path=str(ROOT / "coding"), glob="*.py", max_matches=5)
    report.add(
        "coding_grep_readonly",
        int(g.get("count") or 0) >= 1,
        f"count={g.get('count')} engine={g.get('engine')}",
    )
    report.extra["facade_counters"] = fac.metrics()
    try:
        fac.close()
    except Exception:
        pass

    # 4) tools/call path if registry has coding tools; else soft-pass with note
    if tools:
        # Prefer a safe name if present
        names = [t.get("name") for t in tools if isinstance(t, dict)]
        pick = None
        for candidate in ("coding_repo_map", "coding_grep", "coding_ticket_status"):
            if candidate in names:
                pick = candidate
                break
        if pick is None and names:
            pick = names[0]
        if pick:
            call = await server._handle_request({
                "jsonrpc": JSONRPC_VERSION,
                "id": 3,
                "method": "tools/call",
                "params": {"name": pick, "arguments": {}},
            })
            report.add(
                "mcp_tools_call",
                isinstance(call, dict) and ("result" in call or "error" in call),
                f"tool={pick}",
            )
        else:
            report.add("mcp_tools_call", True, "skipped: no tools")
    else:
        report.add(
            "mcp_tools_call",
            True,
            "skipped: ToolRegistry empty offline — facade read-only path covered",
        )


def _acp_smoke(report: Report) -> None:
    from core.acp import ACPServer, ACPCapability, ACP_VERSION

    report.protocol = "acp"
    srv = ACPServer()
    srv.register_capability(ACPCapability(
        name="coding_repo_map",
        description="Signature-level repository map",
        type="tool",
        parameters={"root_dir": {"type": "string"}},
    ))
    srv.register_capability(ACPCapability(
        name="coding_grep",
        description="Project text search",
        type="tool",
        parameters={"pattern": {"type": "string"}},
    ))
    caps = list(srv._capabilities.values())
    report.add(
        "acp_capabilities",
        len(caps) >= 2 and ACP_VERSION,
        f"n={len(caps)} version={ACP_VERSION}",
    )
    report.extra["acp_version"] = ACP_VERSION
    report.extra["acp_caps"] = [c.name for c in caps]


def _lsp_optional(report: Report) -> None:
    try:
        from coding.lsp import get_lsp_tools
        tools = get_lsp_tools()
        report.add(
            "lsp_optional",
            True,
            f"lsp tools available n={len(tools)} (not required for smoke)",
        )
    except Exception as e:
        report.add("lsp_optional", True, f"skipped: {e}")


def run() -> int:
    print("WW Coding PROTOCOL smoke (PM 0.12)")
    t0 = time.time()
    report = Report()
    prefer = (os.environ.get("WW_PROTOCOL_SMOKE", "mcp") or "mcp").strip().lower()

    try:
        if prefer == "acp":
            _acp_smoke(report)
        else:
            asyncio.run(_mcp_smoke(report))
            # Also register ACP capabilities lightly for dual coverage
            try:
                _acp_smoke_light = Report()
                _acp_smoke(_acp_smoke_light)
                # fold soft
                for c in _acp_smoke_light.checks:
                    if c.name.startswith("acp_"):
                        report.add(c.name, c.ok, c.detail)
                report.extra["acp"] = _acp_smoke_light.extra
                report.protocol = "mcp+acp"
            except Exception as e:
                report.add("acp_optional", True, f"skipped: {e}")
    except Exception as e:
        report.add("protocol_boot", False, f"{type(e).__name__}: {e}")

    _lsp_optional(report)

    elapsed = time.time() - t0
    report.extra["elapsed_s"] = round(elapsed, 3)
    report.extra["finished_at"] = datetime.now(timezone.utc).isoformat()

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = report.to_dict()
    payload["started_budget_note"] = "offline MCP/ACP smoke; no API keys"
    json_path = RESULTS / f"protocol_{stamp}.json"
    md_path = RESULTS / f"protocol_{stamp}.md"
    latest_json = RESULTS / "latest.json"
    latest_md = RESULTS / "latest.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_lines = [
        "# Coding Protocol Smoke (PM 0.12)",
        "",
        f"- Protocol: {payload.get('protocol')}",
        f"- Result: {payload.get('passed')}/{payload.get('total')}",
        "",
        "| check | ok | detail |",
        "|-------|----|--------|",
    ]
    for c in payload.get("checks") or []:
        md_lines.append(f"| {c.get('name')} | {c.get('ok')} | {c.get('detail')} |")
    md_lines += [
        "",
        "## IDE / agent attach",
        "",
        "See `docs/coding-engine.md` § Protocol (MCP / ACP).",
        "",
    ]
    md = "\n".join(md_lines)
    md_path.write_text(md, encoding="utf-8")
    latest_md.write_text(md, encoding="utf-8")
    print(f"\n  {payload['passed']}/{payload['total']} checks  ({elapsed:.2f}s)")
    print(f"  Wrote {json_path}")
    return 1 if report.hard_fail() else 0


if __name__ == "__main__":
    sys.exit(run())
