  // WS endpoint is set synchronously by chat-bootstrap.js (window.__BC_WS_URL),
  // which redirects to the picker for any slug without a live chat — so there is
  // no fallback here (the multi-DB /bio_chat/ backend was decommissioned).
  const WS_URL = window.__BC_WS_URL || '';
  // const WS_URL = 'ws://localhost:8055/chembl_chat'

  // 2026-05-17: declare the deep-link-query holder. It was previously used
  // as an implicit global (write at line ~1640, read at ~355) which throws
  // ReferenceError in strict-ish JS contexts and kills the WS handler.
  let initialQFromURL = null;

  marked.setOptions({
    gfm:true, breaks:true, mangle:false, headerIds:false,
    highlight:(code,lang)=>{ try{return hljs.highlight(code,{language:lang}).value;}catch{return hljs.highlightAuto(code).value;} }
  });

  let ws=null, pingInterval=null;
  // WebSocket reconnect-with-backoff. The old code retried at a fixed 1500 ms
  // with no user-facing message and no offline detection — a flaky network or
  // a server restart looked indistinguishable from a hung query.
  let _reconnectAttempts = 0;
  let _reconnectTimer = null;
  let _reconnectCountdownTimer = null;
  let _suppressReconnect = false;  // set by manual reconnect to skip backoff
  const _RECONNECT_MIN_MS  = 1500;
  const _RECONNECT_MAX_MS  = 30000;
  let currentAssistantMessage=null;
  // Container for the current question + its answer (user message stays
  // sticky-pinned at the top of the scroll viewport for the whole turn).
  let currentTurn=null;
  let currentToolCards=new Map();
  let toolBuffers=new Map();
  let toolCount=0;

  const toolStartTimes = new Map();
  const toolElapsed = new Map();

  let orchestratorDone=false;
  let isStreaming=false;

  let orchestratorBuf='';
  let suppressOrchStream=false;

  let pendingUI = new Map();
  let rafScheduled = false;

  const primaryByName = new Map();   // name -> primary tool_id
  const aliasToPrimary = new Map();  // aliasId -> primaryId
  const nameKey = (n)=> String(n||'tool').trim().toLowerCase();
  const canonicalId = (raw)=> aliasToPrimary.get(raw) || raw || 'tool';
  let orchVisibleStreamed = false;
  let fauxOrchTimer = null;
  const FAUX_ORCH_CHARS_PER_TICK = 24;
  const FAUX_ORCH_TICK_MS = 18;

  function stopFauxOrchStream(){
    if (fauxOrchTimer) {
      clearInterval(fauxOrchTimer);
      fauxOrchTimer = null;
    }
  }

  function appendCursor(bubble){
    const cursor = document.createElement('span');
    cursor.className = 'cursor';
    cursor.style.display = 'inline-block';
    cursor.style.width = '2px';
    cursor.style.height = '1em';
    cursor.style.verticalAlign = 'text-bottom';
    cursor.style.background = 'var(--accent)';
    cursor.style.marginLeft = '2px';
    cursor.style.animation = 'blink 1s infinite';
    bubble.appendChild(cursor);
  }

  function renderOrchBubbleText(bubble, text, withCursor=false){
    if (typeof setMarkdown === 'function') {
      setMarkdown(bubble, text || '');
    } else {
      bubble.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
    }
    if (withCursor) appendCursor(bubble);
    bubble.querySelectorAll('pre code').forEach(c => hljs.highlightElement(c));
  }

  function fakeStreamOrchFromFullText(fullText){
    stopFauxOrchStream();
    if (!currentAssistantMessage) currentAssistantMessage = createAssistant();
    const bubble = mainBubble(currentAssistantMessage);
    const text = String(fullText || '');
    if (!text) {
      renderOrchBubbleText(bubble, '', false);
      orchestratorBuf = '';
      orchVisibleStreamed = false;
      orchestratorDone = true;
      maybeFinishStreaming();
      return;
    }

    let idx = 0;
    bubble.textContent = '';
    fauxOrchTimer = setInterval(() => {
      idx = Math.min(text.length, idx + FAUX_ORCH_CHARS_PER_TICK);
      bubble.textContent = text.slice(0, idx);
      appendCursor(bubble);
      scrollEnd();
      if (idx >= text.length) {
        stopFauxOrchStream();
        renderOrchBubbleText(bubble, text, false);
        orchestratorBuf = '';
        orchVisibleStreamed = false;
        orchestratorDone = true;
        maybeFinishStreaming();
        scrollEnd();
      }
    }, FAUX_ORCH_TICK_MS);
  }

  const activeSessionId = 'sess_' + Array.from(crypto.getRandomValues(new Uint8Array(4)), b => b.toString(16).padStart(2,'0')).join('');

  // ── Few-shot feedback state (reset each turn in kickOffStream) ────────────
  const _currentDB = (() => {
    const m = WS_URL.match(/\/(\w+)_chat/);
    return m ? m[1].toLowerCase() : '';
  })();
  let _currentParsedValue   = {};
  let _currentRephrasedQuery = '';

  // /feedback is served at the per-DB tool's ROOT, reachable only through the
  // REST proxy (nginx `/services/<db>/` strips the prefix) — NOT through the
  // `/<db>_chat/` WebSocket proxy (which forwards the URI unchanged and 404s).
  // opentarget has no /feedback endpoint, so _currentDB === '' there and this
  // stays empty, which _postFeedback already guards against.
  const _feedbackBaseUrl = _currentDB
    ? `${window.location.origin}/services/${_currentDB}`
    : '';

  function _postFeedback(turn, verdict) {
    const query = (turn && turn.dataset && turn.dataset.userQuestion) || '';
    if (!query || !_currentDB || !_feedbackBaseUrl) return;
    fetch(_feedbackBaseUrl + '/feedback', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id:      activeSessionId,
        db:              _currentDB,
        query:           query,
        rephrased_query: _currentRephrasedQuery || query,
        parsed_value:    _currentParsedValue,
        verdict:         verdict,
      }),
    }).catch(() => {});  // fire-and-forget; never block the UI
  }

  // Global handler wired via event-delegation.js ACTION_MAP.
  window.feedbackVoteAction = function(btn) {
    const verdict = btn.dataset && btn.dataset.verdict;
    if (!verdict) return;
    const turn = btn.closest('.turn');
    if (!turn) return;

    // Visual confirmation + disable both buttons (one vote per turn).
    const row = btn.closest('.bc-turn-actions');
    if (row) {
      row.querySelectorAll('.bc-feedback-btn').forEach(b => {
        b.disabled = true;
        b.classList.remove('bc-feedback-active');
      });
      btn.classList.add('bc-feedback-active');
      const status = row.querySelector('.bc-turn-action-status');
      if (status) status.textContent = verdict === 'up' ? 'Thanks! Logged ✓' : 'Noted ✓';
    }
    _postFeedback(turn, verdict);
  };

  function setStatus(t,ok){
    document.getElementById('connectionStatus').textContent=t;
    document.getElementById('statusDot').style.background=ok?'var(--ok)':'var(--err)';
  }

  function _ensureConnBanner(){
    let el = document.getElementById('bcConnBanner');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'bcConnBanner';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.style.cssText =
      'position:fixed;top:0;left:0;right:0;z-index:9999;padding:8px 14px;'
      + 'background:var(--err,#b91c1c);color:#fff;font-size:13px;text-align:center;'
      + 'box-shadow:0 1px 4px rgba(0,0,0,.25);display:none';
    document.body.appendChild(el);
    return el;
  }

  function showConnBanner(text, kind){
    const el = _ensureConnBanner();
    if (kind === 'warn') {
      el.style.background = 'var(--warn,#b45309)';
    } else if (kind === 'ok') {
      el.style.background = 'var(--ok,#15803d)';
    } else {
      el.style.background = 'var(--err,#b91c1c)';
    }
    el.textContent = text;
    el.style.display = 'block';
  }

  function hideConnBanner(){
    const el = document.getElementById('bcConnBanner');
    if (el) el.style.display = 'none';
  }

  function _stopReconnectTimers(){
    if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
    if (_reconnectCountdownTimer) { clearInterval(_reconnectCountdownTimer); _reconnectCountdownTimer = null; }
  }

  function _scheduleReconnect(){
    _stopReconnectTimers();
    if (typeof navigator !== 'undefined' && navigator.onLine === false){
      // No point retrying while offline — the online listener will retry.
      setStatus('Offline', false);
      showConnBanner("You're offline. We'll reconnect automatically when your network is back.", 'err');
      return;
    }
    _reconnectAttempts += 1;
    const base = Math.min(_RECONNECT_MAX_MS, _RECONNECT_MIN_MS * Math.pow(2, _reconnectAttempts - 1));
    const jitter = Math.random() * 400;
    const delay = Math.round(base + jitter);
    let remaining = Math.ceil(delay / 1000);
    const renderCountdown = () => {
      setStatus(`Reconnecting in ${remaining}s…`, false);
      showConnBanner(`Disconnected from BioChirp. Reconnecting in ${remaining}s… (attempt ${_reconnectAttempts})`, 'warn');
    };
    renderCountdown();
    _reconnectCountdownTimer = setInterval(() => {
      remaining = Math.max(0, remaining - 1);
      if (remaining > 0) renderCountdown();
    }, 1000);
    _reconnectTimer = setTimeout(() => {
      _stopReconnectTimers();
      setStatus('Reconnecting…', false);
      showConnBanner('Reconnecting to BioChirp…', 'warn');
      connect();
    }, delay);
  }
  function startPing(){
    clearInterval(pingInterval);
    pingInterval=setInterval(()=>{
      if(ws&&ws.readyState===WebSocket.OPEN){
        ws.send(JSON.stringify({type:'ping',ts:Date.now()}));
      }
    },30000);
  }
  function nowStamp(){ return new Date().toLocaleString(); }
  function scrollEnd(){
    const mc=document.getElementById('messagesContainer');
    mc.scrollTop=mc.scrollHeight;
  }
  function setEmptyState(isEmpty){
    const chat = document.querySelector('.chat');
    if (chat) chat.classList.toggle('empty-state', !!isEmpty);
    const qa = document.getElementById('quickAsks');
    if (qa) qa.style.display = isEmpty ? '' : 'none';
  }
  function esc(s){
    const d=document.createElement('div');
    d.textContent=s==null?'':String(s);
    return d.innerHTML;
  }

  function disableInput(){
    document.getElementById('messageInput').disabled=true;
    document.getElementById('sendButton').disabled=true;
  }
  function enableInput(){
    document.getElementById('messageInput').disabled=false;
    document.getElementById('sendButton').disabled=false;
  }

  // function startStreaming(){ isStreaming=true; }
  // function maybeFinishStreaming(){
  //   if (orchestratorDone && toolCount===0) {
  //     isStreaming=false;
  //   }
  // }

  function startStreaming(){
  isStreaming = true;
  disableInput();     // 🔒 lock input while answer is streaming
  }

  function maybeFinishStreaming(){
    if (orchestratorDone && toolCount === 0) {
      isStreaming = false;
      enableInput();    // 🔓 unlock input only after everything is finished
    }
  }


  function connect(){
    _stopReconnectTimers();
    try{ ws && ws.close(); }catch{}
    disableInput();
    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      // Constructor throws synchronously when URL is invalid — schedule a
      // backoff retry instead of leaving the user with a dead UI.
      setStatus('Connection error', false);
      _scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      _reconnectAttempts = 0;
      _stopReconnectTimers();
      setStatus('Connected', true);
      hideConnBanner();
      startPing();
      enableInput();

      // 🔽 NEW: send the question that came from index page
      if (initialQFromURL) {
        const q = initialQFromURL;
        initialQFromURL = null;

        addUser(q);  // show in chat as user message
        const _byokF=window.biochirpBYOK?window.biochirpBYOK.buildPayloadFields():{};
        ws.send(JSON.stringify({ user_input: q, session_id: activeSessionId, ..._byokF }));
        messageInput.value = '';
        messageInput.style.height = 'auto';
        kickOffStream();

        const qa = document.getElementById('quickAsks');
        if (qa) qa.style.display = 'none';

        // Remove ?q= from URL so refresh doesn't resend
        history.replaceState({}, '', window.location.pathname);
      }
    };

    ws.onclose=()=>{
      disableInput();
      if (_suppressReconnect) {
        _suppressReconnect = false;
        return;  // intentional close (manual reconnect) — let caller drive
      }
      _scheduleReconnect();
    };
    ws.onerror=()=>{
      setStatus('Connection error', false);
      disableInput();
      // onclose fires right after onerror in browsers — let the close handler
      // schedule the backoff retry to avoid double-scheduling.
    };
    ws.onmessage=(e)=>{
      let d;
      try{ d = JSON.parse(e.data); }catch{ return; }
      route(d);
    };
  }

  // Browser network-state listeners. The WS layer's onclose covers most
  // disconnects, but navigator.onLine fires earlier on wifi drop / suspend
  // and lets us tell the user "you're offline" before the 30s WS timeout.
  if (typeof window !== 'undefined' && 'addEventListener' in window) {
    window.addEventListener('online', () => {
      _reconnectAttempts = 0;  // network just returned — full retry budget
      hideConnBanner();
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        setStatus('Reconnecting…', false);
        showConnBanner('Network is back. Reconnecting to BioChirp…', 'warn');
        connect();
      }
    });
    window.addEventListener('offline', () => {
      setStatus('Offline', false);
      showConnBanner("You're offline. We'll reconnect automatically when your network is back.", 'err');
      disableInput();
    });
  }
  window.addEventListener('load', ()=>{
    if (window.isReadOnly && window.isReadOnly()) {
      window.applyReadOnlyMode();
      initTheme();
      return;
    }
    resetChatUI();
    connect();
    initTheme();
  });
  function manualReconnect(){
    _reconnectAttempts = 0;
    _suppressReconnect = true;
    _stopReconnectTimers();
    try{ if(ws) ws.close(); }catch{}
    connect();
  }


  function resetChatUI(){
    const mc=document.getElementById('messagesContainer');
    mc.innerHTML = `
      <div class="message assistant">
        <div class="message-avatar" title="Assistant">
          <img src="assets/brand/logo.svg" alt="Assistant">
        </div>
        <div class="message-content">
          <div class="timestamp">${nowStamp()}</div>

          
          <div class="message-bubble" id="bcWelcomeBubble">Loading welcome message…</div>
        </div>
      </div>`;
    stopFauxOrchStream();
    currentAssistantMessage=null; currentTurn=null; currentToolCards.clear(); orchestratorBuf='';
    toolBuffers.clear(); toolCount=0; suppressOrchStream=false;
    orchVisibleStreamed=false;
    orchestratorDone=false; isStreaming=false;
    toolStartTimes.clear(); toolElapsed.clear();
    const last = mc.querySelector('.message-bubble');
    if(last){
      last.innerHTML = DOMPurify.sanitize(marked.parse(last.textContent));
      last.querySelectorAll('pre code').forEach(c=>hljs.highlightElement(c));
    }
    scrollEnd();
    setEmptyState(true);
  }

  function createAssistant(){
    const el=document.createElement('div'); el.className='message assistant';
    el.innerHTML=`<div class="message-avatar" title="Assistant">
        <img src="assets/brand/logo.svg" alt="Assistant">
      </div>
      <div class="message-content">
        <div class="timestamp">${nowStamp()}</div>
        <div class="message-bubble"></div>
      </div>`;
    // Attach to the active turn (so the question stays sticky over its
    // answer); fall back to the bare messages container for orphan
    // assistant content (e.g. the welcome bubble before any question).
    const host = currentTurn || document.getElementById('messagesContainer');
    host.appendChild(el);
    return el;
  }
  function mainBubble(msgDiv){
    const mc=msgDiv.querySelector('.message-content');
    let b=[...mc.querySelectorAll('.message-bubble')].find(x=>!x.closest('.tool-card'));
    if(!b){
      b=document.createElement('div');
      b.className='message-bubble';
      mc.appendChild(b);
    }
    return b;
  }
  function toolClass(n){
    n=(n||'').toLowerCase();
    if(n.includes('chembl')||n.includes('ttd')||n.includes('ctd')||n.includes('hcdt'))return'table-card';
    if(n.includes('web'))return'tool-web';
    if(n.includes('interpreter'))return'tool-interpreter';
    return'tool-generic';
  }
  function toolEmoji(n){
    n = String(n||'').toLowerCase();
    if (n.includes('chembl'))          return '🧬';
    if (n.includes('ttd'))          return '🎯';
    if (n.includes('ctd'))          return '🧪';
    if (n.includes('hcdt'))         return '💊';
    if (n.includes('web'))          return '🌐';
    if (n.includes('interpreter'))  return '🧠';
    if (n.includes('readme'))       return '📖';
    if (n.includes('memory'))       return '🗂️';
    if (n.includes('tavily') || n.includes('search')) return '🔎';
    if (n.includes('orchestrator')) return '🤖';
    return '🛠️';
  }
  function fmtMs(ms){
    if (ms < 1000) return Math.max(0, Math.round(ms)) + ' ms';
    const s = ms/1000;
    return (s<60) ? s.toFixed(2) + ' s' : (s/60).toFixed(2) + ' min';
  }

  function createToolCard(id,name){
    const c=document.createElement('div');
    c.className=`tool-card ${toolClass(name)}`; c.dataset.toolId=id;
    c.setAttribute('role', 'group');
    c.setAttribute('aria-label', `Tool: ${name || 'Tool'}`);
    const icon = toolEmoji(name);
    const headerId = `toolhdr-${id}`;
    const bodyId   = `toolbody-${id}`;
    // tool-header is the click target that expands the card → behaves like a
    // disclosure button. Screen readers also need to know the controlled
    // region (aria-controls) and current state (aria-expanded).
    c.innerHTML=`<div class="tool-header" data-action="toggle-card"
          id="${headerId}"
          role="button"
          tabindex="0"
          aria-expanded="false"
          aria-controls="${bodyId}">
        <div class="tool-info">
          <div class="tool-icon" aria-hidden="true">${icon}</div>
          <div class="tool-name">${esc(name||'Tool')}</div>
          <div class="tool-chips">
            <div class="chip status running" aria-label="Tool status: running">Running…</div>
            <div class="chip time" data-timechip aria-label="Elapsed time">Time: —</div>
          </div>
        </div>
        <div class="expand-icon" aria-hidden="true">▾</div>
      </div>
      <div class="tool-body" id="${bodyId}" role="region" aria-labelledby="${headerId}">
        <div class="tool-output" role="status" aria-live="polite" aria-atomic="false">Streaming…</div>
      </div>`;
    return c;
  }
  function toggleCard(h){
    const c = h.closest('.tool-card');
    c.classList.toggle('expanded');
    if (c.classList.contains('expanded')) c.dataset.userHold = '1';
    // Keep aria-expanded in sync with the .expanded class so screen readers
    // announce the new state. Some tool-header instances are rendered as
    // <summary> (snapshot view) — those don't carry aria-expanded.
    const hdr = c.querySelector('.tool-header[role="button"]');
    if (hdr) hdr.setAttribute('aria-expanded', c.classList.contains('expanded') ? 'true' : 'false');
  }
  // Keyboard activation for tool-header role=button (Enter / Space).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (!t.matches('.tool-header[role="button"]')) return;
    e.preventDefault();
    toggleCard(t);
  });
  function scheduleFlush(){
    if (rafScheduled) return;
    rafScheduled = true;
    requestAnimationFrame(() => {
      rafScheduled = false;
      for (const [id, chunk] of pendingUI.entries()) {
        pendingUI.delete(id);
        const card = currentToolCards.get(id);
        if (!card) continue;
        const outEl = card.querySelector('.tool-output');
        // If the tool has already been finalised (tool_result arrived and
        // we rendered the natural-language summary), drop the late chunk —
        // otherwise the rAF race overwrites our friendly text with the raw
        // streamed bytes.
        if (outEl.dataset.finalised === '1') continue;
        if (!outEl.dataset.streamed) { outEl.dataset.streamed = '1'; outEl.textContent = ''; }
        if (chunk) outEl.textContent += chunk;
        card.classList.add('expanded');
      }
      scrollEnd();
    });
  }

  function sanitizeOrchestrator(content){
    if (!content) return '';
    const t = content.trim();
    const startsWithEcho = /^\s*`{0,3}\s*"?input[_ ]?query"?\s*[:=>]/i.test(t);
    const shortEcho = t.length <= 500 && /input[_ ]?query/i.test(t) && !/\n.*\n.*\n/.test(t);
    const fencedEcho = /^```[\s\S]*?input[_ ]?query[\s\S]*?```$/i.test(t) && !/```[\s\S]*?```[\s\S]*?```/i.test(t);
    return (startsWithEcho || shortEcho || fencedEcho) ? '' : content;
  }

  function onOrchDelta(text){
    if (!text) return;
    const maybe = sanitizeOrchestrator(text);
    if (!maybe) return;

    stopFauxOrchStream();
    orchestratorBuf += maybe;
    if (suppressOrchStream) return;
    orchVisibleStreamed = true;

    if (!currentAssistantMessage) currentAssistantMessage = createAssistant();
    const bubble = mainBubble(currentAssistantMessage);
    renderOrchBubbleText(bubble, orchestratorBuf, true);
    scrollEnd();
  }
  function onOrchFinal(text){
    const cleaned = sanitizeOrchestrator(text || '');
    const finalText = cleaned || orchestratorBuf || '';
    if (!currentAssistantMessage) currentAssistantMessage = createAssistant();
    if (!orchVisibleStreamed && finalText) {
      fakeStreamOrchFromFullText(finalText);
      attachTurnActions(currentTurn);
      return;
    }
    stopFauxOrchStream();
    const bubble = mainBubble(currentAssistantMessage);
    renderOrchBubbleText(bubble, finalText, false);
    orchestratorBuf = '';
    orchVisibleStreamed = false;
    orchestratorDone = true;
    attachTurnActions(currentTurn);
    maybeFinishStreaming();
    scrollEnd();
  }

  // ── Per-turn actions (Share this answer, Copy as BibTeX) ───────────────
  // The conversation-level ★ Share button in the header captures the WHOLE
  // chat. Researchers usually want a stable link to ONE answer plus a
  // citation they can paste into a paper. attachTurnActions adds a small
  // action bar after the assistant message finalizes; both actions reuse
  // the existing /share endpoint and run entirely client-side. Idempotent —
  // safe to call on every finalize event in the same turn.
  function attachTurnActions(turn){
    if (!turn) return;
    if (turn.querySelector('.bc-turn-actions')) return;
    if (window.isReadOnly && window.isReadOnly()) return;
    // Locate the assistant message in this turn; bail if there isn't one
    // yet (orchestrator can fire onOrchFinal during a tool-only step).
    const asst = turn.querySelector('.message.assistant:not(.bcWelcome)');
    if (!asst) return;
    const mc = asst.querySelector('.message-content');
    if (!mc) return;
    const row = document.createElement('div');
    row.className = 'bc-turn-actions';
    row.setAttribute('role', 'toolbar');
    row.setAttribute('aria-label', 'Answer actions');
    row.innerHTML =
      '<button type="button" class="reconnect bc-turn-action" '
        + 'data-action="share-turn" aria-label="Copy a shareable link to this answer">'
        + '★ Share answer</button>'
      + '<button type="button" class="reconnect bc-turn-action" '
        + 'data-action="copy-bibtex" aria-label="Copy this answer as a BibTeX entry">'
        + '⎘ Copy BibTeX</button>'
      // Feedback buttons — only shown when a DB query produced a result.
      + (_currentDB
          ? '<span class="bc-feedback-sep" aria-hidden="true">|</span>'
            + '<button type="button" class="reconnect bc-turn-action bc-feedback-btn" '
              + 'data-action="feedback-vote" data-verdict="up" '
              + 'title="This answer was correct — help improve BioChirp" '
              + 'aria-label="Mark answer as correct">👍</button>'
            + '<button type="button" class="reconnect bc-turn-action bc-feedback-btn" '
              + 'data-action="feedback-vote" data-verdict="down" '
              + 'title="This answer was incorrect or incomplete" '
              + 'aria-label="Mark answer as incorrect">👎</button>'
          : '')
      + '<span class="bc-turn-action-status" aria-live="polite"></span>';
    mc.appendChild(row);
  }

  function _turnFromAction(btn){
    const t = btn.closest('.turn');
    if (!t) return null;
    return t;
  }

  function _setTurnActionStatus(turn, text, kind){
    const s = turn && turn.querySelector('.bc-turn-action-status');
    if (!s) return;
    s.textContent = text || '';
    s.dataset.kind = kind || '';
  }

  async function _captureTurnSnapshot(turn){
    // Capture the WHOLE page DOM (same as the header ★ Share path) but mark
    // the turn so the snapshot's CSS can scroll/highlight it. Sharing only
    // the turn DOM in isolation would lose the chat chrome, theme, and
    // table-rendering CSS the answer relies on.
    const cloneRoot = document.documentElement.cloneNode(true);
    const head = cloneRoot.querySelector('head');
    if (!head) throw new Error('No <head> to inject into.');
    // base href so relative assets resolve inside srcdoc.
    let base = head.querySelector('base');
    if (!base) {
      base = document.createElement('base');
      head.insertBefore(base, head.firstChild);
    }
    base.setAttribute('href', window.location.origin + '/');
    // Read-only marker — same convention as captureAndUploadSnapshot.
    const marker = document.createElement('script');
    marker.textContent =
      'window.__BIOCHIRP_READONLY__=true;'
      + 'window.__BIOCHIRP_FOCUS_TURN__=' + JSON.stringify(turn.dataset.userQuestion || '') + ';';
    head.insertBefore(marker, head.firstChild);
    // Drop the share modal so the snapshot doesn't inherit a stale dialog.
    const modal = cloneRoot.querySelector('#shareModal');
    if (modal) modal.remove();
    // Mark the focused turn so a future stylesheet hook can highlight it.
    const clonedTurns = cloneRoot.querySelectorAll('.turn');
    const target = turn.dataset.userQuestion || '';
    for (const t of clonedTurns){
      if ((t.dataset.userQuestion || '') === target){
        t.classList.add('bc-shared-turn');
        t.setAttribute('id', 'bc-shared-turn');
      }
    }
    const html = '<!DOCTYPE html>\n' + cloneRoot.outerHTML;
    const title = ((turn.dataset.userQuestion || '').slice(0, 120) || 'BioChirp shared answer');
    const resp = await fetch(window.location.origin + '/share', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ html, title, unsafe: true }),
    });
    if (!resp.ok){
      let detail = '';
      try { detail = (await resp.json()).detail || ''; } catch {}
      throw new Error('Share failed (' + resp.status + '): ' + (detail || resp.statusText));
    }
    const data = await resp.json();
    const url = /^https?:/i.test(data.url || '') ? data.url
              : (window.location.origin + (data.url || ('/s/' + data.id)));
    // Deep-link to the focused turn so the recipient lands on the right answer.
    return url + (url.includes('#') ? '' : '#bc-shared-turn');
  }

  window.shareTurnAction = async function (btn){
    const turn = _turnFromAction(btn);
    if (!turn) return;
    btn.disabled = true;
    _setTurnActionStatus(turn, 'Creating link…', 'pending');
    try {
      const url = await _captureTurnSnapshot(turn);
      try { await navigator.clipboard.writeText(url); } catch {}
      // Stash on the turn so a subsequent BibTeX click reuses the link.
      turn.dataset.shareUrl = url;
      _setTurnActionStatus(turn, 'Link copied: ' + url, 'ok');
    } catch (err) {
      _setTurnActionStatus(turn, String(err.message || err), 'err');
    } finally {
      btn.disabled = false;
    }
  };

  // Build a citeable BibTeX @misc entry from the turn's question, today's
  // date, and the page (or per-turn share) URL. Synchronous when a share
  // URL has already been minted on this turn; otherwise mints one first so
  // the citation points to a stable artifact rather than the live chat.
  function _bibtexEntry(turn, shareUrl){
    const question = (turn.dataset.userQuestion || '').trim();
    const ts = turn.dataset.userTimestamp || new Date().toISOString();
    const url = shareUrl || window.location.href;
    const date = ts.slice(0, 10);  // YYYY-MM-DD
    const yr = ts.slice(0, 4);
    // BibTeX key: biochirp + first 8 chars of timestamp digits + first word
    // of question (alphanumeric only) — predictable, collision-resistant.
    const firstWord = (question.match(/[A-Za-z0-9]+/) || ['answer'])[0].toLowerCase();
    const key = 'biochirp_' + ts.replace(/\D/g, '').slice(0, 12) + '_' + firstWord.slice(0, 16);
    // Escape BibTeX special characters in the question so a stray { or %
    // doesn't break the entry.
    const safe = question.replace(/[\\{}#$%&_^~]/g, '\\$&');
    return (
      '@misc{' + key + ',\n'
      + '  title  = {' + safe + '},\n'
      + '  author = {{BioChirp}},\n'
      + '  year   = {' + yr + '},\n'
      + '  month  = {' + date.slice(5, 7) + '},\n'
      + '  day    = {' + date.slice(8, 10) + '},\n'
      + '  note   = {Auditable biomedical retrieval over 26 curated databases. Retrieved ' + date + '.},\n'
      + '  howpublished = {\\url{' + url + '}}\n'
      + '}\n'
    );
  }

  window.copyBibtexAction = async function (btn){
    const turn = _turnFromAction(btn);
    if (!turn) return;
    btn.disabled = true;
    _setTurnActionStatus(turn, 'Building citation…', 'pending');
    try {
      let url = turn.dataset.shareUrl || '';
      if (!url){
        url = await _captureTurnSnapshot(turn);
        turn.dataset.shareUrl = url;
      }
      const bib = _bibtexEntry(turn, url);
      try { await navigator.clipboard.writeText(bib); } catch {}
      _setTurnActionStatus(turn, 'BibTeX entry copied (linked to ' + url + ')', 'ok');
    } catch (err) {
      _setTurnActionStatus(turn, String(err.message || err), 'err');
    } finally {
      btn.disabled = false;
    }
  };

  function route(d){
    switch(d.type){
      case 'heartbeat':
      case 'pong':
      case 'user_ack':
        return;

      case 'orch_step_summary':
        // Capture intermediate pipeline outputs for the feedback payload.
        // schema_mapper carries parsed_value + rephrased_query; ignore others.
        if (d.tool === 'schema_mapper' && d.summary) {
          if (d.summary.parsed_value)    _currentParsedValue    = d.summary.parsed_value;
          if (d.summary.rephrased_query) _currentRephrasedQuery = d.summary.rephrased_query;
        }
        return;

      case 'orchestrator_delta':
        onOrchDelta(d.content);
        return;

      case 'orchestrator_final':
        onOrchFinal(d.content);
        return;

      case 'delta': {
        const nm = String(d.name||'').toLowerCase();
        const tid = String(d.tool_id||'').toLowerCase();
        // 2026-05-17: bio_chat_service streams the final markdown answer as
        // delta events with tool_id="synthesizer" (not "orchestrator"). The
        // legacy per-DB template only routed "orchestrator" deltas to the
        // main message bubble, so on multi-DB the synth answer was being
        // appended to the synth tool-card body instead of the bubble. Treat
        // synthesizer/synth as orchestrator-routed.
        const isOrch = nm.includes('orchestrator') || tid === 'orchestrator'
                    || nm.includes('synth') || tid === 'synthesizer'
                    || tid.startsWith('synth');
        const chunk = d.text ?? d.delta ?? d.content ?? '';
        if (isOrch) {
          // The tool_called handler sets suppressOrchStream=true for every
          // tool including synth. Allow synth deltas through to the bubble.
          suppressOrchStream = false;
          onOrchDelta(chunk);
          return;
        }
        const id = canonicalId(d.tool_id || 'tool');
        const t = String(chunk ?? '');
        toolBuffers.set(id, (toolBuffers.get(id)||'') + t);
        pendingUI.set(id, (pendingUI.get(id)||'') + t);
        scheduleFlush();
        return;
      }

      case 'Tool called':
      case 'tool_called': {
        suppressOrchStream = true;
        const toolName = d.name || (d.item && d.item.raw_item && d.item.raw_item.name) || 'Tool';
        const incomingId = d.tool_id || 'tool';
        const key = nameKey(toolName);
        if (!currentAssistantMessage) currentAssistantMessage = createAssistant();

        if (primaryByName.has(key)) {
          const primary = primaryByName.get(key);
          aliasToPrimary.set(incomingId, primary);
          toolBuffers.set(incomingId, '');
          scrollEnd();
          return;
        }

        primaryByName.set(key, incomingId);
        toolStartTimes.set(incomingId, performance.now());
        toolCount++;

        const card = createToolCard(incomingId, toolName);
        currentToolCards.set(incomingId, card);
        toolBuffers.set(incomingId, '');

        const mc = currentAssistantMessage.querySelector('.message-content');
        const main = mc.querySelector('.message-bubble:not(.tool-card .message-bubble)');
        if (main) mc.insertBefore(card, main); else mc.appendChild(card);
        scrollEnd();
        return;
      }

      case 'Tool output':
      case 'Tool output delta': {
        const id = canonicalId(d.tool_id || (d.item && d.item.tool_id) || 'tool');
        const out = (d.item && (d.item.delta ?? d.item.output)) ?? '';
        const t = (typeof out === 'string') ? out
                 : out && typeof out === 'object' ? JSON.stringify(out)
                 : String(out ?? '');
        toolBuffers.set(id, (toolBuffers.get(id)||'') + t);
        pendingUI.set(id, (pendingUI.get(id)||'') + t);
        scheduleFlush();
        return;
      }

      case 'tool_result': {
        const canon = canonicalId(d.tool_id || 'tool');
        const card = currentToolCards.get(canon);

        const t0 = toolStartTimes.get(canon);
        let ms = null;
        if (typeof t0 === 'number') {
          ms = Math.max(0, performance.now() - t0);
          toolElapsed.set(canon, ms);
        }

        const ok = (d.ok !== undefined) ? d.ok
                 : ((d.item && d.item.ok) !== undefined ? d.item.ok : true);

        if (card) {
          // Stamp pipeline-supplied context so renderToolOutput's empty
          // state can show "DrugCentral timed out (45 s)" instead of the
          // generic "[Empty Table]". The DB token is the tool name itself
          // (e.g. "drugcentral") — friendlier names come from _dbLong.
          const toolNameText = card.querySelector('.tool-name')?.textContent || '';
          card.dataset.dbName = String(toolNameText || '').trim().toLowerCase();
          if (d.failure_cause)    card.dataset.failureCause   = String(d.failure_cause);
          if (d.failure_message)  card.dataset.failureMessage = String(d.failure_message);
          if (d.elapsed_seconds != null) card.dataset.elapsedSeconds = String(d.elapsed_seconds);
          if (d.parsed_term)      card.dataset.parsedTerm = String(d.parsed_term);
          if (d.row_count != null) card.dataset.rowCount = String(d.row_count);

          const statusChip = card.querySelector('.chip.status');
          if (statusChip) {
            // Distinguish "0 rows" (ok=false but no failure_cause) from a
            // genuine transport failure — both currently come through with
            // ok=false because tool_result.ok = has_rows.
            let label;
            if (d.failure_cause === 'timeout') {
              label = d.elapsed_seconds != null
                ? `Timed out (${Number(d.elapsed_seconds).toFixed(1)}s)`
                : 'Timed out';
            } else if (d.failure_cause) {
              label = 'Failed';
            } else if (!ok) {
              label = 'No results';
            } else {
              label = 'Completed (tap to view)';
            }
            statusChip.textContent = label;
            statusChip.className = 'chip status ' + (ok ? 'completed' : (d.failure_cause ? 'failed' : 'empty'));
          }
          const timeChip = card.querySelector('[data-timechip]');
          if (timeChip) timeChip.textContent = 'Time: ' + (ms==null ? '—' : fmtMs(ms));

          const buf = toolBuffers.get(canon) || '';
          renderToolOutput(card, buf, toolNameText);
          card.classList.add('expanded','show-output');
          setTimeout(()=>{
            card.classList.remove('show-output');
            if (!card.dataset.userHold) card.classList.remove('expanded');
          }, 2000);

          const nm = (card.querySelector('.tool-name')?.textContent || '').toLowerCase();
          if (primaryByName.get(nm) === canon) primaryByName.delete(nm);
        }

        if (toolStartTimes.has(canon)) {
          toolStartTimes.delete(canon);
          toolCount = Math.max(0, toolCount - 1);
        }
        if (toolCount === 0) {
          suppressOrchStream = false;
        }
        maybeFinishStreaming();
        return;
      }

      case 'final': {
        const finalText = (typeof d.text === 'string') ? d.text : orchestratorBuf;
        onOrchFinal(finalText || '');
        return;
      }

      // Legacy explicit cases (kept for clarity; the regex fallback below catches these too)
      case 'chembl_table':
      case 'ttd_table':
      case 'ctd_table':
      case 'hcdt_table':
        renderTableEvent(d);
        return;

      default:
        // 2026-05-17: the legacy template only handled 4 hard-coded *_table types,
        // so multi-DB queries silently dropped tables from drugcentral, pharmgkb,
        // clinvar, civic, biogrid, string, hpo, reactome, etc. Generic catch:
        // any "<db>_table" event becomes a renderable table card.
        if (typeof d.type === 'string' && /_table$/.test(d.type)) {
          renderTableEvent(d);
        }
        return;
    }
  }


  // ── Natural-language tool summaries ─────────────────────────────────────
  // Turns the raw JSON / key:value blob each tool emits into one sentence of
  // user-facing English. Used both for the collapsed preview line and the
  // expanded body. Falls through to the original raw-JSON renderer if the
  // tool name is not recognised, so adding a new tool requires no change.
  function _asObj(out){
    if (!out) return null;
    if (typeof out === 'string'){ try { return JSON.parse(out); } catch { return null; } }
    return (typeof out === 'object') ? out : null;
  }
  function _listSentence(arr){
    arr = (arr||[]).filter(x => x!=null && String(x).trim()!=='');
    if (!arr.length) return '';
    if (arr.length === 1) return String(arr[0]);
    if (arr.length === 2) return `${arr[0]} and ${arr[1]}`;
    return arr.slice(0,-1).join(', ') + ', and ' + arr.slice(-1)[0];
  }
  // Map a TTD/CTD/HCDT/… short token to a human-readable database name.
  const _DB_LONG_NAME = {
    ttd:'Therapeutic Targets Database', ctd:'Comparative Toxicogenomics Database',
    hcdt:'Highly Confident Drug-Target database',
    clinvar:'ClinVar',
    hpo:'Human Phenotype Ontology', msigdb:'MSigDB',
    orphanet:'Orphanet',
    reactome:'Reactome', string:'STRING',
    uniprot:'UniProt', opentarget:'Open Targets'
  };
  function _dbLong(token){
    const k = String(token||'').toLowerCase();
    return _DB_LONG_NAME[k] || token;
  }
  // Render an entity field as "<role> **<value>**" with a more natural role name.
  const _FIELD_PHRASE = {
    drug_name:'drug',  target_name:'target', gene_name:'gene', gene_symbol:'gene',
    disease_name:'disease', pathway_name:'pathway', biomarker_name:'biomarker',
    variant_name:'variant', chemical_name:'compound', synonym:'drug alias',
    uniprot_xref:'UniProt entry', target_type:'target development stage',
    activity_type:'potency type', activity_value:'potency value',
    activity_unit:'potency unit', activity_operator:'comparison',
    drug_compound_id:'compound ID', pubchem_cid:'PubChem CID',
    pubchem_sid:'PubChem SID', cas_number:'CAS number', cas:'CAS number',
    chebi_xref:'ChEBI ID', superdrug_atc:'ATC code', formula:'molecular formula',
    mechanism_of_action:'mechanism of action',
    drug_mechanism_of_action_on_target:'mechanism of action',
    approval_status:'approval stage', phenotype_name:'phenotype',
    tf_name:'transcription factor', location:'chromosomal location',
    locus_type:'locus type', locus_group:'locus group',
    collection:'gene-set collection',
    compound_chembl_id:'ChEMBL compound ID', target_chembl_id:'ChEMBL target ID',
  };
  function _phraseField(k){
    return _FIELD_PHRASE[k] || k.replace(/_/g,' ');
  }

  function friendlyToolSentence(toolName, output){
    const key = String(toolName||'').toLowerCase();
    const obj = _asObj(output);
    const txt = (typeof output === 'string') ? output : '';

    // ── out_of_scope (polite NON_BIOMEDICAL refusal) ───────────────────────
    if (key === 'out_of_scope' || key.includes('out_of_scope')){
      return null;   // let the streamed text render verbatim — it's already prose
    }

    // ── router_tool ────────────────────────────────────────────────────────
    if (key.includes('router')){
      // Real shape from backend: text "→ BIOCHIRP_STRUCTURED_RETRIEVAL" or "→ WEB"
      let decision = (obj && (obj.route || obj.decision || obj.next)) || '';
      if (!decision){
        const m = txt.match(/(?:→|->|route[:=]\s*)\s*([A-Z][A-Z0-9_\s]+)/i);
        if (m) decision = m[1].trim();
      }
      const d = decision.toUpperCase();
      // Order matters — several decisions share the substring "RETRIEVAL".
      if (d.includes('STRUCTURED'))
        return 'This question can be answered from a curated database — sending it to BioChirp\'s structured-retrieval pipeline (interpret → select database → query → synthesise).';
      if (d.includes('OUT_OF_SCOPE'))
        return 'This is a biomedical question, but it falls outside this database\'s coverage — falling back to a web search to answer it.';
      if (d.includes('REASONING'))
        return 'This biomedical question needs open-ended reasoning rather than a record lookup — falling back to a web search.';
      if (d.includes('NON_BIOMEDICAL'))
        return 'This is a general, non-biomedical question — the curated databases don\'t cover it, so answering from a general web search.';
      if (d.includes('README') || d.includes('CAPAB'))
        return 'This is a question about BioChirp itself — answering from its capabilities/README.';
      if (d.includes('UNCLASSIFIABLE') || d.includes('OTHER'))
        return 'The question couldn\'t be classified with confidence — using a web search as the safest fallback.';
      if (d.includes('WEB') || d.includes('SEARCH'))
        return 'This question is outside the curated databases — falling back to a biomedical web search.';
      return decision
        ? `Routed this query along the ${decision.replace(/_/g,' ').toLowerCase()} path.`
        : 'Deciding which pipeline (curated databases vs. web search) should handle this question.';
    }

    // ── interpreter ────────────────────────────────────────────────────────
    if (key === 'interpreter' || key.includes('interpret')){
      // Real shape: comma-joined "k: v" pairs e.g. "target_name: PD-1, drug_name: requested"
      // OR (older clients) a JSON parsed_value object.
      const found = {};   // populated fields (value !== "requested")
      const asked = [];   // fields marked as "requested"
      const pv = obj && (obj.parsed_value || obj.value || obj);
      if (pv && typeof pv === 'object'){
        for (const [k, v] of Object.entries(pv)){
          if (v == null || (Array.isArray(v) && !v.length)) continue;
          if (v === 'requested') asked.push(k);
          else found[k] = Array.isArray(v) ? v : [v];
        }
      } else if (txt){
        for (const raw of txt.split(/[,\n]+/)){
          const m = raw.match(/^\s*([A-Za-z_][A-Za-z_0-9]*)\s*:\s*(.+?)\s*$/);
          if (!m) continue;
          const k = m[1], v = m[2];
          if (v === 'requested') asked.push(k);
          else found[k] = [v];
        }
      }

      // Plain-English intent line built from (asked-for fields → entity
      // clauses). Each entity has a single self-contained idiom so we avoid
      // awkward composites like "for the drug the drug imatinib" or "that
      // target the target PD-1".
      function _entityClause(entityKey, valueText){
        switch(entityKey){
          case 'disease_name':    return `for the disease **${valueText}**`;
          case 'target_name':     return `against **${valueText}**`;
          case 'gene_name':       return `linked to the gene **${valueText}**`;
          case 'gene_symbol':     return `linked to the gene **${valueText}**`;
          case 'uniprot_xref':    return `mapped to UniProt entry **${valueText}**`;
          case 'pathway_name':    return `involved in the pathway **${valueText}**`;
          case 'synonym':         return `whose brand name or alias is **${valueText}**`;
          case 'drug_name':       return `for the drug **${valueText}**`;
          case 'chemical_name':   return `for the compound **${valueText}**`;
          case 'tf_name':         return `regulated by the transcription factor **${valueText}**`;
          case 'phenotype_name':  return `associated with the phenotype **${valueText}**`;
          case 'biomarker_name':  return `associated with the biomarker **${valueText}**`;
          case 'variant_name':    return `carrying the variant **${valueText}**`;
          case 'approval_status': return `at the approval stage **${valueText}**`;
          case 'activity_type':   return `measured as **${valueText}**`;
          case 'activity_unit':   return `in units of **${valueText}**`;
          case 'target_type':     return `at the development stage **${valueText}**`;
          case 'drug_mechanism_of_action_on_target':
          case 'mechanism_of_action':
            return `with mechanism of action **${valueText}**`;
          case 'pubchem_cid':     return `with PubChem CID **${valueText}**`;
          case 'cas_number':      return `with CAS number **${valueText}**`;
          case 'chebi_xref':      return `with ChEBI ID **${valueText}**`;
          default:                return `with ${entityKey.replace(/_/g,' ')} **${valueText}**`;
        }
      }
      function _askedNoun(k){
        switch(k){
          case 'drug_name':       return 'matching drugs';
          case 'target_name':     return 'the targets involved';
          case 'gene_name':       return 'the associated genes';
          case 'gene_symbol':     return 'the gene symbols';
          case 'disease_name':    return 'the linked diseases';
          case 'pathway_name':    return 'the involved pathways';
          case 'biomarker_name':  return 'the associated biomarkers';
          case 'approval_status': return 'their approval stage';
          case 'pubchem_cid':     return 'the PubChem CID';
          case 'pubchem_sid':     return 'the PubChem SID';
          case 'cas_number':      return 'the CAS number';
          case 'chebi_xref':      return 'the ChEBI ID';
          case 'uniprot_xref':    return 'the UniProt entry name';
          case 'superdrug_atc':   return 'the ATC code';
          case 'formula':         return 'the molecular formula';
          case 'synonym':         return 'the drug aliases';
          case 'target_type':     return 'the target development stage';
          case 'drug_compound_id':return 'the compound IDs';
          case 'activity_type':   return 'the potency type';
          case 'activity_value':  return 'the potency value';
          case 'activity_unit':   return 'the potency unit';
          case 'drug_mechanism_of_action_on_target':
          case 'mechanism_of_action':
            return 'the mechanism of action';
          default:                return 'the matching ' + k.replace(/_/g,' ');
        }
      }

      const foundEntries  = Object.entries(found);
      const askedFriendly = asked.map(_askedNoun);

      // Nothing matched the schema_kg "key: value[, key: value]" shape this
      // parser expects. If there's real streamed text anyway — e.g. Open
      // Targets' interpreter tool emits full prose sentences via its
      // `message` field, not terse key:value pairs — don't paper over it
      // with a wrong "nothing detected" summary. Bail out so renderToolOutput
      // falls through to its default renderer, which shows the actual
      // streamed text instead of a fabricated (and here, incorrect) one.
      if (!foundEntries.length && !askedFriendly.length && txt && txt.trim()) return null;

      // Compose the intent line
      let intent = '';
      if (askedFriendly.length && foundEntries.length){
        const askedList    = _listSentence(askedFriendly);
        const entityClauses = foundEntries.map(([k,vs]) =>
          _entityClause(k, _listSentence(vs))
        );
        intent = `You asked us to find ${askedList} ${_listSentence(entityClauses)}.`;
      } else if (foundEntries.length){
        const clauses = foundEntries.map(([k,vs]) =>
          _entityClause(k, _listSentence(vs))
        );
        intent = `You asked us to retrieve information ${_listSentence(clauses)}.`;
      } else if (askedFriendly.length){
        intent = `You asked us to return ${_listSentence(askedFriendly)} — but no specific filter entity was detected, so the database will scan broadly.`;
      } else {
        intent = 'No specific entities or requested fields were detected in your question.';
      }

      // Technical breakdown chips
      const foundPhrase = foundEntries.map(
        ([k, vs]) => `\`${k}\` = **${_listSentence(vs)}** (${_phraseField(k)})`
      );
      const askedPhrase = asked.map(k => `\`${k}\` (${_phraseField(k)})`);

      const lines = [];
      lines.push(`**What you asked for:** ${intent}`);
      lines.push('');
      lines.push('**How the question was parsed (technical breakdown):**');
      if (foundPhrase.length) lines.push(`- ✅ Filter values extracted from your question: ${_listSentence(foundPhrase)}.`);
      else                    lines.push('- ℹ️ No concrete filter values were detected.');
      if (askedPhrase.length) lines.push(`- 🎯 Output columns the database will return: ${_listSentence(askedPhrase)}.`);
      else                    lines.push('- 🎯 Output columns: defaulting to the schema (no explicit `requested` markers).');
      lines.push(`- ➡️ Next step: the database selector will route this to the matching curated database, then the database tool will run the filter and join, and finally the synthesiser will compose the answer.`);
      return lines.join('\n');
    }

    // ── database_selector ──────────────────────────────────────────────────
    if (key.includes('database_selector') || key.includes('db_select') || key.includes('selector')){
      // Real shape: newline-joined "TTD: <why text>" lines, or JSON {databases, why}.
      const pairs = [];   // [{db: 'TTD', why: '…'}, …]
      let dbs = obj && (obj.databases || obj.relevant_databases);
      const why = (obj && obj.why) || null;
      if (typeof dbs === 'string') dbs = [dbs];
      if (Array.isArray(dbs)){
        for (const d of dbs) pairs.push({db: String(d), why: (why && why[d]) || ''});
      } else if (txt){
        for (const raw of txt.split('\n')){
          const m = raw.match(/^\s*([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*?)\s*$/);
          if (m) pairs.push({db: m[1], why: m[2]});
          else if (raw.trim()) pairs.push({db: raw.trim(), why: ''});
        }
      }
      if (!pairs.length) return 'Choosing which curated database(s) cover this question.';

      // Single-DB shortcut (covers the /ttd_chat single-DB lock).
      if (pairs.length === 1){
        const {db, why} = pairs[0];
        const w = why.toLowerCase();
        if (w.includes('single-db route') || w.includes('route_lock'))
          return `You are on a single-database chat, so the answer will come from the **${_dbLong(db)}** only.`;
        if (w)
          return `Picked the **${_dbLong(db)}** because ${why}.`;
        return `Picked the **${_dbLong(db)}** as the best match for the entities in your question.`;
      }
      // Multi-DB: list with reasons when present.
      const items = pairs.map(({db, why}) =>
        why ? `**${_dbLong(db)}** (${why})` : `**${_dbLong(db)}**`
      );
      return `Picked ${_listSentence(items)} as the curated database${pairs.length>1?'s':''} most likely to answer this question.`;
    }

    // ── hcdt structured JSON trace (rich interpretation + filter funnel) ──────
    // Emitted by chat.py _build_filter_trace_text() as a JSON string with
    // { row_count, filter_val, filter_trace }. Renders the full "what was
    // understood" interpretation section that the generic FILTER-line parser
    // cannot produce (it doesn't have the requested-vs-filtered split).
    if (key === 'hcdt' && obj && Array.isArray(obj.filter_trace)){
      const rc = obj.row_count ?? 0;
      const filterVal  = obj.filter_val  || {};
      const trace      = obj.filter_trace;
      const longName   = 'Highly Confident Drug-Target database';

      // ── Interpretation section ────────────────────────────────────────────
      const reqFields  = [];   // fields marked "requested" — what the user asked for
      const filtFields = [];   // fields with concrete values — what we filtered on
      for (const [k, v] of Object.entries(filterVal)){
        if (v == null || (Array.isArray(v) && !v.length)) continue;
        if (v === 'requested'){
          reqFields.push(k);
        } else {
          const vals = Array.isArray(v) ? v.filter(x => x !== 'requested') : [v];
          if (vals.length) filtFields.push({field: k, vals});
        }
      }

      const lines = [`**${longName}** — query interpreted as:`];
      lines.push('');
      if (reqFields.length){
        const nice = reqFields.map(k => `**${_phraseField(k)}**`).join(', ');
        lines.push(`🔍 **Searching for:** ${nice}`);
      }
      if (filtFields.length){
        for (const {field, vals} of filtFields){
          const shown = vals.slice(0, 4).map(v => `*${v}*`).join(', ');
          const extra = vals.length > 4 ? ` *(+${vals.length - 4} more)*` : '';
          lines.push(`🔎 **Filter — ${_phraseField(field)}:** ${shown}${extra}`);
        }
      }
      if (!reqFields.length && !filtFields.length){
        lines.push('ℹ️ No specific entities detected — broad scan performed.');
      }

      // ── Filter funnel section ─────────────────────────────────────────────
      const filterSteps = trace.filter(t => !String(t.column||'').startsWith('JOIN'));
      const joinSteps   = trace.filter(t =>  String(t.column||'').startsWith('JOIN'));

      if (filterSteps.length || joinSteps.length){
        lines.push('');
        lines.push(
          `Applied ${filterSteps.length} filter step${filterSteps.length!==1?'s':''}` +
          (joinSteps.length ? ` and ${joinSteps.length} table join${joinSteps.length!==1?'s':''}` : '') +
          ' to build your answer:'
        );
        for (const t of filterSteps){
          const before = t.rows_before || 0;
          const after  = t.rows_after  || 0;
          const pct    = before > 0 ? Math.round((1 - after/before)*1000)/10 : 0;
          const arrow  = after < before ? '↓' : after > before ? '↑' : '=';
          const vals   = (t.input_values||[]).slice(0,4).join(', ');
          const valTxt = vals ? ` _(values: ${vals})_` : '';
          lines.push(
            `- 🔽 Filter on \`${t.column}\` (${_phraseField(t.column)}): ` +
            `**${before.toLocaleString()}** rows ${arrow} **${after.toLocaleString()}** rows ` +
            `(-${pct}%)${valTxt}`
          );
        }
        const _JOIN_PURPOSE_HCDT = {
          drug_disease_association:       'each drug can treat many diseases',
          drug_master_table:              'resolves disease associations to full drug records',
          drug_gene_association_hcdt:     'each target gene is acted on by many drugs',
          gene_master_table:              'resolves gene IDs to full gene records',
          drug_target_association:        'each target can be acted on by many drugs',
          disease_master_table:           'resolves disease names to full disease records',
        };
        for (const t of joinSteps){
          const m = String(t.column||'').match(/JOIN\(([^→]+)→([^)]+)\)/);
          const parent  = m ? m[1] : '?';
          const child   = m ? m[2] : '?';
          const before  = t.rows_before || 0;
          const after   = t.rows_after  || 0;
          const factor  = before > 0 ? (after/before).toFixed(1) : '?';
          const purpose = _JOIN_PURPOSE_HCDT[child] || '';
          const purposeTxt = purpose ? ` — *${purpose}*` : '';
          lines.push(
            `- 🔗 Join \`${parent}\` × \`${child}\`: ` +
            `**${before.toLocaleString()}** rows → **${after.toLocaleString()}** rows ` +
            `(×${factor}${purposeTxt})`
          );
        }
      }

      lines.push('');
      lines.push(`✅ **Final result: ${rc.toLocaleString()} row${rc!==1?'s':''}** returned.`);
      return lines.join('\n');
    }

    // DB worker tools (ttd/ctd/hcdt/…): tables are rendered elsewhere; this
    // only sets the collapsed-preview text on the tool card.
    const dbToolNames = ['ttd','ctd','hcdt','clinvar','hpo','msigdb','orphanet','reactome','string','uniprot','opentarget'];
    const dbHit = dbToolNames.find(n => key === n || key.endsWith('_'+n) || key.startsWith(n+'_'));
    if (dbHit){
      let rc = obj && (obj.row_count ?? obj.rows ?? (Array.isArray(obj.table) ? obj.table.length : null));
      // Parse the per-filter trace lines emitted by ttd_service/pipeline.py
      // (one "FILTER <col>: <before> -> <after> rows (values: …)" per filter).
      const traceLines = [];
      if (txt){
        for (const raw of txt.split('\n')){
          // Column may include spaces / "OR" for the unioned target_name+gene_name stat.
          const m = raw.match(/^\s*FILTER\s+(.+?)(?:\s+\[([^\]]+)\])?\s*:\s*([\d,]+)\s*->\s*([\d,]+)\s*rows?\s*(?:\(values:\s*([^)]*)\))?/i);
          if (m){
            const col    = m[1].trim();
            const table  = (m[2]||'').trim();
            const before = parseInt(m[3].replace(/,/g,''),10);
            const after  = parseInt(m[4].replace(/,/g,''),10);
            const vals   = (m[5]||'').trim();
            traceLines.push({col, table, before, after, vals});
          }
        }
      }
      // Fallback: backend often emits a plain "<N> rows" string for the
      // worker's `delta` chunk — pull the number out of that.
      if (rc == null && txt){
        // Matches "51 rows", "Retrieved 51 rows", "rows: 7,129", "row_count=12".
        const m = txt.match(/row[_\s]*count\s*[:=]\s*(\d+(?:[,_]\d+)*)/i)
              || txt.match(/rows?\s*[:=]\s*(\d+(?:[,_]\d+)*)/i)
              || txt.match(/(\d+(?:[,_]\d+)*)\s*rows?\b/i);
        if (m) rc = parseInt(m[1].replace(/[,_]/g,''), 10);
      }
      const longName = ({
        ttd:'Therapeutic Targets Database', ctd:'Comparative Toxicogenomics Database',
        hcdt:'Highly Confident Drug-Target database',
        clinvar:'ClinVar',
        hpo:'Human Phenotype Ontology', msigdb:'MSigDB',
        orphanet:'Orphanet',
        reactome:'Reactome', string:'STRING',
        uniprot:'UniProt', opentarget:'Open Targets'
      })[dbHit] || dbHit.toUpperCase();

      // Build the multi-line filter-funnel summary when we have a trace.
      if (traceLines.length){
        // Tables that benefit from a one-liner "this stores X↔Y links"
        // explanation when a join expands the row count.
        const _JOIN_PURPOSE = {
          drug_target_association:                 'each target can be acted on by many drugs',
          target_disease_association:              'each target can be linked to many diseases',
          drug_disease_association:                'each drug can treat many diseases',
          target_pathway_association:              'each target can participate in many pathways',
          biomarker_disease_association:           'each biomarker can be linked to many diseases',
          drug_synonyms_association:               'each drug can have many brand-name aliases',
          drug_crossmatching_association:          'each drug maps to identifiers in many external databases',
          target_compound_activity_association:    'each target can have many bioactivity measurements',
          target_uniprot_association:              'each target maps to one (or several) UniProt entries',
        };

        const filterSteps = traceLines.filter(t => !t.col.startsWith('JOIN'));
        const joinSteps   = traceLines.filter(t =>  t.col.startsWith('JOIN'));

        const lines = [];
        lines.push(
          `**${longName}** — applied ${filterSteps.length} filter step${filterSteps.length===1?'':'s'}` +
          (joinSteps.length ? ` and ${joinSteps.length} table join${joinSteps.length===1?'':'s'}` : '') +
          ` to build your answer:`
        );

        // First, the filter steps (reductions)
        for (const t of filterSteps){
          const pct   = (t.before > 0) ? Math.round((1 - t.after/t.before)*1000)/10 : 0;
          const arrow = (t.after < t.before) ? '↓' : (t.after > t.before ? '↑' : '=');
          const human = _phraseField(t.col);
          const vals  = t.vals ? ` _(values: ${t.vals})_` : '';
          lines.push(
            `- 🔽 Filter on \`${t.col}\` (${human})${t.table ? ' in \`'+t.table+'\`' : ''}: **${t.before.toLocaleString()}** rows ${arrow} **${t.after.toLocaleString()}** rows ` +
            `(${pct>=0?'-':'+'}${Math.abs(pct)}%)${vals}`
          );
        }

        // Then, the join steps (expansions)
        for (const t of joinSteps){
          // col looks like "JOIN(parent→child)"
          const m = t.col.match(/^JOIN\(([^→]+)→([^)]+)\)$/);
          const parent = m ? m[1] : '?';
          const child  = m ? m[2] : '?';
          const factor = t.before > 0 ? (t.after / t.before) : 0;
          const purpose = _JOIN_PURPOSE[child] || '';
          const purposeText = purpose ? ` — *${purpose}*` : '';
          lines.push(
            `- 🔗 Join \`${parent}\` × \`${child}\`: **${t.before.toLocaleString()}** rows → **${t.after.toLocaleString()}** rows ` +
            `(×${factor.toFixed(1)} expansion${purposeText})`
          );
        }

        if (rc != null){
          lines.push(`- ✅ **Final result: ${rc.toLocaleString()} row${rc===1?'':'s'}** returned.`);
          if (joinSteps.length && filterSteps.length){
            const minFilter = Math.min(...filterSteps.map(t => t.after));
            if (rc > minFilter){
              lines.push(
                `\n💡 _Why the final count (${rc.toLocaleString()}) is larger than the smallest filtered set (${minFilter.toLocaleString()}): ` +
                `the filter narrowed to ${minFilter.toLocaleString()} matching **target${minFilter===1?'':'s'}** in the master table, ` +
                `then the join with the association tables expanded that to **${rc.toLocaleString()} target-drug pairs** ` +
                `(one row per drug-target combination)._`
              );
            }
          }
        }
        return lines.join('\n');
      }

      if (rc != null) return `Retrieved **${rc}** matching row${rc===1?'':'s'} from the ${longName}.`;
      return `Queried the ${longName}.`;
    }

    // synthesizer / answer / final-text producers — leave to default rendering
    if (key.includes('synth') || key.includes('answer') || key.includes('final')) return null;

    return null;  // → fall through to the default renderer
  }

  function renderToolOutput(card, output, toolName)
  {
    const outEl = card.querySelector('.tool-output');
    delete outEl.dataset.streamed;

    // Drop any queued streaming chunks for this card — otherwise the next
    // requestAnimationFrame flush would clobber the friendly summary we are
    // about to write.
    try {
      const tid = card.dataset.toolId;
      if (tid) pendingUI.delete(tid);
    } catch {}

    // Try the friendly summariser first — only override when we have a
    // confident match. Anything unknown still uses the legacy renderer.
    const friendly = friendlyToolSentence(toolName, output);
    if (friendly){
      outEl.innerHTML = DOMPurify.sanitize(marked.parse(friendly));
      outEl.dataset.finalised = '1';  // tell scheduleFlush to skip late deltas
      return;
    }

    const toolKey = String(toolName||'').toLowerCase();
    const isDbTool = toolKey.includes('chembl') || toolKey.includes('ttd') || toolKey.includes('ctd') || toolKey.includes('hcdt');
    const rows = isDbTool ? extractTableArray(output) : null;

    // Context passed to tableHtml for empty-state rendering. Populated from
    // dataset attributes the WS tool_result handler stamps on the card so
    // "no results" can be distinguished from a timeout / transport failure.
    const emptyCtx = {
      db: card.dataset.dbName || toolKey,
      parsedTerm: card.dataset.parsedTerm || '',
      failureCause: card.dataset.failureCause || '',
      failureMessage: card.dataset.failureMessage || '',
      elapsedSeconds: card.dataset.elapsedSeconds ? Number(card.dataset.elapsedSeconds) : null,
    };

    if (isDbTool && Array.isArray(rows) && rows.length){
      const csv = toCSV(rows);
      const fname = (toolKey || 'table') + '.csv';
      card.dataset.csv = csv; card.dataset.csvFilename = fname;
      outEl.innerHTML =
        `<div class="bc-actions-row">`
        + `<button class="reconnect" data-action="copy-csv">Copy CSV</button>`
        + `<button class="reconnect" data-action="download-csv">Download CSV</button>`
        + `</div>` + tableHtml(rows,'Results', emptyCtx);
      return;
    }

    let obj=null, isTable=false, table=null;
    if (typeof output==='string') {
      try { obj = JSON.parse(output); } catch {}
    } else if (output && typeof output==='object') {
      obj = output;
    }
    if (obj) {
      const t = obj.table || obj?.item?.output?.table;
      if (Array.isArray(t)) { isTable = true; table = t; }
    }

    if (isTable) {
      outEl.innerHTML = tableHtml(table, 'Results', emptyCtx);
    } else {
      const text = obj ? JSON.stringify(obj, null, 2) : String(output || '');
      // ✅ Render as Markdown/HTML instead of literal text
      outEl.innerHTML = DOMPurify.sanitize(marked.parse(text));
      outEl.querySelectorAll('pre code').forEach(c => hljs.highlightElement(c));
    }
    outEl.dataset.finalised = '1';  // lock against late-arriving delta chunks
  }

  // function renderToolOutput(card, output, toolName){
  //   const outEl = card.querySelector('.tool-output');
  //   delete outEl.dataset.streamed;

  //   const toolKey = String(toolName||'').toLowerCase();
  //   const isDbTool = toolKey.includes('ttd') || toolKey.includes('ctd') || toolKey.includes('hcdt');
  //   const rows = isDbTool ? extractTableArray(output) : null;

  //   if (isDbTool && Array.isArray(rows) && rows.length){
  //     const csv = toCSV(rows);
  //     const fname = (toolKey || 'table') + '.csv';
  //     card.dataset.csv = csv; card.dataset.csvFilename = fname;
  //     outEl.innerHTML =
  //       `<div style="display:flex;gap:8px;align-items:center;margin:4px 0 10px;justify-content:flex-end">
  //          <button class="reconnect" data-action="copy-csv">Copy CSV</button>
  //          <button class="reconnect" data-action="download-csv">Download CSV</button>
  //        </div>` + tableHtml(rows,'Results');
  //     return;
  //   }

  //   let obj=null, isTable=false, table=null;
  //   if (typeof output==='string') {
  //     try { obj = JSON.parse(output); } catch {}
  //   } else if (output && typeof output==='object') {
  //     obj = output;
  //   }
  //   if (obj) {
  //     const t = obj.table || obj?.item?.output?.table;
  //     if (Array.isArray(t)) { isTable = true; table = t; }
  //   }
  //   outEl.textContent = isTable ? '' : (obj ? JSON.stringify(obj,null,2) : String(output || ''));
  //   if (isTable) outEl.innerHTML = tableHtml(table,'Results');
  // }

  // Convert an absolute container CSV path to a public URL.
  // /app/results/<file> → /results/<file>  (served by nginx static route)
  // Anything else → /download?path=...  (bio_chat proxy route)
  function _csvUrl(csvPath) {
    if (!csvPath) return '';
    const m = csvPath.match(/\/app\/results\/([^/]+\.csv)$/);
    if (m) return '/results/' + encodeURIComponent(m[1]);
    return new URL('/download?path=' + encodeURIComponent(csvPath), window.location.origin).toString();
  }

  function renderTableEvent(ev){
    if (!currentAssistantMessage) currentAssistantMessage = createAssistant();
    const mc = currentAssistantMessage.querySelector('.message-content');

    // Dedup: TTD emits two events per result — one from the per-tool
    // worker (csv_path only, no inline rows → "Loading…") and one from
    // the orchestrator's publish_table_records_legacy (inline rows +
    // csv_path → rendered preview). If a table card already exists for
    // this csv_path / row_count in the current assistant message, skip
    // creating a second empty card.
    try {
      const sig = (ev && (ev.csv_path || ev.row_count != null))
        ? `${ev.csv_path || ''}::${ev.row_count != null ? ev.row_count : ''}`
        : null;
      if (sig){
        const existing = mc.querySelectorAll('.tool-card.table-card');
        for (const c of existing){
          if (c.dataset.tableSig === sig) return;  // already rendered
        }
      }
      // 2026-05-17: the previous "any rendered card → suppress" dedup was
      // designed for single-DB chat where TTD emits a duplicate csv-only ping
      // for the same table. In multi-DB mode it dropped subsequent DBs.
      // Now we ONLY suppress an event when there's already a card with the
      // SAME csv_path (so duplicate pings of the same table are filtered
      // but other DBs' tables come through). The sig check above already
      // handles exact csv_path::row_count matches; this extra check covers
      // the case where row_count differs (orchestrator re-emission).
      const incomingHasRows = ev && Array.isArray(ev.rows) && ev.rows.length;
      if (!incomingHasRows && ev && ev.csv_path){
        const rendered = mc.querySelectorAll('.tool-card.table-card');
        for (const c of rendered){
          if (c.dataset.tableSig && c.dataset.tableSig.startsWith(ev.csv_path + '::')
              && c.dataset.rendered === '1') {
            return;  // same csv_path already rendered — true duplicate
          }
        }
      }
    } catch {}

    const label = (ev && ev.row_count != null) ? ('rows: ' + ev.row_count) : 'Ready';
    const totalRows = (ev && ev.row_count != null) ? Number(ev.row_count) : null;

    let downloadLink = '';
    if (ev && ev.csv_path) {
      const url = _csvUrl(ev.csv_path);
      downloadLink =
        '<a class="reconnect" target="_blank" rel="noopener" href="' +
        url +
        '">📥 Download CSV</a>';
    }

    const card = document.createElement('div');
    card.className = 'tool-card table-card';
    if (totalRows != null) card.dataset.totalRows = String(totalRows);
    card.innerHTML =
      '<div class="tool-header" data-action="toggle-card">' +
        '<div class="tool-info">' +
          '<div class="tool-icon" title="Table">📊</div>' +
          '<div class="tool-name">' + esc((ev && ev.type) || 'table') + '</div>' +
          '<div class="tool-chips">' +
            '<div class="chip status completed">' + esc(label) + '</div>' +
            '<div class="chip time" data-timechip>Time: —</div>' +
          '</div>' +
        '</div>' +
        '<div class="expand-icon">▾</div>' +
      '</div>' +
      '<div class="tool-body">' +
        '<div class="tool-output">' +
          '<div class="table-toolbar">' +
            '<div class="pager">' +
              '<button class="pager-prev" disabled>◀ Prev</button>' +
              '<span class="pager-label">Page 1</span>' +
              '<button class="pager-next" disabled>Next ▶</button>' +
            '</div>' +
            '<div class="table-actions">' +
              '<button class="reconnect bc-hidden" data-action="load-full-table">⤵ Load full table</button>' +
              '<button class="reconnect bc-hidden" data-action="download-filtered-csv">📥 Download filtered CSV</button>' +
              downloadLink +
            '</div>' +
          '</div>' +
          '<div class="table-preview bc-table-preview">' +
            ((ev && ev.csv_path) ? 'Loading…' : 'No CSV available') +
          '</div>' +
        '</div>' +
      '</div>';

    // Tag the card so a later duplicate event can be skipped (see dedup
    // guard at the top of this function).
    card.dataset.tableSig =
      `${(ev && ev.csv_path) || ''}::${(ev && ev.row_count != null) ? ev.row_count : ''}`;
    const preview = card.querySelector('.table-preview');

    mc.appendChild(card);
    card.classList.add('expanded');
    setTimeout(()=>{ if(!card.dataset.userHold) card.classList.remove('expanded'); }, 5000);
    scrollEnd();
    if (ev && ev.rows && Array.isArray(ev.rows) && ev.rows.length) {
    preview.innerHTML = tableHtml(ev.rows, 'Results');
    preview.style.color = '';
    card.dataset.rendered = '1';
    if (ev.csv_path) initPaginatedPreview(card, ev.csv_path, 5, totalRows);
  } else if (ev && ev.csv_path) {
    initPaginatedPreview(card, ev.csv_path, 5, totalRows);
    card.dataset.rendered = '1';
  }
  }

  async function initPaginatedPreview(card, csvPath, rowsPerPage, totalRows){
    const previewEl = card.querySelector('.table-preview');
    const labelEl   = card.querySelector('.pager-label');
    const prevBtn   = card.querySelector('.pager-prev');
    const nextBtn   = card.querySelector('.pager-next');
    if (!csvPath) { previewEl.textContent = 'No CSV path provided.'; return; }
    try{
      const tStart = performance.now();
      const url = _csvUrl(csvPath);

      let res = await fetch(url, { headers: { Range: 'bytes=0-350000' } });
      if (!(res.ok || res.status === 206)) res = await fetch(url);
      if (!(res.ok || res.status === 206)) throw new Error('HTTP ' + res.status);
      let text = await res.text();
      text = (text || '').replace(/^\uFEFF/, '');
      if (text && text[text.length-1] !== '\n') text += '\n';
      // Capture the full file size from the preview response.
      // - 206 Partial Content: "Content-Range: bytes 0-N/TOTAL"
      // - 200 OK (file smaller than the range): Content-Length is the whole file
      let totalBytes = 0;
      const cr = res.headers.get('Content-Range') || '';
      const crMatch = cr.match(/\/(\d+)\s*$/);
      if (crMatch) {
        totalBytes = Number(crMatch[1]);
      } else if (res.status === 200) {
        const cl = res.headers.get('Content-Length');
        if (cl) totalBytes = Number(cl);
      }

      const parsed = parseCSVHead(text, 300);
      const rows = parsed.rows || [];
      const cols = (parsed.columns && parsed.columns.length) ? parsed.columns : (rows[0] ? Object.keys(rows[0]) : []);
      if (!rows.length){
        previewEl.textContent = 'CSV appears empty or could not be parsed.';
        return;
      }
      // 200 OK (no Range honoured) AND at least as many rows as the declared
      // total => we already have the whole file in memory.
      const loadedAll = (res.status === 200) && (totalRows == null || rows.length >= totalRows);
      card._preview = {
        rows,
        cols,
        page: 1,
        rpp: rowsPerPage || 5,
        filters: {},
        filteredRows: rows,
        // sort state — set by header-click handler in attachSortHandlers().
        // dir cycles: null (unsorted) → 'asc' → 'desc' → null on the same col.
        sort: { col: null, dir: null },
        totalRows: (totalRows != null ? totalRows : rows.length),
        totalBytes,
        loadedAll,
        csvPath,
      };
      buildPreviewSkeleton(card);
      prevBtn.onclick = ()=>{
        if(card._preview.page>1){
          card._preview.page--;
          renderPreviewPage(card);
        }
      };
      nextBtn.onclick = ()=>{
        const totalPages = Math.max(1, Math.ceil((card._preview.filteredRows || []).length / card._preview.rpp));
        if(card._preview.page<totalPages){
          card._preview.page++;
          renderPreviewPage(card);
        }
      };
      renderPreviewPage(card);
      updateTableActionVisibility(card);
      const timeChip = card.querySelector('[data-timechip]');
      if (timeChip) timeChip.textContent = 'Time: ' + fmtMs(performance.now() - tStart);
    }catch(err){
      console.error('[preview] failed:', err);
      previewEl.textContent = 'Preview failed. (Use Download link)';
    }
  }

  // Build the table skeleton ONCE so that filter UI isn't destroyed on every
  // page change. renderPreviewPage replaces only the <tbody>.
  // Each column header carries an Excel-style filter dropdown arrow that
  // opens a popup with a search box and per-value checkboxes.
  function buildPreviewSkeleton(card){
    const previewEl = card.querySelector('.table-preview');
    const p = card._preview;
    const allCols = p.cols || [];
    const srcKey = pickSourceDbKey(allCols);
    // Track which key (if any) we're hoisting so renderPreviewPage agrees.
    p.srcKey = srcKey;
    const cols = srcKey ? allCols.filter(c => c !== srcKey) : allCols;
    p.displayCols = cols;
    const escAttr = (s)=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const stickyHead = srcKey
      ? '<th class="preview-col-th preview-source-th ' + _STICKY_TH_CLASS + '">' +
          '<span class="bc-col-head">' +
            '<span class="preview-col-label bc-col-sort" data-sort-col="' + escAttr(srcKey) + '" role="button" tabindex="0" title="Click to sort">' +
              'Source<span class="bc-sort-ind" aria-hidden="true"></span>' +
            '</span>' +
            '<button type="button" class="col-filter-btn" data-col="' + escAttr(srcKey) + '" title="Filter" aria-label="Filter Source">&#9662;</button>' +
          '</span>' +
        '</th>'
      : '';
    const headCells = cols.map(c =>
      '<th class="preview-col-th">' +
        '<span class="bc-col-head">' +
          '<span class="preview-col-label bc-col-sort" data-sort-col="' + escAttr(c) + '" role="button" tabindex="0" title="Click to sort">' +
            esc(c.replace(/_/g,' ')) + '<span class="bc-sort-ind" aria-hidden="true"></span>' +
          '</span>' +
          '<button type="button" class="col-filter-btn" data-col="' + escAttr(c) + '" title="Filter" aria-label="Filter ' + escAttr(c) + '">&#9662;</button>' +
        '</span>' +
      '</th>'
    ).join('');
    const legendHtml = srcKey
      ? _dbLegendStripHtml(_uniqueDbsFromRows(p.rows, srcKey))
      : '';
    previewEl.style.color = '';
    previewEl.innerHTML =
      '<div class="preview-meta bc-preview-meta" data-meta></div>' +
      legendHtml +
      '<div class="bc-table-scroll">' +
        '<table class="preview-table bc-table">' +
          '<caption class="bc-preview-caption">Preview</caption>' +
          '<thead>' +
            '<tr>' + stickyHead + headCells + '</tr>' +
          '</thead>' +
          '<tbody data-tbody></tbody>' +
        '</table>' +
      '</div>';
    previewEl.querySelectorAll('.col-filter-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        openColFilterPopup(card, btn, btn.dataset.col);
      });
    });
    installColFilterCloseHandler();
    attachSortHandlers(card);
    updateSortIndicators(card);
  }

  // Click on a column label cycles sort direction for that column:
  //   unsorted → asc → desc → unsorted. Clicking a *different* column always
  //   starts a fresh asc sort. Sort is applied AFTER any active checkbox
  //   filter (see applyPreviewFilters → applyPreviewSort) so the two
  //   compose: filter narrows the row set, sort orders what remains.
  function attachSortHandlers(card){
    const previewEl = card.querySelector('.table-preview');
    if (!previewEl) return;
    previewEl.querySelectorAll('.bc-col-sort').forEach(label => {
      const handler = (e) => {
        e.stopPropagation();
        const col = label.dataset.sortCol;
        if (!col) return;
        const p = card._preview;
        if (!p) return;
        const cur = p.sort || { col: null, dir: null };
        let next;
        if (cur.col !== col)         next = { col, dir: 'asc' };
        else if (cur.dir === 'asc')  next = { col, dir: 'desc' };
        else if (cur.dir === 'desc') next = { col: null, dir: null };
        else                         next = { col, dir: 'asc' };
        p.sort = next;
        applyPreviewSort(card);
        p.page = 1;
        renderPreviewPage(card);
        updateSortIndicators(card);
      };
      label.addEventListener('click', handler);
      label.addEventListener('keydown', (e) => {
        // Keyboard parity: Enter / Space activate the sort like a button.
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          handler(e);
        }
      });
    });
  }

  // Sort p.filteredRows in place. Empty / null values always sink to the
  // bottom regardless of direction so the user never has to page past a
  // wall of blanks. Numeric columns (every non-empty value parses as a
  // finite number) compare numerically; everything else is localeCompare.
  function applyPreviewSort(card){
    const p = card && card._preview;
    if (!p) return;
    const s = p.sort || { col: null, dir: null };
    if (!s.col || !s.dir) {
      // Restore filter order (insertion order of the filtered slice).
      // Re-derive filteredRows by re-applying filters without sort.
      const active = [];
      for (const k in (p.filters || {})) {
        const f = p.filters[k];
        if (f && f.allowed) active.push([k, f.allowed]);
      }
      p.filteredRows = active.length
        ? p.rows.filter(row => active.every(([c, allowed]) => allowed.has(row[c] == null ? '' : String(row[c]))))
        : p.rows.slice();
      return;
    }
    const col = s.col;
    const rows = (p.filteredRows || []).slice();
    // Numeric detection: every non-empty cell parses to a finite number.
    let numeric = true;
    for (const r of rows) {
      const v = r[col];
      if (v == null || v === '') continue;
      const n = Number(v);
      if (!Number.isFinite(n)) { numeric = false; break; }
    }
    const dirMul = (s.dir === 'desc') ? -1 : 1;
    rows.sort((a, b) => {
      const av = a[col], bv = b[col];
      const aEmpty = (av == null || av === '');
      const bEmpty = (bv == null || bv === '');
      if (aEmpty && bEmpty) return 0;
      if (aEmpty) return 1;   // always last regardless of direction
      if (bEmpty) return -1;
      if (numeric) {
        return (Number(av) - Number(bv)) * dirMul;
      }
      return String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: 'base' }) * dirMul;
    });
    p.filteredRows = rows;
  }

  // Paint the ↑/↓ arrow on the active sort column and clear it everywhere else.
  function updateSortIndicators(card){
    const previewEl = card && card.querySelector('.table-preview');
    if (!previewEl) return;
    const p = card._preview || {};
    const s = p.sort || { col: null, dir: null };
    previewEl.querySelectorAll('.bc-col-sort').forEach(label => {
      const ind = label.querySelector('.bc-sort-ind');
      const isActive = (s.col && label.dataset.sortCol === s.col);
      label.classList.toggle('is-sorted-asc',  isActive && s.dir === 'asc');
      label.classList.toggle('is-sorted-desc', isActive && s.dir === 'desc');
      if (ind) {
        ind.textContent = isActive
          ? (s.dir === 'asc' ? ' ▲' : ' ▼')
          : '';
      }
    });
  }

  function hasActiveFilters(p){
    if (!p || !p.filters) return false;
    for (const k in p.filters) {
      const f = p.filters[k];
      if (f && f.allowed) return true;
    }
    return false;
  }

  function applyPreviewFilters(card){
    const p = card._preview;
    if (!p) return;
    const active = [];
    for (const col in (p.filters || {})) {
      const f = p.filters[col];
      if (f && f.allowed) active.push([col, f.allowed]);
    }
    if (!active.length) {
      p.filteredRows = p.rows.slice();
    } else {
      p.filteredRows = p.rows.filter(row => {
        for (const [col, allowed] of active) {
          const cellVal = row[col] == null ? '' : String(row[col]);
          if (!allowed.has(cellVal)) return false;
        }
        return true;
      });
    }
    // Sort runs AFTER filtering so the two compose: filter narrows the row
    // set, sort orders what remains. If no sort is active this is a no-op
    // beyond a defensive re-derive of filteredRows.
    if (p.sort && p.sort.col && p.sort.dir) applyPreviewSort(card);
    p.page = 1;
    renderPreviewPage(card);
    updateTableActionVisibility(card);
    updateSortIndicators(card);
  }

  function updateTableActionVisibility(card){
    const p = card._preview;
    if (!p) return;
    const dlFilt = card.querySelector('[data-action="download-filtered-csv"]');
    const loadFull = card.querySelector('[data-action="load-full-table"]');
    const hasFilter = hasActiveFilters(p);
    if (dlFilt) dlFilt.classList.toggle('bc-hidden', !hasFilter);
    if (loadFull) {
      const partial = !p.loadedAll && (p.totalRows > p.rows.length);
      loadFull.classList.toggle('bc-hidden', !partial);
      if (partial) {
        let sizeNote = '';
        if (p.totalBytes) {
          const mb = p.totalBytes / 1048576;
          sizeNote = ' / ' + (mb < 10 ? mb.toFixed(1) : Math.round(mb)) + ' MB';
        }
        loadFull.textContent = 'Load full table (' + p.totalRows.toLocaleString() + ' rows' + sizeNote + ')';
      }
    }
  }

  function renderPreviewPage(card){
    try{
      const p = card._preview || {};
      const previewEl = card.querySelector('.table-preview');
      const labelEl   = card.querySelector('.pager-label');
      const prevBtn   = card.querySelector('.pager-prev');
      const nextBtn   = card.querySelector('.pager-next');
      const tbody     = previewEl && previewEl.querySelector('[data-tbody]');
      const metaEl    = previewEl && previewEl.querySelector('[data-meta]');
      if (!tbody) return;

      const filtered = p.filteredRows || [];
      const srcKey = p.srcKey || null;
      const cols = p.displayCols || (srcKey ? (p.cols || []).filter(c => c !== srcKey) : (p.cols || []));
      const colSpan = cols.length + (srcKey ? 1 : 0);
      const page = p.page || 1;
      const rpp = p.rpp || 5;
      const total = filtered.length;
      const totalPages = Math.max(1, Math.ceil(total/rpp));
      const start = (page-1)*rpp;
      const slice = filtered.slice(start, start+rpp);

      if (!slice.length){
        tbody.innerHTML = '<tr><td colspan="' + Math.max(1, colSpan) + '" class="bc-empty-placeholder">No rows match the current filters.</td></tr>';
      } else {
        tbody.innerHTML = slice.map(r => {
          const stickyTd = srcKey
            ? '<td class="' + _STICKY_TD_CLASS + '">' + _dbBadgeHtml(r[srcKey]) + '</td>'
            : '';
          return '<tr>' + stickyTd + cols.map(c => {
            const v = r[c]==null ? '' : String(r[c]);
            const safe = esc(v);
            return '<td class="bc-cell"><div class="bc-cell-inner">' + safe + '</div></td>';
          }).join('') + '</tr>';
        }).join('');
      }

      if (labelEl) labelEl.textContent = 'Page ' + page + ' of ' + totalPages;
      if (prevBtn) prevBtn.disabled = (page<=1);
      if (nextBtn) nextBtn.disabled = (page>=totalPages);

      const hasFilter = hasActiveFilters(p);
      if (metaEl){
        if (hasFilter){
          const partialNote = p.loadedAll
            ? ''
            : ' (filter sees ' + p.rows.length.toLocaleString() + ' of ' + p.totalRows.toLocaleString() + ' rows; use Load full table to filter all)';
          metaEl.textContent = total.toLocaleString() + ' matching row' + (total===1?'':'s') + partialNote;
        } else if (!total) {
          metaEl.textContent = '';
        } else {
          const lhs = 'rows ' + (start+1) + '-' + Math.min(start+slice.length,total) + ' of ' + total.toLocaleString();
          const rhs = (p.loadedAll || total >= p.totalRows) ? '' : ' (preview, ' + p.totalRows.toLocaleString() + ' total)';
          metaEl.textContent = lhs + rhs;
        }
      }
      scrollEnd();
    }catch(e){
      console.error('[preview] render error:', e);
    }
  }

  // ----- Excel-style per-column filter popup -----------------------------
  function installColFilterCloseHandler(){
    if (window.__bcColFilterCloseInstalled) return;
    window.__bcColFilterCloseInstalled = true;
    document.addEventListener('click', (e) => {
      document.querySelectorAll('.col-filter-popup').forEach(pop => {
        if (pop.contains(e.target)) return;
        if (e.target.closest && e.target.closest('.col-filter-btn')) return;
        pop.remove();
      });
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        document.querySelectorAll('.col-filter-popup').forEach(pop => pop.remove());
      }
    });
    window.addEventListener('resize', () => {
      document.querySelectorAll('.col-filter-popup').forEach(pop => pop.remove());
    });
  }

  function updateFilterButtonIndicator(card, col){
    const btn = card.querySelector('.col-filter-btn[data-col="' + (window.CSS && CSS.escape ? CSS.escape(col) : col.replace(/"/g,'\\"')) + '"]');
    if (!btn) return;
    const f = card._preview && card._preview.filters && card._preview.filters[col];
    if (f && f.allowed) btn.classList.add('has-filter');
    else btn.classList.remove('has-filter');
  }

  function openColFilterPopup(card, anchorBtn, col){
    // Toggle: re-clicking the same column's button closes the popup.
    const existing = document.querySelector('.col-filter-popup');
    if (existing) {
      const wasSameCol = existing.dataset.col === col;
      existing.remove();
      if (wasSameCol) return;
    }

    const p = card._preview;
    if (!p) return;
    // Unique values from the currently-loaded rows.
    const valSet = new Set();
    for (const r of p.rows) valSet.add(r[col] == null ? '' : String(r[col]));
    const allVals = Array.from(valSet).sort((a, b) =>
      a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
    );
    const MAX_LIST = 1000;
    const tooMany = allVals.length > MAX_LIST;
    const displayVals = tooMany ? allVals.slice(0, MAX_LIST) : allVals;

    const currentAllowed = p.filters && p.filters[col] && p.filters[col].allowed;
    const isChecked = (v) => !currentAllowed || currentAllowed.has(v);

    const escAttr = (s)=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const partialNote = p.loadedAll
      ? ''
      : '<div class="col-filter-partial-note">Showing values from ' + p.rows.length.toLocaleString() + ' of ' + p.totalRows.toLocaleString() + ' loaded rows. Load full table to see all.</div>';

    const popup = document.createElement('div');
    popup.className = 'col-filter-popup';
    popup.dataset.col = col;
    popup.innerHTML =
      '<div class="col-filter-hdr">Filter <strong>' + esc(col) + '</strong></div>' +
      '<input class="col-filter-search" type="text" placeholder="Search values..." />' +
      partialNote +
      '<label class="col-filter-all-row">' +
        '<input type="checkbox" class="col-filter-select-all" />' +
        '<span><strong>(Select All)</strong></span>' +
      '</label>' +
      '<div class="col-filter-list">' +
        displayVals.map(v =>
          '<label class="col-filter-item">' +
            '<input type="checkbox" class="col-filter-cb" data-val="' + escAttr(v) + '"' + (isChecked(v) ? ' checked' : '') + ' />' +
            '<span>' + (v === '' ? '<em>(Blanks)</em>' : esc(v)) + '</span>' +
          '</label>'
        ).join('') +
        (tooMany
          ? '<div class="col-filter-overflow"><em>Showing ' + MAX_LIST.toLocaleString() + ' of ' + allVals.length.toLocaleString() + ' values. Use search to narrow.</em></div>'
          : '') +
      '</div>' +
      '<div class="col-filter-actions">' +
        '<button type="button" class="reconnect" data-popup-action="clear">Clear filter</button>' +
        '<button type="button" class="reconnect" data-popup-action="close">Done</button>' +
      '</div>';
    document.body.appendChild(popup);

    // Position the popup fixed under the anchor button.
    const rect = anchorBtn.getBoundingClientRect();
    popup.style.position = 'fixed';
    const popupWidth = 300;
    let left = rect.left;
    if (left + popupWidth > window.innerWidth - 8) left = Math.max(8, window.innerWidth - popupWidth - 8);
    popup.style.left = left + 'px';
    popup.style.top = (rect.bottom + 4) + 'px';
    popup.style.zIndex = '1000';

    const searchInp = popup.querySelector('.col-filter-search');
    const selectAllCb = popup.querySelector('.col-filter-select-all');
    const list = popup.querySelector('.col-filter-list');
    const items = Array.from(list.querySelectorAll('.col-filter-item'));

    function refreshSelectAll(){
      const visibleCbs = [];
      items.forEach(it => {
        if (it.style.display !== 'none') visibleCbs.push(it.querySelector('.col-filter-cb'));
      });
      const allChecked = visibleCbs.length > 0 && visibleCbs.every(cb => cb.checked);
      const someChecked = visibleCbs.some(cb => cb.checked);
      selectAllCb.checked = allChecked;
      selectAllCb.indeterminate = !allChecked && someChecked;
    }

    function commitFromCheckboxes(){
      const checked = new Set();
      items.forEach(it => {
        const cb = it.querySelector('.col-filter-cb');
        if (cb.checked) checked.add(cb.dataset.val);
      });
      if (!p.filters) p.filters = {};
      if (checked.size === allVals.length) {
        delete p.filters[col];
      } else {
        p.filters[col] = { allowed: checked };
      }
      applyPreviewFilters(card);
      updateFilterButtonIndicator(card, col);
    }

    searchInp.addEventListener('input', () => {
      const q = searchInp.value.trim().toLowerCase();
      items.forEach(it => {
        const v = (it.querySelector('.col-filter-cb').dataset.val || '').toLowerCase();
        it.style.display = (!q || v.indexOf(q) !== -1) ? '' : 'none';
      });
      refreshSelectAll();
    });

    selectAllCb.addEventListener('change', () => {
      const tgt = selectAllCb.checked;
      items.forEach(it => {
        if (it.style.display !== 'none') {
          it.querySelector('.col-filter-cb').checked = tgt;
        }
      });
      commitFromCheckboxes();
    });

    list.addEventListener('change', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('col-filter-cb')) {
        commitFromCheckboxes();
        refreshSelectAll();
      }
    });

    popup.addEventListener('click', (e) => {
      const act = e.target.closest && e.target.closest('[data-popup-action]');
      if (!act) return;
      const name = act.dataset.popupAction;
      if (name === 'clear') {
        items.forEach(it => { it.querySelector('.col-filter-cb').checked = true; });
        commitFromCheckboxes();
        refreshSelectAll();
      } else if (name === 'close') {
        popup.remove();
      }
    });

    refreshSelectAll();
    setTimeout(() => searchInp.focus(), 0);
  }
  // -----------------------------------------------------------------------

  async function loadFullTableFromCard(btn){
    const card = btn.closest('.tool-card');
    const p = card && card._preview;
    if (!p || !p.csvPath || p.loadedAll) return;

    const url = _csvUrl(p.csvPath);

    // Size guard. The /download endpoint is registered with FastAPI's
    // @app.get and rejects HEAD (405), so we either reuse the byte size
    // captured by the preview fetch or do a 1-byte Range probe — both work
    // against the existing server without changes.
    let bytes = p.totalBytes || 0;
    if (!bytes) {
      try {
        const probe = await fetch(url, { headers: { Range: 'bytes=0-0' } });
        const cr = probe.headers.get('Content-Range') || '';
        const m = cr.match(/\/(\d+)\s*$/);
        if (m) bytes = Number(m[1]);
        else bytes = Number(probe.headers.get('Content-Length') || 0);
        p.totalBytes = bytes;
      } catch { /* probe failed -- fall through, no guard */ }
    }

    const SAFE_LIMIT = 50 * 1024 * 1024;  // 50 MB
    if (bytes > SAFE_LIMIT) {
      const mb = (bytes / 1024 / 1024).toFixed(0);
      const memEstMb = (bytes * 3 / 1024 / 1024).toFixed(0);
      const ok = confirm(
        'This table is ' + mb + ' MB (' + p.totalRows.toLocaleString() + ' rows).\n\n' +
        'Loading the whole file for filtering will use roughly ' + memEstMb + ' MB of browser memory and may take a minute.\n\n' +
        'Continue?\n\n' +
        '(The plain Download CSV link works at any size.)'
      );
      if (!ok) return;
    }

    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Loading...';
    try{
      const res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      let text = await res.text();
      text = (text || '').replace(/^﻿/, '');
      if (text && text[text.length-1] !== '\n') text += '\n';
      const parsed = parseCSVHead(text, Number.MAX_SAFE_INTEGER);
      const rows = parsed.rows || [];
      if (!rows.length) throw new Error('parsed empty');
      p.rows = rows;
      p.cols = (parsed.columns && parsed.columns.length) ? parsed.columns : p.cols;
      p.loadedAll = true;
      p.totalRows = rows.length;
      // Any open popup was built from the partial value set; close them so
      // the next open rebuilds against the full set.
      document.querySelectorAll('.col-filter-popup').forEach(pop => pop.remove());
      applyPreviewFilters(card);  // re-apply active filters across the full set
      btn.style.display = 'none';
    }catch(err){
      console.error('[loadFullTable] failed:', err);
      btn.disabled = false;
      btn.textContent = original;
      alert('Could not load the full table. Use the Download CSV link instead.');
    }
  }

  function downloadFilteredCSVFromCard(btn){
    const card = btn.closest('.tool-card');
    const p = card && card._preview;
    if (!p) return;
    const rows = p.filteredRows || [];
    if (!rows.length) { alert('No rows match the current filters.'); return; }
    const cols = p.cols || Object.keys(rows[0] || {});
    const escv = (v)=>{ const s = (v==null?'':String(v)); return /[",\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s; };
    const csv = cols.join(',') + '\n' + rows.map(r => cols.map(c => escv(r[c])).join(',')).join('\n');
    let stem = 'table';
    try { stem = (p.csvPath || '').split('/').pop().replace(/\.csv$/i,'') || 'table'; } catch {}
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = stem + '_filtered.csv';
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(a.href);
    a.remove();
  }
  window.loadFullTableFromCard = loadFullTableFromCard;
  window.downloadFilteredCSVFromCard = downloadFilteredCSVFromCard;

  function extractTableArray(raw){
    try{
      const obj = (typeof raw==='string') ? JSON.parse(raw) : raw;
      const t = obj && (obj.table ?? (obj.item && obj.item.output && obj.item.output.table));
      if (Array.isArray(t)) return t;
    }catch(e){}
    if (typeof raw === 'string'){
      const m = raw.match(/table\s*=\s*(\[[\s\S]*?\])\s*(?:\w+\s*=|$)/i);
      if (m){
        let arrTxt = m[1];
        let jsonish = arrTxt
          .replace(/([{,\s])'([^']+?)'\s*:/g, '$1"$2":')
          .replace(/:\s*'([^']*)'/g, ': "$1"')
          .replace(/None\b/g, 'null').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false');
        try{
          const t = JSON.parse(jsonish);
          if (Array.isArray(t)) return t;
        }catch(e){}
      }
    }
    return null;
  }
  function toCSV(rows){
    if(!rows || !rows.length) return '';
    const cols = Array.from(new Set(rows.flatMap(r => Object.keys(r))));
    const escv = (v)=>{
      const s = (v==null ? '' : String(v));
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s;
    };
    const head = cols.join(',');
    const body = rows.map(r => cols.map(c => escv(r[c])).join(',')).join('\n');
    return head + '\n' + body;
  }
  function parseCSVHead(text, maxRows){
    const lines = text.split(/\r?\n/);
    if(!lines.length) return { columns:[], rows:[] };
    let idx=0; while(idx<lines.length && !lines[idx].trim()) idx++;
    if(idx>=lines.length) return { columns:[], rows:[] };
    const columns = parseLine(lines[idx++].replace(/^\uFEFF/,''));

    const rows=[];
    for(; idx<lines.length && rows.length<(maxRows||50); idx++){
      const line = lines[idx]; if(!line) continue;
      const vals = parseLine(line);
      if(vals.length===1 && vals[0]==='') continue;
      const row={}; for(let c=0;c<columns.length;c++) row[columns[c]] = vals[c] ?? '';
      rows.push(row);
    }
    return { columns, rows };
    function parseLine(line){
      const out=[]; let i=0, cur='', inQ=false;
      while(i<line.length){
        const ch=line[i];
        if(inQ){
          if(ch === '"'){
            if(i+1<line.length && line[i+1]==='"'){ cur+='"'; i+=2; }
            else { inQ=false; i++; }
          }else{ cur+=ch; i++; }
        }else{
          if(ch === '"'){ inQ=true; i++; }
          else if(ch === ','){ out.push(cur); cur=''; i++; }
          else { cur+=ch; i++; }
        }
      }
      out.push(cur);
      return out;
    }
  }

  // function tableHtml(arr,caption){
  //   if(!Array.isArray(arr)||!arr.length) return `<div style="color:var(--mut);font-style:italic;padding:6px 2px">[Empty Table]</div>`;
  //   const cols=Object.keys(arr[0]);
  //   const thead=`<thead><tr>${cols.map(k=>`<th style="position:sticky;top:0;background:var(--bg3);color:var(--txt);padding:10px 12px;border-bottom:1px solid var(--brd);text-align:left;font-size:13px">${k.replace(/_/g,' ')}</th>`).join('')}</tr></thead>`;
  //   const tbody=arr.map(r=>`<tr>${cols.map(c=>{
  //     const v = r[c]==null ? '' : String(r[c]);
  //     const safe = esc(v);
  //     return `<td style="padding:9px 12px;border-bottom:1px solid var(--brd)"><div style="overflow-wrap:anywhere; white-space:normal" title="${safe}">${safe}</div></td>`;
  //   }).join('')}</tr>`).join('');
  //   const cap=caption?`<caption style="text-align:left;color:var(--accent);padding:8px 6px 6px;font-weight:800">🔎 ${esc(caption)}</caption>`:'';
  //   return `<div style="width:100%;overflow:auto;margin:.3em 0 .7em 0"><table style="border-collapse:separate;border-spacing:0;width:100%;background:var(--card);border:1px solid var(--brd);border-radius:11px">${cap}${thead}<tbody>${tbody}</tbody></table></div>`;
  // }

  // ---------- Source-DB provenance: badge colors + legend -----------------
  // The federated query stamps each row with `_source_db`. We hoist that
  // column out of the normal grid and render it as a sticky-left colored
  // badge so users can see provenance at a glance, plus a legend strip
  // above the table.
  const SOURCE_DB_KEY = '_source_db';
  const SOURCE_DB_KEY_ALT = 'source_db';

  function pickSourceDbKey(cols){
    if (!Array.isArray(cols)) return null;
    if (cols.indexOf(SOURCE_DB_KEY) !== -1)     return SOURCE_DB_KEY;
    if (cols.indexOf(SOURCE_DB_KEY_ALT) !== -1) return SOURCE_DB_KEY_ALT;
    return null;
  }

  function _dbColor(name){
    // Stable hash-to-hue so the same DB always gets the same swatch.
    const s = String(name || '').toLowerCase();
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    const hue = h % 360;
    // Slightly different background/foreground for light vs dark theme.
    // The CSS variables already adapt, but we want a vivid swatch in both.
    return {
      bg:   `hsl(${hue} 70% 42% / 0.22)`,
      bdr:  `hsl(${hue} 70% 50% / 0.55)`,
      fg:   `hsl(${hue} 90% 78%)`,
      dot:  `hsl(${hue} 75% 55%)`,
    };
  }

  function _dbBadgeHtml(db){
    if (db == null || String(db).trim() === '') {
      return `<span class="bc-db-badge--empty">—</span>`;
    }
    const c = _dbColor(db);
    const safe = esc(String(db));
    // Dynamic per-DB color comes through CSS custom properties so the badge
    // class itself is static; this stops the inline declaration from
    // exploding the DOM size and keeps CSP options open.
    const cssVars = `--bc-bg:${c.bg};--bc-bdr:${c.bdr};--bc-fg:${c.fg};--bc-dot:${c.dot}`;
    return `<span class="bc-db-badge" title="Source: ${safe}" style="${cssVars}">`
      + `<span class="bc-db-dot" aria-hidden="true"></span>${safe}`
      + `</span>`;
  }

  function _uniqueDbsFromRows(rows, srcKey){
    const set = new Set();
    if (!Array.isArray(rows) || !srcKey) return [];
    for (const r of rows){
      if (!r || typeof r !== 'object') continue;
      const v = r[srcKey];
      if (v != null && String(v).trim() !== '') set.add(String(v));
    }
    return Array.from(set).sort((a,b)=>a.localeCompare(b));
  }

  function _dbLegendStripHtml(dbs){
    if (!Array.isArray(dbs) || !dbs.length) return '';
    const items = dbs.map(d => _dbBadgeHtml(d)).join(' ');
    return `<div class="db-legend bc-db-legend">`
      + `<span class="bc-db-legend-title">Sources</span>`
      + items
      + `</div>`;
  }

  // Marker classes used by tableHtml — kept as named constants so the few
  // remaining inline-style escape hatches (paginated preview view) reference
  // the same class names that chat-shared.css carries.
  const _STICKY_TH_CLASS = 'bc-sticky-th';
  const _STICKY_TD_CLASS = 'bc-sticky-td';

  function emptyTableHtml(opts){
    // opts: { db, parsedTerm, failureCause, failureMessage, elapsedSeconds, suggestBroader }
    const db = opts && opts.db ? _dbLong(opts.db) : 'The database';
    const parsed = opts && opts.parsedTerm ? String(opts.parsedTerm) : '';
    const cause  = opts && opts.failureCause ? String(opts.failureCause) : '';
    const msg    = opts && opts.failureMessage ? String(opts.failureMessage) : '';
    const elapsed = opts && opts.elapsedSeconds != null ? Number(opts.elapsedSeconds) : null;

    // Compose the headline. Transport failure (timeout / network / http) is
    // surfaced verbatim; an honest 0-row result gets a different, calmer
    // sentence with the parsed query so the user can see whether the
    // interpreter misread their question.
    let headline;
    if (cause === 'timeout') {
      const ms = elapsed != null ? ` (${elapsed.toFixed(1)} s)` : '';
      headline = `${esc(db)} timed out${ms}`;
    } else if (cause === 'network' || cause === 'http_error' || cause === 'error' || cause === 'unknown_db') {
      headline = `${esc(db)} could not return a result${msg ? ': ' + esc(msg) : ''}`;
    } else if (parsed) {
      headline = `${esc(db)} returned no rows matching <strong>${esc(parsed)}</strong>.`;
    } else {
      headline = `${esc(db)} returned no rows for this query.`;
    }

    const interpLine = (parsed && cause !== 'timeout' && cause !== 'network' && cause !== 'http_error')
      ? `<div class="bc-empty-note">The interpreter read your query as: <code>${esc(parsed)}</code></div>`
      : '';

    const broader = opts && opts.suggestBroader !== false
      ? `<div class="bc-empty-note">Try broadening your query (e.g. drop modifiers, use the parent term).</div>`
      : '';

    return (
      `<div class="bc-empty-table">`
      + `<div>${headline}</div>`
      + interpLine
      + broader
      + `</div>`
    );
  }

  function tableHtml(arr, caption, opts){
    if(!Array.isArray(arr)||!arr.length)
      return emptyTableHtml(opts || {});
    const allCols = Object.keys(arr[0]);
    const srcKey = pickSourceDbKey(allCols);
    const cols = srcKey ? allCols.filter(c => c !== srcKey) : allCols;

    const stickyTh = srcKey
      ? `<th class="${_STICKY_TH_CLASS}">Source</th>`
      : '';

    const thead = `<thead><tr>${stickyTh}${
      cols.map(k =>
        `<th>${esc(k.replace(/_/g," "))}</th>`
      ).join('')
    }</tr></thead>`;

    const tbody = arr.map(r => {
      const stickyTd = srcKey
        ? `<td class="${_STICKY_TD_CLASS}">${_dbBadgeHtml(r[srcKey])}</td>`
        : '';
      return `<tr>${stickyTd}${
        cols.map(c => {
          const v = r[c]==null ? '' : String(r[c]);
          const safe = esc(v);
          return `<td class="bc-cell"><div class="bc-cell-inner">${safe}</div></td>`;
        }).join('')
      }</tr>`;
    }).join('');

    const cap = caption
      ? `<caption class="bc-caption">🔎 ${esc(caption)}</caption>`
      : '';

    const legend = srcKey ? _dbLegendStripHtml(_uniqueDbsFromRows(arr, srcKey)) : '';

    return `<div class="bc-table-wrap">`
      + legend
      + `<div class="bc-table-scroll">`
      + `<table class="bc-table">${cap}${thead}<tbody>${tbody}</tbody></table>`
      + `</div>`
      + `</div>`;
  }



  function copyCSVFromCard(btn){
    const card = btn.closest('.tool-card');
    const csv = card?.dataset?.csv || '';
    if (!csv) return;
    navigator.clipboard.writeText(csv).then(()=>{
      btn.textContent='Copied!';
      setTimeout(()=>btn.textContent='Copy CSV',900);
    });
  }
  function downloadCSVFromCard(btn){
    const card = btn.closest('.tool-card');
    const csv = card?.dataset?.csv || '';
    const name = card?.dataset?.csvFilename || 'table.csv';
    if (!csv) return;
    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(a.href);
    a.remove();
  }

  const messageInput=document.getElementById('messageInput');
  const sendButton=document.getElementById('sendButton');
  const quickAsks=document.getElementById('quickAsks');
  messageInput.addEventListener('keydown',e=>{
    if(e.key==='Enter' && !e.shiftKey){
      e.preventDefault();
      sendMessage();
    }
  });
  if (quickAsks){
    quickAsks.addEventListener('click', (e)=>{
      const btn = e.target.closest('.qa-card');
      if (!btn) return;
      const question = btn.getAttribute('data-question') || btn.textContent || '';
      if (question.trim()) askPreset(question.trim());
    });
  }
  messageInput.addEventListener('input',function(){
    this.style.height='auto';
    this.style.height=Math.min(this.scrollHeight,150)+'px';
    const qa = document.getElementById('quickAsks');
    if (qa && messageInput.value.trim().length) qa.style.display = 'none';
  });

  function addUser(text){
    setEmptyState(false);
    // Start a fresh turn so this question stays sticky over its answer.
    const turn = document.createElement('div'); turn.className = 'turn';
    // Stash the verbatim question on the turn element so the per-turn Share
    // button can generate a BibTeX entry without re-reading the bubble HTML
    // (which gets markdown-rendered with surrounding "Q:" decoration).
    turn.dataset.userQuestion = String(text || '');
    turn.dataset.userTimestamp = new Date().toISOString();
    document.getElementById('messagesContainer').appendChild(turn);
    currentTurn = turn;
    const el=document.createElement('div'); el.className='message user';
    el.innerHTML=`<div class="message-avatar" title="You">👤</div>
      <div class="message-content">
        <div class="timestamp">${nowStamp()}</div>
        <div class="message-bubble"></div>
      </div>`;
    el.querySelector('.message-bubble').textContent = text;
    turn.appendChild(el);
    scrollEnd();
  }

  function kickOffStream(){
    stopFauxOrchStream();
    orchestratorDone=false;
    startStreaming();
    currentAssistantMessage=null; currentToolCards.clear();
    orchestratorBuf=''; toolBuffers.clear(); toolCount=0;
    pendingUI.clear();
    suppressOrchStream=false;
    orchVisibleStreamed=false;
    toolStartTimes.clear(); toolElapsed.clear();
    // Reset per-turn few-shot state captured from orch_step_summary events.
    _currentParsedValue    = {};
    _currentRephrasedQuery = '';
  }

  function sendMessage(){
    const txt=messageInput.value.trim();
    if(!txt || !ws || ws.readyState!==WebSocket.OPEN) return;
    addUser(txt);
    const byokFields=window.biochirpBYOK?window.biochirpBYOK.buildPayloadFields():{};
    ws.send(JSON.stringify({user_input:txt, session_id: activeSessionId, ...byokFields}));
    messageInput.value=''; messageInput.style.height='auto';
    kickOffStream();
    const qa = document.getElementById('quickAsks');
    if (qa) qa.style.display = 'none';
  }

  function askPreset(text){
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    addUser(text);
    const byokFields=window.biochirpBYOK?window.biochirpBYOK.buildPayloadFields():{};
    ws.send(JSON.stringify({ user_input: text, session_id: activeSessionId, ...byokFields }));
    messageInput.value=''; messageInput.style.height='auto';
    kickOffStream();
    const qa = document.getElementById('quickAsks');
    if (qa) qa.style.display = 'none';
  }
  function showQuickAsks(){
    setEmptyState(true);
  }

  function initTheme(){
    try{
      const saved = localStorage.getItem('biochirp_theme');
      const initial = saved || 'light';
      document.documentElement.setAttribute('data-theme', initial);
    }catch{}
    document.getElementById('themeToggle')?.addEventListener('click', ()=>{
      const cur = document.documentElement.getAttribute('data-theme') || 'light';
      const next = (cur==='dark') ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try{ localStorage.setItem('biochirp_theme', next); }catch{}
    });
  }

  // ── Documentation modal handlers ───────────────────────────────────────
  // Same focus-trap pattern as the CTD/HCDT/DrugCentral chat pages.
  (function setupDocModal(){
    const docBtn      = document.getElementById('docBtn');
    const docOverlay  = document.getElementById('docOverlay');
    const docCloseBtn = document.getElementById('docCloseBtn');
    if (!docBtn || !docOverlay) return;
    let lastFocusedEl = null;
    function getFocusable(container){
      return [...container.querySelectorAll('button,[href],input,textarea,select,[tabindex]:not([tabindex="-1"])')]
        .filter(el => !el.disabled && el.offsetParent !== null);
    }
    function openDocModal(){
      lastFocusedEl = document.activeElement;
      docOverlay.style.display = 'flex';
      docOverlay.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
      const f = getFocusable(docOverlay); if (f[0]) f[0].focus();
    }
    function closeDocModal(){
      docOverlay.style.display = 'none';
      docOverlay.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
      if (lastFocusedEl && typeof lastFocusedEl.focus === 'function') lastFocusedEl.focus();
    }
    docBtn.addEventListener('click', openDocModal);
    if (docCloseBtn) docCloseBtn.addEventListener('click', closeDocModal);
    docOverlay.addEventListener('click', (e) => {
      if (e.target === docOverlay) closeDocModal();
    });
    document.addEventListener('keydown', (e) => {
      if (docOverlay.style.display !== 'flex') return;
      if (e.key === 'Escape'){ e.preventDefault(); closeDocModal(); return; }
      if (e.key === 'Tab'){
        const focusables = getFocusable(docOverlay);
        if (!focusables.length) return;
        const first = focusables[0], last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first){
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last){
          e.preventDefault(); first.focus();
        }
      }
    });
  })();

  document.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q');
    if (!q || !q.trim()) return;

    initialQFromURL = q.trim();

    // Prefill the textbox so the user sees what was asked
    const input = document.getElementById('messageInput');
    if (input) {
      input.value = initialQFromURL;
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 150) + 'px';
    }
  });
