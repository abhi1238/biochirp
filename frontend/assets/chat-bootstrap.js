/* === BioChirp db_chat bootstrap (2026-05-17) =================================
   Runs SYNCHRONOUSLY to set window.__BC_WS_URL before the main <script> below
   defines const WS_URL. Then asynchronously fetches /configs/db_chats.json and
   patches the title, header, placeholder, quick-asks, welcome bubble, and doc
   panel for the requested ?db=<slug>. */
(function bcBootstrap(){
  // Slug resolution: URL `?db=` wins; otherwise a page-level default set via
  // `window.__BC_DEFAULT_SLUG = '...'` before this script loads (front-door
  // pages like hcdt_api.html set it so a bare visit lands on the right slug).
  const slug = (new URLSearchParams(location.search).get('db')
                || window.__BC_DEFAULT_SLUG
                || '').toLowerCase().trim();
  // Each schema_kg DB runs its own self-contained orchestrator chat at
  // /<slug>_chat/ (shared code in app/per_db_tool/schema_kg_chat.py).
  // 2026-06-18: the MULTI-DB aggregate (multi / multi_v2 / bio → /bio_chat/,
  // port 8030) was DECOMMISSIONED and nothing serves it.
  // `opentarget` is the 11th DB (GraphQL OpenTargets); its chat routes to
  // opentarget_service's OWN WebSocket at /opentarget/ → port 8026
  // (nginx `location ^~ /opentarget/` proxies to 8026).
  const SPECIAL_WS = {
    'opentarget':   '/opentarget/',
    // schema_kg per-DB chats (mirror SCHEMA_KG_DBS in scripts/gen_compose.py)
    'hcdt':         '/hcdt_chat/',
    'ttd':          '/ttd_chat/',
    'ctd':          '/ctd_chat/',
    'uniprot':      '/uniprot_chat/',
    'reactome':     '/reactome_chat/',
    'clinvar':      '/clinvar_chat/',
    'hpo':          '/hpo_chat/',
    'string':       '/string_chat/',
    'msigdb':       '/msigdb_chat/',
    'orphanet':     '/orphanet_chat/',
  };
  const wsPath = SPECIAL_WS[slug];
  // No valid chat for this slug — including the removed multi-DB / bio_chat
  // aggregate — so bounce the visitor back to the picker instead of opening a
  // dead WebSocket.
  if (!wsPath) { location.replace('/'); return; }
  const wsProto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
  window.__BC_WS_URL = wsProto + '//' + location.host + wsPath;
  window.__BC_SLUG = slug;
  window.__BC_DB_HINT = null;

  // Async: load config and patch DOM
  // cache:'no-store' so doc-panel content always reflects the latest
  // db_chats.json after a deploy — otherwise the browser keeps the stale
  // doc_full / samples until the user manually hard-reloads.
  fetch('/configs/db_chats.json', {cache: 'no-store'}).then(r => r.ok ? r.json() : null).then(all => {
    if (!all || !all[slug]) {
      console.warn('[db_chat] missing config for', slug);
      return;
    }
    const c = all[slug];
    // <title>
    document.title = 'BioChirp — ' + c.name;
    // Header chat-title (icon + name + subtitle)
    const titleEl = document.getElementById('bcChatTitle');
    if (titleEl) {
      const subtitle = c.is_multi
        ? 'Auto-routes across all 11 BioChirp databases'
        : ((c.entity_types || []).slice(0, 3).join(', ') || c.name);
      titleEl.innerHTML =
        '<span style="margin-right:6px">' + (c.icon || '🧬') + '</span>' +
        c.name + ' <small>• ' + subtitle + '</small>';
    }
    // Textarea placeholder
    const ta = document.getElementById('messageInput');
    if (ta) ta.placeholder = c.is_multi
      ? 'Ask any biomedical question — BioChirp picks the right databases automatically…'
      : ('Ask ' + c.name + ' about ' + ((c.entity_types || []).slice(0,4).join(', ') || 'biomedical data') + '…');
    // Accent color (CSS variable)
    if (c.accent) document.documentElement.style.setProperty('--accent', c.accent);
    // Quick-asks (re-fill the empty container)
    const qa = document.getElementById('quickAsks');
    if (qa) {
      qa.innerHTML = '';
      const qaSamples = (c.samples || []).slice(0, 8);
      // Fit small sample counts (e.g. TTD's 5) into a single desktop row
      // instead of always wrapping at the CSS default of 4. Above 6 columns
      // cards get too cramped on a typical viewport, so larger sample sets
      // (7-8) fall back to the default 4-col wrap instead of forcing 7-8
      // across one row. Narrow-viewport media queries in chat-shared.css
      // override this back down to 2/1 columns regardless.
      qa.style.setProperty('--qa-cols', qaSamples.length > 0 && qaSamples.length <= 6 ? qaSamples.length : 4);
      qaSamples.forEach(q => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'qa-card';
        b.dataset.question = q;
        b.textContent = q.length > 60 ? q.slice(0, 57) + '…' : q;
        qa.appendChild(b);
      });
    }
    // Welcome bubble — chat-bootstrap.js runs BEFORE chat-main.js creates
    // the #bcWelcomeBubble element, so the immediate patch usually misses.
    // Stash the markdown on window AND retry patching via a brief
    // MutationObserver until the element appears (cap ~5s to avoid leaks).
    const md = c.welcome || '';
    window.__BC_WELCOME_MD = md;
    function _renderWelcome(el){
      if (!el) return false;
      if (window.marked && window.DOMPurify) {
        el.innerHTML = window.DOMPurify.sanitize(window.marked.parse(md));
      } else {
        el.textContent = md;
      }
      el.dataset.bcRendered = '1';
      return true;
    }
    // First try synchronously.
    _renderWelcome(document.getElementById('bcWelcomeBubble'));
    // chat-main.js RECREATES the bubble on every chat reset with the
    // hardcoded "Loading welcome message…" placeholder. Keep watching
    // forever so we patch each new instance (idempotent via the
    // dataset.bcRendered guard). No timeout — the observer is cheap.
    const obs = new MutationObserver(() => {
      const el = document.getElementById('bcWelcomeBubble');
      if (el && el.dataset.bcRendered !== '1'){
        _renderWelcome(el);
      }
    });
    obs.observe(document.body, {childList: true, subtree: true});
    // Doc overlay — prefer the detailed `doc_full` field (full markdown:
    // tool-roles table, headings, scope-out lists, schema fields, tables).
    // Fall back to `welcome` for backward compat with older config entries.
    const docMd = c.doc_full || c.welcome || '';
    const db = document.getElementById('bcDocBody');
    if (db) {
      const samples = (c.samples || []).map(q => '<li>' + q.replace(/[&<>"']/g, ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])) + '</li>').join('');
      const entities = (c.entity_types || []).join(', ') || '—';
      const docHtml = (window.marked && window.DOMPurify)
        ? window.DOMPurify.sanitize(window.marked.parse(docMd))
        : '<p>' + docMd.replace(/[&<>]/g, ch=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch])) + '</p>';
      db.innerHTML =
        '<h4>What this chat answers</h4>' +
        docHtml +
        '<h4>Entity types this database covers</h4>' +
        '<p><code>' + entities + '</code></p>' +
        '<h4>Sample questions</h4>' +
        '<ul>' + samples + '</ul>';
    }
    // Doc title
    const dt = document.getElementById('docTitle');
    if (dt) dt.textContent = c.name + ' — Scope & Sample Questions';

    // Sidebar (db_chat.html only — elements absent in multi_db_chat.html)
    const sidebarName = document.getElementById('sidebarDbName');
    if (sidebarName) {
      sidebarName.textContent = c.name || slug.toUpperCase();
      const iconEl = document.getElementById('sidebarDbIcon');
      if (iconEl) iconEl.textContent = c.icon || '🧬';
      if (c.accent) {
        const sidebar = document.getElementById('dbSidebar');
        if (sidebar) sidebar.style.setProperty('--accent', c.accent);
      }
      const entityChips = document.getElementById('sidebarEntityChips');
      const entitySection = document.getElementById('sidebarEntitySection');
      if (entityChips && c.entity_types && c.entity_types.length) {
        entityChips.innerHTML = c.entity_types.map(function(t){
          return '<span class="sidebar-entity-chip">' + t.replace(/[&<>"']/g, function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];}) + '</span>';
        }).join('');
        if (entitySection) entitySection.style.display = '';
      }
      const samplesEl = document.getElementById('sidebarSamples');
      const samplesSection = document.getElementById('sidebarSamplesSection');
      if (samplesEl && c.samples && c.samples.length) {
        samplesEl.innerHTML = c.samples.slice(0, 8).map(function(q){
          var safe = q.replace(/[&<>"']/g, function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];});
          var label = q.length > 58 ? q.slice(0, 55) + '…' : q;
          return '<button class="sidebar-sample" data-action="ask-preset" data-preset="' + safe + '">' + label + '</button>';
        }).join('');
        if (samplesSection) samplesSection.style.display = '';
      }
    }
  }).catch(err => console.error('[db_chat] config load failed', err));
})();
