// BioChirp shared "Share snapshot" runtime — used by every *_chat_api.html.
//
// Contract:
//   - The page must include this script and assets/share.css.
//   - The page must contain three elements with these IDs:
//       <button id="shareBtn" data-action="share-snapshot">★ Share</button>
//       <div    id="readonlyBanner" class="readonly-banner" hidden>…</div>
//       <div    id="shareModal"     class="share-modal"     hidden>
//          <input id="shareUrl">  <button id="shareCopyBtn">…</button>
//          <div   id="shareStatus">  <div id="shareUrlRow">  <div id="shareHint">
//       </div>
//   - The page's bootstrap (window.load) must short-circuit when readonly:
//       if (window.isReadOnly && window.isReadOnly()) {
//         window.applyReadOnlyMode();
//         initTheme();      // theme is safe in readonly
//         return;           // do NOT call resetChatUI() or connect()
//       }
//   - data-action click delegation in assets/event-delegation.js maps:
//       share-snapshot    → window.createShareSnapshot
//       close-share-modal → window.closeShareModal
//       copy-share-url    → window.copyShareUrl

(function () {
  if (window.__biochirpShareInstalled) return;
  window.__biochirpShareInstalled = true;

  // The /share endpoint serves snapshots inside a sandboxed iframe with the
  // captured DOM as srcdoc. Before serialisation we inject
  // window.__BIOCHIRP_READONLY__ = true so the page's own bootstrap can skip
  // the websocket connection and disable the composer.
  window.isReadOnly = function () { return !!window.__BIOCHIRP_READONLY__; };

  window.applyReadOnlyMode = function () {
    document.body.classList.add('readonly-mode');
    const banner = document.getElementById('readonlyBanner');
    if (banner) banner.hidden = false;
    const cs = document.getElementById('connectionStatus');
    if (cs) cs.textContent = 'Shared snapshot';
    const dot = document.getElementById('statusDot');
    if (dot) dot.style.background = 'var(--mut)';
    const qa = document.getElementById('quickAsks');
    if (qa) qa.style.display = 'none';
  };

  // Captures the *current* DOM, injects a read-only marker + a <base href> so
  // relative asset URLs still resolve when the snapshot is rendered inside an
  // iframe srcdoc, and POSTs to /share. Returns the public URL.
  async function captureAndUploadSnapshot() {
    const cloneRoot = document.documentElement.cloneNode(true);
    const head = cloneRoot.querySelector('head');
    if (!head) throw new Error('No <head> element to inject into.');

    // <base href> so relative URLs (assets/...) keep working in srcdoc.
    let base = head.querySelector('base');
    if (!base) {
      base = document.createElement('base');
      head.insertBefore(base, head.firstChild);
    }
    base.setAttribute('href', window.location.origin + '/');

    // Read-only marker — must run BEFORE every other inline script so the
    // bootstrap sees the flag. Insert as the *first* head child.
    const marker = document.createElement('script');
    marker.textContent = 'window.__BIOCHIRP_READONLY__=true;';
    head.insertBefore(marker, head.firstChild);

    // Drop the share modal and any transient overlays from the snapshot.
    const modal = cloneRoot.querySelector('#shareModal');
    if (modal) modal.remove();

    const html = '<!DOCTYPE html>\n' + cloneRoot.outerHTML;
    const title = (document.title || 'BioChirp shared snapshot').slice(0, 200);

    const resp = await fetch(window.location.origin + '/share', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ html, title, unsafe: true }),
    });
    if (!resp.ok) {
      let detail = '';
      try { detail = (await resp.json()).detail || ''; } catch {}
      throw new Error('Share failed (' + resp.status + '): ' + (detail || resp.statusText));
    }
    const data = await resp.json();
    return /^https?:/i.test(data.url || '')
      ? data.url
      : (window.location.origin + (data.url || ('/s/' + data.id)));
  }

  function openShareModal() {
    const m = document.getElementById('shareModal');
    if (m) m.hidden = false;
  }

  window.closeShareModal = function () {
    const m = document.getElementById('shareModal');
    if (m) m.hidden = true;
    const status = document.getElementById('shareStatus');
    if (status) { status.textContent = 'Capturing snapshot…'; status.style.color = ''; }
    const urlRow = document.getElementById('shareUrlRow');
    if (urlRow) urlRow.hidden = true;
    const hint = document.getElementById('shareHint');
    if (hint) hint.hidden = true;
  };

  window.copyShareUrl = function () {
    const inp = document.getElementById('shareUrl');
    if (!inp) return;
    inp.select();
    try {
      navigator.clipboard.writeText(inp.value).then(() => {
        const btn = document.getElementById('shareCopyBtn');
        if (btn) {
          const t = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(() => { btn.textContent = t; }, 1200);
        }
      });
    } catch { /* selection is the fallback */ }
  };

  window.createShareSnapshot = async function () {
    if (window.isReadOnly()) return; // no nested shares
    openShareModal();
    const status = document.getElementById('shareStatus');
    const urlRow = document.getElementById('shareUrlRow');
    const hint = document.getElementById('shareHint');
    const urlInput = document.getElementById('shareUrl');
    try {
      status.textContent = 'Capturing snapshot…';
      status.style.color = '';
      const url = await captureAndUploadSnapshot();
      status.textContent = 'Snapshot ready. Copy the link below to share:';
      urlInput.value = url;
      urlRow.hidden = false;
      hint.hidden = false;
      urlInput.focus(); urlInput.select();
    } catch (err) {
      status.textContent = String(err.message || err);
      status.style.color = 'var(--err)';
      urlRow.hidden = true;
      hint.hidden = true;
    }
  };
})();
