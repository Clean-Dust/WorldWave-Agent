"""
ww/core/dashboard.py — WW observability Dashboard v0.1

Lightweight local Web Dashboard:
- LEARN loop visualization (each node state)
- Tool call trace
- Token consumption monitor
- Memory system health
- Depth checkpoint browsing

embedding FastAPI server. 
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

# ── Dashboard HTML ──

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WW Dashboard</title>
<style>
  /* 🌙 WW Dark Theme Dashboard */
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --purple: #bc8cff;
    --cyan: #39d2c0;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 20px;
    max-width: 1400px; margin: 0 auto;
  }
  h1 { font-size: 1.8rem; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; }
  h1 span { font-size: 0.9rem; color: var(--text-muted); font-weight: 400; }
  h2 { font-size: 1.1rem; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  h2 small { font-size: 0.7rem; color: var(--text-muted); font-weight: 400; }
  .subtitle { color: var(--text-muted); font-size: 0.85rem; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; position: relative;
  }
  .card h3 { font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 12px; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status-dot.green { background: var(--green); }
  .status-dot.yellow { background: var(--yellow); }
  .status-dot.red { background: var(--red); }
  .status-dot.purple { background: var(--purple); }
  .status-dot.cyan { background: var(--cyan); }

  /* Phase badges */
  .phase-badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 0.75rem; font-weight: 600; margin-right: 4px;
  }
  .phase-perceive { background: #1f2937; color: #60a5fa; border: 1px solid #2563eb; }
  .phase-recall { background: #1f2937; color: #a78bfa; border: 1px solid #7c3aed; }
  .phase-plan { background: #1f2937; color: #34d399; border: 1px solid #059669; }
  .phase-act { background: #1f2937; color: #fbbf24; border: 1px solid #d97706; }
  .phase-evaluate { background: #1f2937; color: #f472b6; border: 1px solid #db2777; }
  .phase-learn { background: #1f2937; color: #38bdf8; border: 1px solid #0284c7; }
  .phase-idle { background: #1f2937; color: #9ca3af; border: 1px solid #4b5563; }

  /* Tool call table */
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th { text-align: left; color: var(--text-muted); font-weight: 500;
       padding: 6px 4px; border-bottom: 1px solid var(--border); }
  td { padding: 5px 4px; border-bottom: 1px solid #21262d; }
  .tool-name { font-family: 'Fira Code', 'Cascadia Code', monospace; font-size: 0.75rem; }
  .tool-ok { color: var(--green); }
  .tool-fail { color: var(--red); }
  .progress-bar {
    width: 100%; height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; margin-top: 4px;
  }
  .progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
  .progress-fill.green { background: var(--green); }
  .progress-fill.yellow { background: var(--yellow); }
  .progress-fill.cyan { background: var(--cyan); }

  .stat { display: flex; justify-content: space-between; padding: 4px 0; font-size: 0.85rem; }
  .stat-value { font-family: 'Fira Code', monospace; font-weight: 600; }

  /* Memory block visual */
  .mem-blocks { display: flex; gap: 2px; margin-top: 8px; flex-wrap: wrap; }
  .mem-block { width: 12px; height: 12px; border-radius: 2px; transition: 0.3s; }
  .mem-block.used { background: var(--accent); }
  .mem-block.empty { background: #21262d; }
  .mem-block.archived { background: var(--yellow); opacity: 0.5; }

  /* Session list */
  .session-item {
    padding: 8px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 6px;
    cursor: pointer; font-size: 0.85rem;
  }
  .session-item:hover { border-color: var(--accent); }
  .session-goal { color: var(--text); margin-bottom: 2px; }
  .session-meta { color: var(--text-muted); font-size: 0.75rem; display: flex; gap: 12px; }

  .section { margin-top: 24px; }
  .empty-state { color: var(--text-muted); font-size: 0.85rem; text-align: center; padding: 20px; }

  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .running { animation: pulse 2s infinite; }

  .flex-row { display: flex; align-items: center; gap: 8px; }
</style>
</head>
<body>

<h1>🌊 Worldwave <span>v0.5</span></h1>
<div class="subtitle">
  <span id="hostname">—</span> · 
  <span>Spiral <strong id="spiral-num">0</strong></span> · 
  <span id="refresh-time"></span>
</div>

<div class="grid">
  <!-- LEARN Cycle -->
  <div class="card" style="grid-column: span 2;">
    <h3>🔄 LEARN loop</h3>
    <div style="display: flex; gap: 6px; flex-wrap: wrap;" id="phase-list">
      <span class="phase-badge phase-perceive">① Perceive PERCEIVE</span>
      <span class="phase-badge phase-recall">② recall RECALL</span>
      <span class="phase-badge phase-plan">③ Plan PLAN</span>
      <span class="phase-badge phase-act">④ Act ACT</span>
      <span class="phase-badge phase-evaluate">⑤ evaluate EVALUATE</span>
      <span class="phase-badge phase-learn">⑥ Learn LEARN</span>
    </div>
    <div style="margin-top: 12px;">
      <div class="stat"><span>when  phase</span><span id="current-phase" class="stat-value phase-idle" style="border: none;">idle</span></div>
      <div class="stat"><span> Execute Spirals</span><span id="spirals-done" class="stat-value">0</span></div>
      <div class="stat"><span>Step Progress</span><span id="step-progress" class="stat-value">0/0</span></div>
      <div class="progress-bar"><div id="step-bar" class="progress-fill" style="width: 0%"></div></div>
    </div>
  </div>

  <!-- System Status -->
  <div class="card">
    <h3>⚙️ systemstate</h3>
    <div id="sys-status">
      <div class="stat"><span>Run State</span><span id="running-status" class="stat-value"><span class="status-dot green"></span>running</span></div>
      <div class="stat"><span>availabletool</span><span id="tool-count" class="stat-value">0</span></div>
      <div class="stat"><span>Register Model</span><span id="model-count" class="stat-value">—</span></div>
      <div class="stat"><span>API latency</span><span id="api-latency" class="stat-value">—</span></div>
    </div>
  </div>

  <!-- Memory Health -->
  <div class="card">
    <h3>🧠 Memory System</h3>
    <div id="mem-status">
      <div class="stat"><span>hippocampuscapacity</span><span id="hippocampus-usage" class="stat-value">—</span></div>
      <div class="stat"><span>Fact Library Entries</span><span id="memory-count" class="stat-value">—</span></div>
      <div class="stat"><span>Last Consolidation</span><span id="last-sleep" class="stat-value">—</span></div>
      <div class="mem-blocks" id="mem-blocks"></div>
    </div>
  </div>
</div>

<div class="grid">
  <!-- Tool Calls -->
  <div class="card" style="grid-column: span 2;">
    <h3>🔧 Recent Tool Calls</h3>
    <div id="tool-history">
      <div class="empty-state">Waiting for tool calls…</div>
    </div>
  </div>

  <!-- Token Usage -->
  <div class="card">
    <h3>💰 Token Consumption</h3>
    <div id="token-usage">
      <div class="stat"><span>This Round</span><span id="token-round" class="stat-value">—</span></div>
      <div class="stat"><span>Total</span><span id="token-total" class="stat-value">—</span></div>
      <div class="stat"><span>LLM Call Count</span><span id="llm-calls" class="stat-value">—</span></div>
    </div>
  </div>
</div>

<!-- Sessions Section -->
<div class="section">
  <h2>📋 Recent Sessions <small id="session-count"></small></h2>
  <div id="session-list"><div class="empty-state">load …</div></div>
</div>

<script>
// ── Tool functions ──
async function fetchJSON(url) {
    const r = await fetch(url);
    return await r.json();
  } catch { return null; }
}

async function loadStatus() {
  const app = (await fetchJSON('/ww/status')) || {};
  const sys = app.system || app.data || app;
  
  // System info
  document.getElementById('hostname').textContent = sys.hostname || '-';
  
  // LEARN cycle
  let phase = (app.current_phase || 'idle').toLowerCase();
  document.getElementById('current-phase').textContent = phase;
  document.getElementById('current-phase').className = 'stat-value phase-' + phase;
  document.getElementById('spiral-num').textContent = app.current_spiral || 0;
  document.getElementById('spirals-done').textContent = app.current_spiral || 0;

  // Progress
  const stepTotal = app.steps_total || 0;
  const stepDone = app.steps_completed || 0;
  document.getElementById('step-progress').textContent = stepDone + '/' + stepTotal;
  const pct = stepTotal > 0 ? (stepDone / stepTotal * 100) : 0;
  document.getElementById('step-bar').style.width = pct + '%';
  document.getElementById('step-bar').className = 'progress-fill ' + (pct >= 100 ? 'green' : pct > 0 ? 'yellow' : '');

  // Running status
  const runEl = document.getElementById('running-status');
  if (app.running) {
    runEl.innerHTML = '<span class="status-dot green running"></span>running';
  } else {
    runEl.innerHTML = '<span class="status-dot"></span>idle';
  }

  // Memory blocks (hippocampus visualization)
  const memBlocks = document.getElementById('mem-blocks');
  const hippoUsed = app.hippocampus_used || 0;
  const hippoCap = app.hippocampus_capacity || 100;
  const blocks = [];
  const ratio = hippoCap > 0 ? hippoUsed / hippoCap : 0;
  const totalBlocks = 20;
  const usedBlocks = Math.round(ratio * totalBlocks);
  for (let i = 0; i < totalBlocks; i++) {
    const cls = i < usedBlocks ? 'mem-block used' : 'mem-block empty';
    blocks.push('<div class="' + cls + '"></div>');
  }
  memBlocks.innerHTML = blocks.join('');
  document.getElementById('hippocampus-usage').textContent = hippoUsed + '/' + hippoCap;
  document.getElementById('memory-count').textContent = app.memory_count || '-';
  document.getElementById('last-sleep').textContent = app.last_sleep || '-';

  // Tool count
  document.getElementById('tool-count').textContent = app.tool_count || '-';
  document.getElementById('model-count').textContent = app.model_count || '-';
  document.getElementById('api-latency').textContent = app.api_latency || '-';

  // Token usage
  document.getElementById('token-round').textContent = app.token_round || '-';
  document.getElementById('token-total').textContent = app.token_total || '-';
  document.getElementById('llm-calls').textContent = app.llm_calls || '-';

  // Refresh time
  document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString();
}

async function loadToolHistory() {
  // Try to get logs
  const logs = await fetchJSON('/ww/logs?source=tools&limit=20');
  if (!logs || !logs.entries || logs.entries.length === 0) {
    document.getElementById('tool-history').innerHTML = '<div class="empty-state">No tool call records yet</div>';
    return;
  }
  
  const rows = logs.entries.slice(0, 15).map(e => {
    const data = e.data || {};
    const tool = data.tool || e.message || '?';
    const success = data.success;
    const latency = data.latency ? (data.latency.toFixed(1) + 's') : '';
    const statusClass = success === false ? 'tool-fail' : 'tool-ok';
    const statusIcon = success === false ? '✗' : '✓';
    return '<tr><td class="tool-name">' + tool.slice(0, 25) + '</td>'
         + '<td><span class="' + statusClass + '">' + statusIcon + '</span></td>'
         + '<td>' + latency + '</td>'
         + '<td>' + (e.timestamp || '').slice(11, 19) + '</td></tr>';
  }).join('');
  
  document.getElementById('tool-history').innerHTML =
    '<table><tr><th>tool</th><th>state</th><th>latency</th><th>  </th></tr>' + rows + '</table>';
}

async function loadSessions() {
  const data = await fetchJSON('/ww/sessions?limit=10');
  if (!data || !data.sessions || data.sessions.length === 0) {
    document.getElementById('session-list').innerHTML = '<div class="empty-state">No sessions yet</div>';
    document.getElementById('session-count').textContent = '';
    return;
  }
  document.getElementById('session-count').textContent = '(' + data.count + ' total)';
  const items = data.sessions.map(s => {
    const goal = (s.goal || '').slice(0, 60);
    const meta = s.updated_at ? s.updated_at.slice(0, 19).replace('T', ' ') : '';
    const status = s.status || '?';
    return '<div class="session-item">'
         + '<div class="session-goal">' + goal + '</div>'
         + '<div class="session-meta">'
         + '<span>🆔 ' + s.session_id.slice(0, 12) + '</span>'
         + '<span>🌀 ' + (s.spirals_completed || 0) + ' spirals</span>'
         + '<span>📌 ' + status + '</span>'
         + '<span>' + meta + '</span>'
         + '</div></div>';
  }).join('');
  document.getElementById('session-list').innerHTML = items;
}

/* ── SSE / auto refresh ── */
let sseConnected = false;
function connectSSE() {
  if (typeof EventSource === 'undefined') {
    // Does not support SSE browser, fallback to polling
    setInterval(refresh, 5000);
    return;
  }
  const evtSource = new EventSource('/ww/dashboard/stream');
  evtSource.onmessage = function(e) {
    try {
      const state = JSON.parse(e.data);
      updateDashboard(state);
    } catch (err) {
      console.warn('SSE parse error:', err);
    }
    sseConnected = true;
  };
  evtSource.onerror = function() {
    sseConnected = false;
    console.warn('SSE disconnected, falling back to polling');
    // fallback
    if (!window._sseFallback) {
      window._sseFallback = setInterval(refresh, 5000);
    }
  };
  // Successful connection, clear fallback
  evtSource.onopen = function() {
    if (window._sseFallback) {
      clearInterval(window._sseFallback);
      window._sseFallback = null;
    }
    sseConnected = true;
  };
}
function updateDashboard(state) {
  document.getElementById('current-phase').textContent = state.phase || '?';
  document.getElementById('current-spiral').textContent = state.spiral || 0;
  document.getElementById('steps-completed').textContent = state.steps_completed || 0;
  document.getElementById('steps-total').textContent = state.steps_total || 0;
  const pct = state.steps_total > 0 ? Math.round(state.steps_completed / state.steps_total * 100) : 0;
  document.getElementById('steps-pct').textContent = pct + '%';
  if (state.hippocampus_used !== undefined) {
    document.getElementById('hip-used').textContent = state.hippocampus_used;
    document.getElementById('hip-capacity').textContent = '(' + state.hippocampus_capacity + ')';
  }
  document.getElementById('tool-count').textContent = state.tool_count || 0;
  document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString();
}

async function refresh() {
  await Promise.all([loadStatus(), loadToolHistory(), loadSessions()]);
}

// Start: first connect SSE, then do a complete refresh
window.addEventListener('DOMContentLoaded', function() {
  refresh();
  connectSSE();
});
</script>
</body>
</html>
"""


def create_dashboard_router() -> APIRouter:
    """create Dashboard route. """
    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return DASHBOARD_HTML

    return router
