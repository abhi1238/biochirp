/* BioChirp db_api.html bootstrap.
 *
 * Fetches /configs/db_apis.json and patches the generic console template
 * (title, accent colors, quick-asks, schema tables, examples, purpose text)
 * for the requested ?db=<slug>. Publishes window.__BC_API_CONFIG so
 * console-main.js (the generic Try-it/cURL/copy logic) knows which DB it's
 * talking to. This is the REST-console counterpart to chat-bootstrap.js.
 */
(function bcApiBootstrap(){
  "use strict";

  const slug = (new URLSearchParams(location.search).get('db')
                || window.__BC_API_DEFAULT_SLUG
                || '').toLowerCase().trim();

  const $ = (s) => document.querySelector(s);

  function escHtml(s){
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
  }
  const pretty = (o) => { try { return JSON.stringify(o, null, 2); } catch { return String(o); } };

  fetch('/configs/db_apis.json', { cache: 'no-store' })
    .then(r => r.ok ? r.json() : null)
    .then(all => {
      const cfg = all && all[slug];
      if (!cfg) {
        console.error('[db_api] no config for slug=' + slug);
        const main = $('#pageTitle');
        if (main) main.textContent = 'Unknown database: ' + (slug || '(none)');
        const sub = $('#pageSub');
        if (sub) sub.textContent = 'Check the ?db= query param — see /configs/db_apis.json for valid slugs.';
        return;
      }

      // ── Theming: 3 CSS custom properties drive every accent-colored
      // surface (buttons, gradients, quick-ask cards) via color-mix() in
      // db_api.html's stylesheet — no per-DB CSS block needed.
      const root = document.documentElement;
      root.style.setProperty('--accent', cfg.accent);
      root.style.setProperty('--accent2', cfg.accent2);
      root.style.setProperty('--btn-primary-text', cfg.btnPrimaryText || '#00162a');

      // ── Header ──────────────────────────────────────────────────────
      document.title = `BioChirp — ${cfg.name} Service Console`;
      const pageTitle = $('#pageTitle'); if (pageTitle) pageTitle.textContent = `${cfg.name} Service Console`;
      const pageSub = $('#pageSub'); if (pageSub) pageSub.textContent = `/${slug} • ${cfg.subtitle}`;

      const chatLink = $('#chatLink');
      if (chatLink) {
        chatLink.href = `/db_chat.html?db=${encodeURIComponent(slug)}`;
        chatLink.style.display = '';
      }

      // ── Quick-ask preset cards ──────────────────────────────────────
      const quickAsks = $('#quickAsks');
      if (quickAsks && Array.isArray(cfg.quickAsks)) {
        quickAsks.innerHTML = cfg.quickAsks.map(qa =>
          `<button class="qa-card" type="button" data-preset='${escHtml(JSON.stringify(qa.preset))}'>${escHtml(qa.label)}</button>`
        ).join('');
      }

      // ── Request-model table ─────────────────────────────────────────
      const pvNotes = $('#parsedValueNotes'); if (pvNotes) pvNotes.textContent = cfg.parsedValueNotes || '';

      const extraBlock = $('#extraParsedValueBlock');
      const extraBody = $('#extraParsedValueBody');
      const extraTitle = $('#extraParsedValueTitle');
      if (extraBlock && extraBody && Array.isArray(cfg.extraParsedValueKeys) && cfg.extraParsedValueKeys.length) {
        extraTitle.textContent = `parsed_value keys (${cfg.name})`;
        extraBody.innerHTML = cfg.extraParsedValueKeys.map(k =>
          `<tr><td class="mono">${escHtml(k.key)}</td><td>${escHtml(k.notes)}</td></tr>`
        ).join('');
        extraBlock.style.display = '';
      }

      const exampleInput = $('#example-input'); if (exampleInput) exampleInput.textContent = pretty(cfg.exampleRequest);

      // ── Response-model table + example ──────────────────────────────
      const respDb = $('#respDbExample'); if (respDb) respDb.textContent = `"${slug}"`;
      const respTool = $('#respToolExample'); if (respTool) respTool.textContent = `"${slug}"`;
      const respMsg = $('#respMessageNote'); if (respMsg) respMsg.textContent = cfg.responseMessageNote || 'Status note';

      const exampleOutput = $('#example-output'); if (exampleOutput) exampleOutput.textContent = pretty(cfg.exampleResponse);
      const copyOutBtn = $('#copyExampleOutput'); if (copyOutBtn) copyOutBtn.setAttribute('data-copy', JSON.stringify(cfg.exampleResponse));

      // ── POST /<slug> section ─────────────────────────────────────────
      const postH = $('#postH'); if (postH) postH.textContent = `/${slug}`;
      const postPurpose = $('#postPurpose'); if (postPurpose) postPurpose.innerHTML = `<strong>Purpose:</strong> ${cfg.purposeHtml || ''}`;
      const inBody = $('#in-body'); if (inBody) inBody.setAttribute('aria-label', `POST /${slug} request body`);

      // Publish the resolved config for console-main.js.
      window.__BC_API_CONFIG = {
        slug,
        cfg,
        apiBase: `/services/${slug}`,
        postPath: `/${slug}`,
        postTimeoutMs: cfg.postTimeoutMs || 45000,
      };
      document.dispatchEvent(new CustomEvent('bc-api-config-ready'));
    })
    .catch(err => console.error('[db_api] failed to load db_apis.json', err));
})();
