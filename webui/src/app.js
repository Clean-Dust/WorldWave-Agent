// Worldwave AI — PWA Web Client v0.2
// Connects to WW server API via same-origin + ?api_key= auth.

const API_KEY = localStorage.getItem('ww_api_key') || '';
const MAX_SPIRALS = 5;

function auth(url) {
    return API_KEY ? url + (url.includes('?') ? '&' : '?') + 'api_key=' + encodeURIComponent(API_KEY) : url;
}

let tools = [];

// ── Init ────────────────────────────────────────────────────────

async function init() {
    const input = document.getElementById('user-input');
    if (input) input.focus();
    await loadTools();
    await checkStatus();
}

async function loadTools() {
    try {
        const res = await fetch(auth('/ww/tools'));
        const data = await res.json();
        tools = data.tools || [];
        renderTools(tools);
        const search = document.getElementById('tool-search');
        if (search) search.placeholder = `Search tools (${tools.length} available)...`;
    } catch (e) {
        setStatus('🔴 Disconnected', 'offline');
    }
}

async function checkStatus() {
    try {
        const res = await fetch(auth('/ww/status'));
        if (res.ok) setStatus('🟢 Connected', 'online');
    } catch (e) {
        setStatus('🔴 Disconnected', 'offline');
    }
}

function setStatus(indicator, badge) {
    const el = document.getElementById('status-indicator');
    const bd = document.getElementById('connection-status');
    if (el) el.textContent = indicator;
    if (bd) bd.textContent = badge;
}

// ── Tools Sidebar ───────────────────────────────────────────────

function renderTools(toolList) {
    const container = document.getElementById('tools-list');
    if (!container) return;
    container.innerHTML = toolList.slice(0, 60).map(t =>
        `<div class="tool-item" title="${escAttr(t.description || '')}">
            ${escHtml(t.name)} <span style="color:var(--primary);font-size:10px">${escHtml(t.category || '')}</span>
        </div>`
    ).join('');
}

function filterTools() {
    const query = (document.getElementById('tool-search')?.value || '').toLowerCase();
    const filtered = query ? tools.filter(t => t.name.toLowerCase().includes(query)) : tools;
    renderTools(filtered);
}

// ── Chat ────────────────────────────────────────────────────────

async function sendMessage() {
    const input = document.getElementById('user-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    appendMessage('user', text);
    showLoading(true);

    try {
        const res = await fetch(auth('/ww/run'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ goal: text, max_spirals: MAX_SPIRALS }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.message || err.error || `HTTP ${res.status}`);
        }
        const data = await res.json();
        const results = data.results || [];
        let response = '';
        for (const r of results) {
            const ev = r.evaluation || {};
            if (ev.reason) {
                response = ev.reason;
            }
            const actions = r.actions || [];
            for (const a of actions) {
                const output = a.result && a.result.output ? a.result.output : '';
                if (output) {
                    response += '\n[' + a.tool + '] ' + output;
                }
            }
        }
        if (!response) {
            response = 'Status: ' + (data.status || '?') + ' (' + (data.spirals_completed || 0) + ' spirals)';
        }
        appendMessage('assistant', response);
    } catch (e) {
        appendMessage('assistant', `❌ ${escHtml(e.message)}`);
    } finally {
        showLoading(false);
    }
}

function appendMessage(role, text) {
    const area = document.getElementById('chat-area');
    if (!area) return;
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    div.textContent = text;
    area.appendChild(div);
    area.scrollTop = area.scrollHeight;
}

function showLoading(show) {
    const el = document.getElementById('loading');
    if (el) el.style.display = show ? 'block' : 'none';
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function escAttr(s) {
    return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Start ───────────────────────────────────────────────────────

init();
setInterval(checkStatus, 30000);
