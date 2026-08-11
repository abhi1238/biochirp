// BioChirp insights.js — adds two user-insight features to every chat page:
//   1. Snapshot freshness chip prepended to the synthesizer's answer bubble,
//      parsed from the trailing "Source: … | Version: … | Snapshot: YYYY-MM-DD"
//      line that the backend synthesizer emits.
//   2. In-table search and per-column sort wired into the existing paginated
//      preview produced by initPaginatedPreview / renderPreviewPage.
//
// No backend changes. The shared MutationObserver picks up new bubbles and
// table cards as they stream in. Idempotent: re-running on the same element
// is a no-op.

(function(){
  'use strict';
  if (window.__bioInsightsLoaded) return;
  window.__bioInsightsLoaded = true;

  // ─── 1. Snapshot freshness chip ─────────────────────────────────────────
  // Use the global flag so we find ALL Source/Version/Snapshot triples in
  // multi-DB answers (e.g. bio_chat returns TTD + DrugCentral + ChEMBL etc.
  // and emits a Source line per DB). One chip per DB cited.
  const SNAPSHOT_FULL_RE_GLOBAL = /Source:\s*([^|\n]+?)\s*\|\s*Version:\s*([^|\n]+?)\s*\|\s*Snapshot(?:\s*date)?:\s*(\d{4}-\d{2}-\d{2})/gi;
  const SNAPSHOT_DATE_ONLY_RE = /Snapshot(?:\s*date)?:\s*(\d{4}-\d{2}-\d{2})/i;

  function ageString(dateStr){
    const then = new Date(dateStr + 'T00:00:00Z');
    if (isNaN(then.getTime())) return '';
    const ms = Date.now() - then.getTime();
    if (ms < 0) return 'future-dated';
    const days = Math.floor(ms / 86400000);
    if (days < 1) return 'today';
    if (days < 30) return days + ' day' + (days===1?'':'s') + ' old';
    const months = Math.floor(days / 30.4375);
    if (months < 12) return months + ' month' + (months===1?'':'s') + ' old';
    const years = days / 365.25;
    const yStr = (years % 1 < 0.05) ? years.toFixed(0) : years.toFixed(1);
    return yStr + ' year' + (yStr==='1'?'':'s') + ' old';
  }

  function freshnessTone(dateStr){
    const then = new Date(dateStr + 'T00:00:00Z').getTime();
    if (isNaN(then)) return 'aging';
    const days = (Date.now() - then) / 86400000;
    if (days < 180) return 'fresh';   // <6 months
    if (days < 540) return 'aging';   // 6-18 months
    return 'stale';                   // >18 months
  }

  function escHtml(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
  }

  function buildSnapshotChip(date, source, version){
    const wrap = document.createElement('div');
    wrap.className = 'bio-snapshot-chip tone-' + freshnessTone(date);
    wrap.setAttribute('data-bio-snapshot', '1');
    const age = ageString(date);
    let title;
    if (source && version) title = source.trim() + ' — ' + version.trim();
    else if (source)       title = source.trim();
    else                   title = 'Data snapshot';
    wrap.innerHTML =
      '<span class="bio-snapshot-icon">📅</span>' +
      '<span class="bio-snapshot-text"><strong>' + escHtml(title) + '</strong>' +
        ' · snapshot ' + escHtml(date) +
        (age ? ' <span class="bio-snapshot-age">(' + escHtml(age) + ')</span>' : '') +
      '</span>';
    return wrap;
  }

  function maybeAttachSnapshotChip(bubble){
    if (!bubble) return;
    const text = bubble.textContent || '';
    if (text.length < 20) return;  // skip stub renders

    // Collect ALL Source/Version/Snapshot triples in the bubble. Multi-DB
    // answers cite multiple sources — one chip per source.
    const found = [];
    const seen = new Set();
    let m;
    SNAPSHOT_FULL_RE_GLOBAL.lastIndex = 0;
    while ((m = SNAPSHOT_FULL_RE_GLOBAL.exec(text)) !== null) {
      const key = (m[1] + '|' + m[3]).toLowerCase().trim();
      if (seen.has(key)) continue;
      seen.add(key);
      found.push({ source: m[1].trim(), version: m[2].trim(), date: m[3] });
    }

    // Fallback: bare "Snapshot: YYYY-MM-DD" without surrounding Source/Version
    if (!found.length) {
      const dm = text.match(SNAPSHOT_DATE_ONLY_RE);
      if (dm) found.push({ source: '', version: '', date: dm[1] });
    }
    if (!found.length) return;

    // Idempotency: if the existing chips already cover the same set of
    // {source, date} pairs we found this pass, skip. Otherwise remove the
    // old chips (the answer may have grown to cite a new DB) and rebuild.
    const existing = bubble.querySelectorAll(':scope > [data-bio-snapshot]');
    const existingKeys = new Set();
    existing.forEach(c => {
      const k = c.getAttribute('data-bio-snapshot-key');
      if (k) existingKeys.add(k);
    });
    const newKeys = new Set(found.map(f => (f.source + '|' + f.date).toLowerCase()));
    const sameSet = existingKeys.size === newKeys.size &&
                    [...newKeys].every(k => existingKeys.has(k));
    if (sameSet) return;
    existing.forEach(c => c.remove());

    // Build a row of chips (one per cited source) and prepend it.
    const row = document.createElement('div');
    row.className = 'bio-snapshot-row';
    row.setAttribute('data-bio-snapshot', '1');
    found.forEach(f => {
      const chip = buildSnapshotChip(f.date, f.source, f.version);
      chip.setAttribute('data-bio-snapshot-key', (f.source + '|' + f.date).toLowerCase());
      row.appendChild(chip);
    });
    bubble.insertBefore(row, bubble.firstChild);
  }

  // ─── 2. Table search + per-column sort ─────────────────────────────────
  function enhanceTableCard(card){
    if (card.dataset.bioInsightsTable === '1') return;
    // Wait until initPaginatedPreview has populated card._preview AND exposed
    // _render (a small one-line patch added to that function — see
    // initPaginatedPreview in the per-page HTML).
    if (!card._preview || typeof card._preview._render !== 'function') return;
    card.dataset.bioInsightsTable = '1';

    const toolbar = card.querySelector('.table-toolbar');
    const previewEl = card.querySelector('.table-preview');
    if (!toolbar || !previewEl) return;

    // Snapshot the original (unfiltered) rows so we can restore on clear.
    if (!card._preview._originalRows){
      card._preview._originalRows = (card._preview.rows || []).slice();
    }

    // Build the search input.
    const searchWrap = document.createElement('div');
    searchWrap.className = 'bio-table-search';
    searchWrap.innerHTML =
      '<input type="search" placeholder="🔎 Filter rows…" aria-label="Filter rows" />' +
      '<span class="bio-match-count" aria-live="polite"></span>';
    // Insert before .table-actions so the layout stays: pager | search | actions
    const actions = toolbar.querySelector('.table-actions');
    if (actions) toolbar.insertBefore(searchWrap, actions);
    else toolbar.appendChild(searchWrap);

    const searchInput = searchWrap.querySelector('input');
    const matchCount  = searchWrap.querySelector('.bio-match-count');
    let debounceT;
    searchInput.addEventListener('input', ()=>{
      clearTimeout(debounceT);
      debounceT = setTimeout(()=>{
        card._bioSearch = searchInput.value;
        applyFilterSort(card, matchCount);
      }, 120);
    });

    // Sort: delegate clicks on <th> in the preview.
    previewEl.addEventListener('click', (ev)=>{
      const th = ev.target.closest('th');
      if (!th || !previewEl.contains(th)) return;
      const col = th.getAttribute('data-bio-col');
      if (!col) return;
      const prev = card._bioSort || {};
      const dir = (prev.col === col && prev.dir === 'asc') ? 'desc'
                : (prev.col === col && prev.dir === 'desc') ? null : 'asc';
      card._bioSort = dir ? { col, dir } : null;
      applyFilterSort(card, matchCount);
    });

    // Decorate headers on every re-render. We watch the .table-preview for
    // CHILD-LIST changes only (no subtree), because renderPreviewPage replaces
    // the whole inner table on each page/search/sort — that fires the
    // childList mutation we care about. Subtree:true caused an infinite
    // loop: decorateHeaders mutates the <th> nodes (classList, removeChild,
    // appendChild on the indicator span) which would fire the observer
    // again, locking the main thread when the table first loaded.
    let decorating = false;
    const headerObs = new MutationObserver(() => {
      if (decorating) return;
      decorating = true;
      try { decorateHeaders(card); } finally {
        // Release on next frame so any cascading mutations from our own
        // decoration are absorbed by the disconnect (they fire under the
        // flag and are skipped).
        requestAnimationFrame(() => { decorating = false; });
      }
    });
    headerObs.observe(previewEl, { childList:true });  // NO subtree:true
    decorateHeaders(card);
  }

  function decorateHeaders(card){
    const previewEl = card.querySelector('.table-preview');
    if (!previewEl) return;
    const ths = previewEl.querySelectorAll('thead th');
    if (!ths.length) return;
    const sort = card._bioSort || {};
    const sample = (card._preview && card._preview._originalRows && card._preview._originalRows[0]) || null;
    const cols = sample ? Object.keys(sample) : null;
    ths.forEach((th, i)=>{
      const col = cols ? cols[i]
        : (th.textContent || '').trim().replace(/\s+/g, '_').toLowerCase();
      if (!th.getAttribute('data-bio-col')) th.setAttribute('data-bio-col', col);
      th.classList.add('bio-sortable-th');
      th.classList.toggle('bio-sort-active', sort.col === col);
      // Replace any prior indicator.
      const old = th.querySelector('.bio-sort-ind');
      if (old) old.remove();
      const ind = document.createElement('span');
      ind.className = 'bio-sort-ind';
      if (sort.col === col) ind.textContent = (sort.dir === 'asc') ? ' ▲' : ' ▼';
      else                  ind.textContent = ' ⇅';
      th.appendChild(ind);
    });
  }

  function applyFilterSort(card, matchCountEl){
    const orig = (card._preview && card._preview._originalRows) || [];
    let view = orig;
    const q = (card._bioSearch || '').trim().toLowerCase();
    if (q){
      view = view.filter(row => {
        for (const k in row){
          const v = row[k];
          if (v != null && String(v).toLowerCase().includes(q)) return true;
        }
        return false;
      });
    }
    const sort = card._bioSort;
    if (sort && sort.col){
      const dir = sort.dir === 'desc' ? -1 : 1;
      const col = sort.col;
      let numeric = true;
      for (const r of view){
        const v = r[col];
        if (v == null || v === '') continue;
        if (isNaN(Number(v))){ numeric = false; break; }
      }
      view = view.slice().sort((a,b)=>{
        const av = a[col], bv = b[col];
        if (av == null && bv == null) return 0;
        if (av == null || av === '') return 1;
        if (bv == null || bv === '') return -1;
        if (numeric) return (Number(av) - Number(bv)) * dir;
        return String(av).localeCompare(String(bv)) * dir;
      });
    }
    card._preview.rows  = view;
    card._preview.total = view.length;
    card._preview.page  = 1;
    if (matchCountEl){
      const orig_n = orig.length;
      matchCountEl.textContent = (view.length === orig_n)
        ? ''
        : (view.length + ' / ' + orig_n);
    }
    try { card._preview._render(); } catch(e){ console.warn('[insights] render failed', e); }
  }

  // ─── 2b. Plain <table> in a bubble (db_chat.html-style) ────────────────
  // db_chat.html and similar simpler chat UIs render the result as a
  // markdown-parsed <table> directly inside .msg.assistant .bubble. There's
  // no pagination layer, so we enhance the <table> DOM directly: wrap it
  // with a search input, hide non-matching <tr>s, and sort <tbody> rows
  // in place on header click.
  function enhancePlainTable(table){
    if (!table || table.dataset.bioInsightsPlain === '1') return;
    if (!table.tHead || !table.tBodies || !table.tBodies[0]) return;
    if (table.rows.length < 2) return;  // header-only / empty
    table.dataset.bioInsightsPlain = '1';

    // Snapshot original row order so a third sort click can restore it.
    const tbody = table.tBodies[0];
    const originalOrder = Array.from(tbody.rows);

    // Build a toolbar above the table.
    const wrap = document.createElement('div');
    wrap.className = 'bio-plain-table-wrap';
    const toolbar = document.createElement('div');
    toolbar.className = 'bio-plain-table-toolbar';
    toolbar.innerHTML =
      '<div class="bio-table-search">' +
        '<input type="search" placeholder="🔎 Filter rows…" aria-label="Filter rows" />' +
        '<span class="bio-match-count" aria-live="polite"></span>' +
      '</div>';
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(toolbar);
    wrap.appendChild(table);

    const searchInput = toolbar.querySelector('input');
    const matchCount  = toolbar.querySelector('.bio-match-count');

    function applySearch(){
      const q = (searchInput.value || '').trim().toLowerCase();
      let shown = 0;
      const total = tbody.rows.length;
      for (const tr of tbody.rows){
        const visible = !q || (tr.textContent || '').toLowerCase().includes(q);
        tr.style.display = visible ? '' : 'none';
        if (visible) shown++;
      }
      matchCount.textContent = q ? (shown + ' / ' + total) : '';
    }
    let debounceT;
    searchInput.addEventListener('input', ()=>{
      clearTimeout(debounceT);
      debounceT = setTimeout(applySearch, 100);
    });

    // Sort: click <th> to cycle asc → desc → original.
    const ths = table.tHead.querySelectorAll('th');
    let sortState = { col: -1, dir: null };  // dir ∈ {'asc','desc',null}
    ths.forEach((th, idx) => {
      th.classList.add('bio-sortable-th');
      const ind = document.createElement('span');
      ind.className = 'bio-sort-ind';
      ind.textContent = ' ⇅';
      th.appendChild(ind);
      th.addEventListener('click', () => {
        let dir;
        if (sortState.col !== idx)        dir = 'asc';
        else if (sortState.dir === 'asc') dir = 'desc';
        else                              dir = null;
        sortState = { col: dir ? idx : -1, dir };
        // Reset indicators.
        ths.forEach((other, j) => {
          other.classList.toggle('bio-sort-active', j === idx && dir);
          const oi = other.querySelector('.bio-sort-ind');
          if (oi){
            if (j === idx && dir) oi.textContent = dir === 'asc' ? ' ▲' : ' ▼';
            else                  oi.textContent = ' ⇅';
          }
        });
        if (!dir){
          // Restore original order.
          originalOrder.forEach(r => tbody.appendChild(r));
        } else {
          const dirSign = dir === 'desc' ? -1 : 1;
          const rows = Array.from(tbody.rows);
          // Detect numeric column from visible cells.
          let numeric = true;
          for (const r of rows){
            const cell = r.cells[idx];
            const v = cell ? cell.textContent.trim() : '';
            if (v === '') continue;
            if (isNaN(Number(v))){ numeric = false; break; }
          }
          rows.sort((a, b) => {
            const av = a.cells[idx] ? a.cells[idx].textContent.trim() : '';
            const bv = b.cells[idx] ? b.cells[idx].textContent.trim() : '';
            if (av === '' && bv === '') return 0;
            if (av === '') return 1;
            if (bv === '') return -1;
            return (numeric
              ? (Number(av) - Number(bv))
              : av.localeCompare(bv)) * dirSign;
          });
          rows.forEach(r => tbody.appendChild(r));
        }
        applySearch();  // keep filter visibility consistent after sort
      });
    });
  }

  // ─── 3. Boot ────────────────────────────────────────────────────────────
  function sweep(){
    // Snapshot chip — match BOTH rich chat (.message.assistant .message-bubble)
    // AND simpler db_chat.html UI (.msg.assistant .bubble).
    document.querySelectorAll(
      '.message.assistant .message-bubble, .msg.assistant .bubble'
    ).forEach(b => {
      if (b.closest('.tool-card')) return;
      maybeAttachSnapshotChip(b);
    });
    // Paginated preview cards (rich chat).
    document.querySelectorAll('.tool-card.table-card').forEach(enhanceTableCard);
    // Plain <table> inside an assistant bubble (db_chat.html-style).
    document.querySelectorAll(
      '.msg.assistant .bubble table, .message.assistant .message-bubble table'
    ).forEach(t => {
      if (t.closest('.tool-card')) return;          // tool-card tables handled above
      if (t.closest('.bio-plain-table-wrap')) return;
      enhancePlainTable(t);
    });
  }

  function startObserver(){
    const root = document.querySelector('.chat') || document.body;
    // Coalesce sweeps with requestAnimationFrame so streaming text (which
    // mutates the DOM hundreds of times per second) doesn't trigger one
    // full-document scan per character — that froze the main thread on
    // big tables. One sweep per frame is plenty.
    let pending = false;
    function scheduleSweep(){
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; sweep(); });
    }
    const mo = new MutationObserver(scheduleSweep);
    // Drop `characterData: true` — streaming text deltas (e.g. the router
    // emitting "BIOCHIRP_STRUCTURED_RETRIEVAL" character by character)
    // dominated the mutation stream and made sweep run thousands of times
    // per second. childList alone is enough to catch new bubbles + cards.
    mo.observe(root, { childList:true, subtree:true });
    sweep();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startObserver);
  } else {
    startObserver();
  }
})();
