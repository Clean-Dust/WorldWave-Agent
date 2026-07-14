// Worldwave AI — PWA Web Client v0.3
// Connects to WW server API via same-origin + Bearer / ?api_key= auth.

const API_KEY = (typeof localStorage !== 'undefined' && localStorage.getItem('ww_api_key')) || '';
const MAX_SPIRALS = 5;

/** Query-string fallback (server also accepts Bearer / X-API-Key). */
function auth(url) {
    return API_KEY ? url + (url.includes('?') ? '&' : '?') + 'api_key=' + encodeURIComponent(API_KEY) : url;
}

/** Headers for authenticated fetch (Bearer preferred; query still set by auth()). */
function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (API_KEY) {
        h['Authorization'] = 'Bearer ' + API_KEY;
        h['X-API-Key'] = API_KEY;
    }
    return h;
}

let tools = [];

// ── Reply extraction (mirrors ww_cli.extract_user_response) ─────

/**
 * True if text looks like an internal status leak, not user-facing content.
 * Never surface strings containing "Reflex arc" or "direct response".
 */
function isInternalResponseText(text) {
    if (!text || typeof text !== 'string') return true;
    const s = text.trim();
    if (!s) return true;
    const lower = s.toLowerCase();
    if (lower.includes('reflex arc')) return true;
    if (lower.includes('direct response')) return true;
    if (lower.startsWith('error:')) return true;
    if (lower.includes('traceback')) return true;
    return false;
}

function _cleanReply(val) {
    if (typeof val !== 'string') return '';
    const s = val.trim();
    if (!s || isInternalResponseText(s)) return '';
    return s;
}

/**
 * Best user-facing reply from a /ww/run payload.
 * Priority:
 *   1. Top-level response/reply/output/message
 *   2. Actions with tool reflex_text/respond (result.output|text|response)
 *   3. Any successful action result.output|text|response
 *   4. evaluation.response/summary if not internal
 * Never returns internal leaks. Empty string if nothing usable.
 */
function extractUserResponse(data) {
    if (!data || typeof data !== 'object') return '';

    for (const key of ['response', 'reply', 'output', 'message']) {
        const got = _cleanReply(data[key]);
        if (got) return got;
    }

    const spiralResults = Array.isArray(data.results) ? data.results : [];
    const REPLY_TOOLS = new Set(['reflex_text', 'respond', 'reply', 'final_answer']);

    // Prefer reflex_text / respond-style tools
    for (const r of spiralResults) {
        if (!r || typeof r !== 'object') continue;
        for (const a of (r.actions || [])) {
            if (!a || typeof a !== 'object') continue;
            const tool = String(a.tool || '').toLowerCase();
            if (!REPLY_TOOLS.has(tool)) continue;
            const res = a.result || {};
            if (!res || typeof res !== 'object') continue;
            for (const key of ['output', 'text', 'response']) {
                const got = _cleanReply(res[key]);
                if (got) return got;
            }
        }
    }

    // Any successful action with output/text/response
    for (const r of spiralResults) {
        if (!r || typeof r !== 'object') continue;
        for (const a of (r.actions || [])) {
            if (!a || typeof a !== 'object') continue;
            const res = a.result || {};
            if (!res || typeof res !== 'object') continue;
            if (res.success === false) continue;
            for (const key of ['output', 'text', 'response']) {
                const got = _cleanReply(res[key]);
                if (got) return got;
            }
        }
    }

    // evaluation.response / evaluation.summary (skip internal)
    for (const r of spiralResults) {
        if (!r || typeof r !== 'object') continue;
        const ev = r.evaluation || {};
        if (!ev || typeof ev !== 'object') continue;
        for (const key of ['response', 'summary']) {
            const got = _cleanReply(ev[key]);
            if (got) return got;
        }
    }

    return '';
}

// ── API Key Prompt ──────────────────────────────────────────────

function showKeyPrompt() {
    // Remove any existing overlay
    const old = document.getElementById('key-overlay');
    if (old) old.remove();

    const overlay = document.createElement('div');
    overlay.id = 'key-overlay';
    overlay.innerHTML = `
        <div style="
            position:fixed;inset:0;background:var(--bg);display:flex;
            align-items:center;justify-content:center;z-index:9999;
        ">
            <div style="
                background:var(--surface);padding:32px;border-radius:12px;
                border:1px solid var(--border);max-width:400px;width:90%;
                text-align:center;
            ">
                <h2 style="color:var(--primary);margin-bottom:8px;">⚡ Worldwave AI</h2>
                <p style="color:var(--dim);margin-bottom:16px;">Enter your WW API key to get started</p>
                <input id="key-input" type="password" placeholder="WW_API_KEY" style="
                    width:100%;padding:10px;border-radius:6px;border:1px solid var(--border);
                    background:var(--bg);color:var(--text);font:inherit;margin-bottom:12px;
                ">
                <button id="key-submit" style="
                    width:100%;padding:10px;background:var(--primary);color:#fff;
                    border:none;border-radius:6px;cursor:pointer;font-weight:600;
                ">Connect</button>
                <p style="color:var(--dim);font-size:11px;margin-top:12px;">
                    Find your key with: <code style="background:var(--bg);padding:2px 6px;border-radius:3px;">ww status</code>
                </p>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);
    const input = document.getElementById('key-input');
    const btn = document.getElementById('key-submit');
    if (input) input.focus();
    if (btn) btn.onclick = () => {
        const val = (input && input.value) ? input.value.trim() : '';
        if (val) {
            localStorage.setItem('ww_api_key', val);
            location.reload();
        }
    };
    if (input) input.onkeydown = (e) => {
        if (e.key === 'Enter') btn && btn.click();
    };
}

// ── Init ────────────────────────────────────────────────────────

async function init() {
    const input = document.getElementById('user-input');
    if (input) input.focus();
    await loadTools();
    await checkStatus();
}

async function loadTools() {
    try {
        const res = await fetch(auth('/ww/tools'), { headers: authHeaders() });
        if (res.status === 401) { showKeyPrompt(); return; }
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
        const res = await fetch(auth('/ww/status'), { headers: authHeaders() });
        if (res.status === 401) { showKeyPrompt(); return; }
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
            headers: authHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ goal: text, max_spirals: MAX_SPIRALS }),
        });
        if (res.status === 401) { showKeyPrompt(); showLoading(false); return; }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.message || err.error || `HTTP ${res.status}`);
        }
        const data = await res.json();
        // Mirror ww_cli.extract_user_response — never show "Reflex arc direct response"
        let response = extractUserResponse(data);
        if (!response) {
            response = 'No reply text from server';
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

// ── Start (skip in Node unit checks) ────────────────────────────

if (typeof document !== 'undefined') {
    if (!API_KEY) {
        showKeyPrompt();
    } else {
        init();
        setInterval(checkStatus, 30000);
    }
}

// Node/CommonJS export for unit-like fixture tests (no browser)
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        isInternalResponseText,
        extractUserResponse,
        authHeaders,
        auth,
    };
}
