// Worldwave AI — PWA Web Client
// Connects to WW server API for chat, tool execution, and diff display.

const API_BASE = localStorage.getItem('ww_server') || 'http://localhost:9300';
const API_KEY = localStorage.getItem('ww_api_key') || '';

let tools = [];
let sessions = [];

// ── Init ────────────────────────────────────────────────────────

async function init() {
    document.getElementById('user-input').focus();
    await loadTools();
    await checkStatus();
}

async function loadTools() {
    try {
        const res = await fetch(`${API_BASE}/tools/list`, {
            headers: apiHeaders(),
        });
        const data = await res.json();
        tools = data.tools || [];
        renderTools(tools);
        document.getElementById('tool-search').placeholder = `Search tools (${tools.length} available)...`;
    } catch (e) {
        document.getElementById('status-indicator').textContent = '🔴 Disconnected';
        document.getElementById('connection-status').textContent = 'offline';
    }
}

async function checkStatus() {
    try {
        const res = await fetch(`${API_BASE}/status`, { headers: apiHeaders() });
        if (res.ok) {
            document.getElementById('status-indicator').textContent = '🟢 Connected';
            document.getElementById('connection-status').textContent = 'online';
        }
    } catch (e) {
        document.getElementById('status-indicator').textContent = '🔴 Disconnected';
        document.getElementById('connection-status').textContent = 'offline';
    }
}

function apiHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (API_KEY) h['x-api-key'] = API_KEY;
    return h;
}

// ── Tools Sidebar ───────────────────────────────────────────────

function renderTools(toolList) {
    const container = document.getElementById('tools-list');
    container.innerHTML = toolList.slice(0, 50).map(t =>
        `<div class="tool-item" onclick="runTool('${t.name}')" title="${t.description || ''}">
            ${t.name} <span style="color:var(--primary);font-size:10px">${t.category || ''}</span>
        </div>`
    ).join('');
}

function filterTools() {
    const query = document.getElementById('tool-search').value.toLowerCase();
    const filtered = query ? tools.filter(t => t.name.toLowerCase().includes(query)) : tools;
    renderTools(filtered);
}

async function runTool(name) {
    const params = prompt(`Parameters for ${name} (JSON):`, '{}');
    if (!params) return;
    try {
        const parsed = JSON.parse(params);
        appendMessage('user', `Run: ${name}(${JSON.stringify(parsed)})`);
        showLoading(true);
        const res = await fetch(`${API_BASE}/tools/call`, {
            method: 'POST',
            headers: apiHeaders(),
            body: JSON.stringify({ tool: name, params: parsed }),
        });
        const data = await res.json();
        appendMessage('assistant', formatToolResult(data));
    } catch (e) {
        appendMessage('assistant', `Error: ${e.message}`);
    } finally {
        showLoading(false);
    }
}

function formatToolResult(data) {
    if (data.error) return `❌ ${data.error}`;

    // Check if there's an ANSI diff
    if (data.ansi_diff) {
        return renderDiff(data.ansi_diff, data.stats);
    }

    // Check for side-by-side diff
    if (data.side_by_side) {
        return `<pre style="font-size:11px">${escapeHtml(data.side_by_side)}</pre>`;
    }

    // General result
    const text = JSON.stringify(data, null, 2);
    if (text.length > 3000) {
        return `<pre>${escapeHtml(text.slice(0, 3000))}\n... (truncated)</pre>`;
    }
    return `<pre>${escapeHtml(text)}</pre>`;
}

function renderDiff(ansiDiff, stats) {
    // Strip ANSI codes and convert to HTML
    const clean = ansiDiff
        .replace(/\x1b\[[0-9;]*m/g, '')
        .split('\n')
        .map(line => {
            if (line.startsWith('---')) return `<div class="diff-header">${escapeHtml(line)}</div>`;
            if (line.startsWith('+++')) return `<div class="diff-header">${escapeHtml(line)}</div>`;
            if (line.startsWith('@@')) return `<div class="diff-header">${escapeHtml(line)}</div>`;
            if (line.startsWith('- ')) return `<div class="diff-rem diff-hunk">${escapeHtml(line)}</div>`;
            if (line.startsWith('+ ')) return `<div class="diff-add diff-hunk">${escapeHtml(line)}</div>`;
            if (line.startsWith('──')) return `<div style="color:var(--dim);padding:4px 12px;font-size:11px">${escapeHtml(line)}</div>`;
            return `<div class="diff-hunk">${escapeHtml(line)}</div>`;
        })
        .join('');

    const summary = stats ? `+${stats.added || 0} -${stats.removed || 0}` : '';
    return `<div class="diff-container">${clean}</div>${summary ? `<div style="font-size:11px;color:var(--dim);margin-top:4px">${summary}</div>` : ''}`;
}

// ── Chat ────────────────────────────────────────────────────────

async function sendMessage() {
    const input = document.getElementById('user-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    appendMessage('user', text);
    showLoading(true);

    try {
        const res = await fetch(`${API_BASE}/chat`, {
            method: 'POST',
            headers: apiHeaders(),
            body: JSON.stringify({ message: text }),
        });
        const data = await res.json();
        const response = data.response || data.message || JSON.stringify(data);
        appendMessage('assistant', response);
    } catch (e) {
        appendMessage('assistant', `Error: ${e.message}`);
    } finally {
        showLoading(false);
    }
}

function appendMessage(role, text) {
    const area = document.getElementById('chat-area');
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    div.innerHTML = text.replace(/\n/g, '<br>');
    area.appendChild(div);
    area.scrollTop = area.scrollHeight;
}

function showLoading(show) {
    document.getElementById('loading').style.display = show ? 'block' : 'none';
}

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

// ── Start ───────────────────────────────────────────────────────

init();

// Periodic status check
setInterval(checkStatus, 30000);

// Register service worker for PWA
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
}
