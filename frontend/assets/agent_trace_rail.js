/* agent_trace_rail.js — live tool-execution trace panel for single-DB chat.
 *
 * Renders a left-side rail listing every tool call as it happens:
 *   memory_tool → router_tool → clarifier → interpreter → <db> → synthesizer → critic
 *
 * Each node shows: tool name, status (pending/running/done/failed),
 * elapsed time, and a short preview of the output.
 *
 * Activates for BOTH single-DB surfaces:
 *   - agentic     ("*_agent", e.g. ttd_agent)  — emits the events via the
 *                 OpenAI-Agents-SDK orchestrator loop.
 *   - deterministic (bare slug, e.g. "ttd")    — the per_db_chat pipeline
 *                 emits the SAME tool_called / tool_result / delta / final
 *                 events (pre-memory, pre-router, pre-interp, the <db> tool,
 *                 synthesizer, post-critic), so the rail renders the
 *                 step-wise progress identically.
 * Multi-DB slugs ("multi", "multi_v2", …) were decommissioned 2026-06-18
 * (their decomp_tree.js plan view moved to decommissioned/); the slug guard
 * below stays as a harmless defensive check.
 */
(function bcAgentTraceRail(){
  "use strict";
  if (window.__bcAgentTraceInit) return;
  window.__bcAgentTraceInit = true;

  const slug = (new URLSearchParams(location.search).get('db') || '')
    .toLowerCase().trim();
  // Activate for single-DB agentic ("*_agent") AND single-DB deterministic
  // (bare slugs like "ttd", "chembl") surfaces — both pipelines emit the
  // same tool_called / tool_result events. Multi-DB slugs ("multi",
  // "multi_v2", …) were decommissioned; the guard below excludes them
  // defensively (they no longer reach this page).
  const isAgentSlug = /^[a-z][a-z0-9_]*_agent$/.test(slug);
  const isDeterministicDbSlug =
    /^[a-z][a-z0-9_]*$/.test(slug) && !/^multi(_|$)/.test(slug);
  if (!isAgentSlug && !isDeterministicDbSlug) return;
  console.info('[agent_trace_rail] activated for slug=' + slug);

  // Pricing per million tokens (input, output) in USD.
  // Keys must match the model name/alias as returned by the LiteLLM proxy.
  const MODEL_PRICING = {
    // ── BioChirp live aliases (see litellm_config.yaml) ───────────────────
    // router / clarifier: openrouter/mistralai/ministral-8b-2512
    'ministral-8b-2512': { in: 0.10, out: 0.10 },
    // synthesizer / critic: groq/llama-3.1-8b-instant
    'llama-3.1-8b-instant': { in: 0.05, out: 0.08 },
    // TTD legacy / gemini-flash-lite aliases
    'gemini-2.5-flash-lite': { in: 0.10, out: 0.40 },
    // ── OpenAI direct ─────────────────────────────────────────────────────
    'gpt-4.1':      { in: 2.00,  out: 8.00  },
    'gpt-4.1-mini': { in: 0.40,  out: 1.60  },
    'gpt-4.1-nano': { in: 0.10,  out: 0.40  },
    'gpt-4o':       { in: 2.50,  out: 10.00 },
    'gpt-4o-mini':  { in: 0.15,  out: 0.60  },
    // ── Anthropic ─────────────────────────────────────────────────────────
    'claude-opus-4-7':   { in: 15.00, out: 75.00 },
    'claude-sonnet-4-6': { in: 3.00,  out: 15.00 },
    'claude-haiku-4-5':  { in: 0.80,  out: 4.00  },
  };

  function tokenCostUsd(tokens){
    if (!tokens || typeof tokens !== 'object') return null;
    const p = MODEL_PRICING[tokens.model] || MODEL_PRICING[
      // fuzzy: match prefix (e.g. "gpt-4.1-nano:20250409" → "gpt-4.1-nano")
      Object.keys(MODEL_PRICING).find(k => String(tokens.model).startsWith(k))
    ];
    if (!p) return null;
    const inCost  = ((tokens.in  || 0) / 1e6) * p.in;
    const outCost = ((tokens.out || 0) / 1e6) * p.out;
    return inCost + outCost;
  }

  // Friendly icons + display names. Tool names are case-insensitive.
  const TOOL_META = {
    memory_tool:   {icon: '🗂️',  label: 'Memory check'},
    router_tool:   {icon: '🧭',  label: 'Router'},
    clarifier:     {icon: '🪄',  label: 'Clarifier'},
    decomposer:    {icon: '🪓',  label: 'Decomposer'},
    interpreter:   {icon: '🧬',  label: 'Interpreter'},
    web:           {icon: '🌐',  label: 'Web search'},
    tavily:        {icon: '🦅',  label: 'Tavily (fallback)'},
    readme:        {icon: '📖',  label: 'Readme'},
    synthesizer:   {icon: '✍️',  label: 'Synthesizer'},
    critic:        {icon: '⚖️',  label: 'Critic'},
  };
  function metaFor(name){
    const k = String(name || '').toLowerCase();
    if (TOOL_META[k]) return TOOL_META[k];
    // Per-DB tool — same as slug stripped of "_agent"
    if (k === slug.replace(/_agent$/, '')) {
      return {icon: '🎯', label: k.toUpperCase() + ' retrieval'};
    }
    return {icon: '🔧', label: name};
  }

  // ── State ────────────────────────────────────────────────────────────
  let panel = null;     // root DOM element
  let listEl = null;    // <ol> inside the panel
  const nodes = new Map();  // tool_id → {state: pending|running|done|failed,
                            //            startedAt, doneAt, preview, name, el}

  // Per-question reset markers — clear nodes when a new question turn
  // starts. `turnEnded` flips true on `final` / `error`; the next
  // `tool_called` then resets the rail. This is needed because the server
  // emits `user_ack` only ONCE per WebSocket connection (at connect), so
  // it cannot mark per-question boundaries — without `turnEnded` the rail
  // keeps stacking every question's nodes for the life of the connection.
  let questionCount = 0;
  let turnEnded = false;
  let turnCostUsd = 0;  // running LLM cost ($) for the current turn

  // ── DOM ──────────────────────────────────────────────────────────────
  function ensurePanel(){
    if (panel) return;
    // Reserve the 340px layout gutter (agent_trace_rail.css) only once the
    // rail actually has something to show — setting this at script-init
    // time (regardless of whether any trace ever arrives) left every fresh
    // chat page with a permanent empty gray strip on desktop.
    document.documentElement.setAttribute('data-bc-agent-trace-active', '1');
    panel = document.createElement('aside');
    panel.className = 'bc-agent-rail bc-agent-panel';
    panel.innerHTML = `
      <div class="bc-agent-header">
        <span class="bc-agent-icon">🤖</span>
        <span class="bc-agent-title">Agent trace</span>
        <span class="bc-agent-state">running</span>
      </div>
      <ol class="bc-agent-list"></ol>
      <div class="bc-agent-footer">
        Tools execute top-to-bottom. Click a node to scroll to its card in the chat.
      </div>
    `;
    document.body.appendChild(panel);
    listEl = panel.querySelector('.bc-agent-list');
  }

  function resetPanel(){
    nodes.clear();
    turnCostUsd = 0;
    ensurePanel();
    if (listEl) listEl.innerHTML = '';
    // Drop the completed-turn styling — resetPanel now runs mid-session
    // (between questions), not just at connect, so `bc-state-done` from
    // the previous turn must be cleared or the rail looks "completed".
    panel.classList.remove('bc-state-done');
    const st = panel.querySelector('.bc-agent-state');
    if (st) { st.textContent = 'running'; st.className = 'bc-agent-state'; }
    _updateFooterCost();
  }

  function _updateFooterCost(){
    const footer = panel && panel.querySelector('.bc-agent-footer');
    if (!footer) return;
    if (turnCostUsd > 0) {
      const cents = turnCostUsd * 100;
      const label = cents < 0.1
        ? '<$0.001'
        : '$' + turnCostUsd.toFixed(cents < 1 ? 4 : 2);
      footer.innerHTML = `Turn cost: <strong>${label}</strong> &nbsp;·&nbsp; Click a node to scroll to its card.`;
    } else {
      footer.textContent = 'Tools execute top-to-bottom. Click a node to scroll to its card in the chat.';
    }
  }

  function escHtml(s){
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function nodeHtml(toolId, name, meta){
    return `<li class="bc-agent-node state-pending" data-tool-id="${escHtml(toolId)}">
      <div class="bc-agent-row">
        <span class="bc-agent-bullet"></span>
        <span class="bc-agent-emoji">${meta.icon}</span>
        <span class="bc-agent-name">${escHtml(meta.label)}</span>
        <span class="bc-agent-time" data-time></span>
      </div>
      <div class="bc-agent-preview" data-preview></div>
    </li>`;
  }

  function addNode(toolId, name){
    if (!listEl) ensurePanel();
    if (nodes.has(toolId)) return nodes.get(toolId);
    const meta = metaFor(name);
    const tpl = document.createElement('div');
    tpl.innerHTML = nodeHtml(toolId, name, meta);
    const el = tpl.firstElementChild;
    listEl.appendChild(el);
    const node = {
      state: 'running',
      startedAt: Date.now(),
      doneAt: null,
      preview: '',
      name: name,
      el: el,
    };
    el.classList.remove('state-pending');
    el.classList.add('state-running');
    nodes.set(toolId, node);
    return node;
  }

  function appendPreview(toolId, text){
    const node = nodes.get(toolId);
    if (!node) return;
    if (!text) return;
    // Keep preview short — first ~200 chars of cumulative text.
    node.preview = (node.preview + text).slice(0, 400);
    const pv = node.el.querySelector('[data-preview]');
    if (pv) {
      // Render minimal markdown safely (bold, code) — full markdown is in
      // the main chat card; here we just want a compact glance.
      const cleaned = node.preview
        .replace(/\n+/g, ' ')
        .slice(0, 180);
      pv.textContent = cleaned + (node.preview.length > 180 ? '…' : '');
    }
  }

  function finishNode(toolId, ok, tokens){
    const node = nodes.get(toolId);
    if (!node) return;
    node.doneAt = Date.now();
    node.state = ok === false ? 'failed' : 'done';
    node.el.classList.remove('state-running', 'state-pending');
    node.el.classList.add('state-' + node.state);
    const t = node.el.querySelector('[data-time]');
    if (t) {
      const ms = node.doneAt - node.startedAt;
      const timeStr = ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(2) + ' s';
      const cost = tokenCostUsd(tokens);
      if (cost != null) {
        turnCostUsd += cost;
        const cents = cost * 100;
        const costStr = cents < 0.01
          ? '<$0.0001'
          : '$' + cost.toFixed(cents < 0.1 ? 5 : cents < 1 ? 4 : 2);
        const tokStr = tokens
          ? ` (${tokens.in || 0}↑ ${tokens.out || 0}↓)`
          : '';
        t.textContent = timeStr + ' · ' + costStr + tokStr;
        _updateFooterCost();
      } else {
        t.textContent = timeStr;
      }
    }
  }

  function markPanelDone(){
    if (!panel) return;
    panel.classList.add('bc-state-done');
    const st = panel.querySelector('.bc-agent-state');
    if (st) { st.textContent = 'completed'; }
  }

  // ── WebSocket interception ───────────────────────────────────────────
  // ws-auth-shim.js (loaded before this script on db_chat.html) wraps
  // window.WebSocket with a PatchedWebSocket that only supports on*
  // property setters — ws.addEventListener is NOT available on it.
  // Chain via onmessage setter in setTimeout(0) so chat-main.js sets its
  // handler first; our handler fires after chat-main.js processes each
  // message, keeping both fully independent.
  const _OrigWS = window.WebSocket;
  window.WebSocket = function(url, protocols){
    const ws = protocols != null ? new _OrigWS(url, protocols) : new _OrigWS(url);
    setTimeout(function(){
      const prev = ws.onmessage;
      ws.onmessage = function(ev){
        if (prev) prev.call(this, ev);
        try { handleEvent(JSON.parse(ev.data)); } catch(_){}
      };
    }, 0);
    return ws;
  };
  window.WebSocket.CONNECTING = _OrigWS.CONNECTING;
  window.WebSocket.OPEN       = _OrigWS.OPEN;
  window.WebSocket.CLOSING    = _OrigWS.CLOSING;
  window.WebSocket.CLOSED     = _OrigWS.CLOSED;
  window.WebSocket.prototype  = _OrigWS.prototype;

  function handleEvent(m){
    if (!m || typeof m !== 'object') return;
    const t = m.type;
    if (!t) return;

    if (t === 'user_ack') {
      // WS connection established (sent once per connection). Reset the
      // panel for the first question; later questions reset via the
      // `turnEnded` flag on their first `tool_called` below.
      questionCount++;
      resetPanel();
      turnEnded = false;
      return;
    }
    if (t === 'tool_called') {
      // A `tool_called` arriving after the previous turn's `final` is the
      // first step of a NEW question — reset so the rail shows ONLY the
      // current question's trace.
      if (turnEnded) { resetPanel(); turnEnded = false; questionCount++; }
      addNode(m.tool_id, m.name);
      return;
    }
    if (t === 'delta') {
      appendPreview(m.tool_id, m.text || '');
      return;
    }
    if (t === 'tool_result') {
      finishNode(m.tool_id, m.ok !== false, m.tokens || null);
      return;
    }
    if (t === 'final') {
      markPanelDone();
      _updateFooterCost();
      // Turn complete — the next `tool_called` belongs to a new question
      // and must reset the rail first.
      turnEnded = true;
      return;
    }
    if (t === 'error') {
      // Mark any still-running node as failed.
      for (const [tid, node] of nodes.entries()){
        if (node.state === 'running') finishNode(tid, false);
      }
      markPanelDone();
      turnEnded = true;
      return;
    }
  }

  // Click handler — scroll to the tool card in the chat.
  // R10 (2026-05-21): scope the `querySelector` to the messages
  // container instead of `document`. The chat transcript can grow to
  // many hundreds of tool nodes over a long session; searching the
  // whole DOM on every click was a measurable perf hit on Firefox.
  // The messages container is created by db_chat.html as
  // `#messagesContainer`; fall back to document if it ever isn't.
  document.addEventListener('click', (ev) => {
    const li = ev.target.closest('.bc-agent-node[data-tool-id]');
    if (!li) return;
    const tid = li.getAttribute('data-tool-id');
    if (!tid) return;
    const scope = document.getElementById('messagesContainer') || document;
    const card = scope.querySelector(`[data-tool-id="${CSS.escape(tid)}"]`);
    if (card) card.scrollIntoView({behavior: 'smooth', block: 'center'});
  });
})();
