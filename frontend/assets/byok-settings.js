/**
 * BioChirp BYOK (Bring Your Own Key) settings panel.
 *
 * Stores the user's OpenRouter API key + per-role model choices in localStorage.
 * Sent with every WebSocket message; backend creates a per-connection
 * AsyncOpenAI client on openrouter.ai — completely isolated from other users.
 *
 * Per-role model priority (backend):
 *   per-role BYOK  >  global BYOK fallback  >  server env-var default
 *
 * Public API:
 *   window.biochirpBYOK.buildPayloadFields()  → {} | {openrouter_api_key, ...}
 *   window.biochirpBYOK.isActive()            → true when a key is stored
 */
(function () {
  'use strict';

  /* ── localStorage keys ────────────────────────────────────── */
  var LS_KEY               = 'biochirp_or_key';
  var LS_ROUTER_MODEL      = 'biochirp_or_router_model';
  var LS_SELECTOR_MODEL    = 'biochirp_or_selector_model';
  var LS_DECOMPOSER_MODEL  = 'biochirp_or_decomposer_model';
  var LS_INTENT_MODEL      = 'biochirp_or_intent_model';
  var LS_SYNTH_MODEL       = 'biochirp_or_synth_model';

  /* ── Per-role definitions ─────────────────────────────────── */
  var ROLES = [
    {
      id:      'router',
      lsKey:   LS_ROUTER_MODEL,
      payload: 'openrouter_router_model',
      label:   '🔀 Router',
      desc:    'Classifies each query as a biomedical DB lookup, a web search, or off-topic, ' +
               'and also answers non-biomedical questions from web snippets. Fast, small-output — ' +
               'a lightweight model works well here.',
      envNote: 'Server default: <code>qwen3.5-nothink:latest</code> (via <code>ROUTER_MODEL_NAME</code>)',
    },
    {
      id:      'selector',
      lsKey:   LS_SELECTOR_MODEL,
      payload: 'openrouter_selector_model',
      label:   '🗂 DB Selector',
      desc:    'Re-ranks candidate databases by semantic similarity to the query and picks ' +
               'the best 1–5 to query. Runs once per question — a fast model is sufficient.',
      envNote: 'Server default: <code>qwen3.5-nothink:latest</code>',
    },
    {
      id:      'decomposer',
      lsKey:   LS_DECOMPOSER_MODEL,
      payload: 'openrouter_decomposer_model',
      label:   '✂ Decomposer',
      desc:    'Breaks complex multi-part questions into an ordered list of sub-queries ' +
               '(up to 3) with explicit data dependencies. Outputs a small JSON plan — ' +
               'a fast reasoning model works well.',
      envNote: 'Server default: <code>qwen3.5-nothink:latest</code> (via <code>DECOMPOSER_MODEL_NAME</code>)',
    },
    {
      id:      'intent',
      lsKey:   LS_INTENT_MODEL,
      payload: 'openrouter_intent_model',
      label:   '🧩 Intent Classifier',
      desc:    'Detects query intent flags (enumeration, literature, bioactivity, PTM, ' +
               'prevalence, inheritance) so the synthesizer knows how to format and depth the answer. ' +
               'Single short JSON output — shares the router model by default.',
      envNote: 'Server default: <code>qwen3.5-nothink:latest</code> (via <code>BIOCHIRP_SYNTH_ENUM_CLASSIFIER</code> → <code>ROUTER_MODEL_NAME</code>)',
    },
    {
      id:      'synth',
      lsKey:   LS_SYNTH_MODEL,
      payload: 'openrouter_synth_model',
      label:   '✍ Synthesizer',
      desc:    'Writes the final answer from retrieved database rows. This is the response ' +
               'the user actually reads — a stronger model gives richer, better-cited answers.',
      envNote: 'Server default: <code>synthesizer</code> alias → <code>gpt-oss:20b</code> (via <code>SYNTHESIZER_MODEL_NAME</code>)',
    },
  ];

  /* ── Curated model list (OpenRouter IDs) ──────────────────── */
  var MODELS = [
    { id: 'openai/gpt-4o',                         label: 'GPT-4o' },
    { id: 'openai/gpt-4o-mini',                    label: 'GPT-4o Mini' },
    { id: 'anthropic/claude-sonnet-4-5',           label: 'Claude Sonnet 4.5' },
    { id: 'anthropic/claude-haiku-4-5',            label: 'Claude Haiku 4.5' },
    { id: 'google/gemini-2.5-flash-preview-05-20', label: 'Gemini 2.5 Flash' },
    { id: 'google/gemini-2.0-flash-001',           label: 'Gemini 2.0 Flash' },
    { id: 'meta-llama/llama-3.3-70b-instruct',     label: 'Llama 3.3 70B' },
    { id: 'mistralai/mistral-large-2411',          label: 'Mistral Large' },
    { id: 'qwen/qwen-2.5-72b-instruct',            label: 'Qwen 2.5 72B' },
  ];

  /* ── localStorage helpers ─────────────────────────────────── */
  function lsGet(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } }
  function lsSet(k, v) { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) {} }

  /* ── CSS ──────────────────────────────────────────────────── */
  var CSS = [
    '#byok-overlay{display:none;position:fixed;inset:0;z-index:9000;',
    'background:rgba(0,0,0,.55);backdrop-filter:blur(3px);',
    'align-items:flex-start;justify-content:center;overflow-y:auto;padding:40px 16px;}',
    '#byok-overlay.open{display:flex;}',
    '#byok-box{background:var(--bg1,#fff);border:1px solid var(--brd,rgba(0,0,0,.1));',
    'border-radius:14px;padding:22px 24px;width:min(520px,94vw);',
    'box-shadow:0 18px 55px rgba(0,0,0,.22);display:flex;flex-direction:column;gap:18px;',
    'margin:auto;}',
    '#byok-box h3{margin:0;font-size:14px;font-weight:700;color:var(--txt,#0b1624);}',
    '.byok-hint{font-size:11px;color:var(--mut,#486173);line-height:1.5;margin-top:-8px;}',
    '.byok-divider{border:none;border-top:1px solid var(--brd,rgba(0,0,0,.1));margin:0;}',
    '.byok-section-hdr{font-size:11px;font-weight:700;text-transform:uppercase;',
    'letter-spacing:.06em;color:var(--mut,#486173);margin-bottom:-6px;}',
    '.byok-row{display:flex;flex-direction:column;gap:5px;}',
    '.byok-row label{font-size:12px;font-weight:700;color:var(--txt,#0b1624);}',
    '.byok-row .byok-desc{font-size:11px;color:var(--mut,#486173);line-height:1.45;}',
    '.byok-row .byok-desc code{font-family:monospace;font-size:10px;',
    'background:var(--bg3,#f4f7fb);padding:1px 4px;border-radius:4px;}',
    '.byok-row .byok-envnote{font-size:10px;color:var(--mut,#486173);font-style:italic;}',
    '.byok-row .byok-envnote code{font-family:monospace;font-size:10px;}',
    '.byok-key-wrap{display:flex;gap:6px;}',
    '.byok-key-wrap input,.byok-sel,.byok-custom{',
    'padding:8px 10px;border-radius:8px;border:1.5px solid var(--brd,rgba(0,0,0,.1));',
    'background:var(--bg3,#f4f7fb);color:var(--txt,#0b1624);font-size:13px;',
    'outline:none;width:100%;}',
    '.byok-key-wrap input:focus,.byok-sel:focus,.byok-custom:focus{',
    'border-color:var(--accent,#1fa4ff);}',
    '.byok-toggle{flex-shrink:0;padding:8px 10px;border-radius:8px;',
    'border:1.5px solid var(--brd,rgba(0,0,0,.1));background:var(--bg3,#f4f7fb);',
    'cursor:pointer;color:var(--mut,#486173);font-size:13px;white-space:nowrap;}',
    '.byok-toggle:hover{border-color:var(--accent,#1fa4ff);}',
    '.byok-custom{font-family:monospace;font-size:12px;}',
    '.byok-status{font-size:11px;padding:4px 9px;border-radius:999px;',
    'background:rgba(15,178,122,.12);color:var(--ok,#0fb27a);',
    'border:1px solid rgba(15,178,122,.25);display:inline-block;}',
    '.byok-status.off{background:var(--hover,rgba(0,0,0,.04));',
    'color:var(--mut,#486173);border-color:var(--brd,rgba(0,0,0,.1));}',
    '.byok-actions{display:flex;gap:8px;justify-content:flex-end;padding-top:2px;}',
    '.byok-save{padding:8px 18px;border-radius:8px;border:none;',
    'background:var(--accent,#1fa4ff);color:#fff;font-size:13px;font-weight:600;cursor:pointer;}',
    '.byok-save:hover{opacity:.85;}',
    '.byok-clear{padding:8px 14px;border-radius:8px;',
    'border:1.5px solid var(--brd,rgba(0,0,0,.12));background:transparent;',
    'color:var(--err,#e25050);font-size:13px;cursor:pointer;}',
    '.byok-clear:hover{background:rgba(226,80,80,.08);}',
    '.byok-cancel{padding:8px 14px;border-radius:8px;',
    'border:1.5px solid var(--brd,rgba(0,0,0,.12));background:transparent;',
    'color:var(--mut,#486173);font-size:13px;cursor:pointer;}',
    '.byok-cancel:hover{background:var(--hover,rgba(0,0,0,.04));}',
    '#byok-btn.byok-on{border-color:var(--accent,#1fa4ff)!important;',
    'color:var(--accent,#1fa4ff)!important;}',
  ].join('');

  /* ── Helpers ──────────────────────────────────────────────── */
  function getKey() { return lsGet(LS_KEY); }

  function getRoleModel(role) { return lsGet(role.lsKey); }

  function makeModelOptions(selectedVal) {
    var opts = '<option value="">(server default)</option>';
    MODELS.forEach(function (m) {
      var sel = selectedVal === m.id ? ' selected' : '';
      opts += '<option value="' + m.id + '"' + sel + '>' + m.label + '</option>';
    });
    var customSel = selectedVal && !MODELS.some(function (m) { return m.id === selectedVal; })
      ? ' selected' : '';
    opts += '<option value="__custom__"' + customSel + '>Other — enter model ID below</option>';
    return opts;
  }

  function getRoleInputVal(role) {
    var sel = document.getElementById('byok-sel-' + role.id);
    if (!sel) return '';
    if (sel.value === '__custom__') {
      var c = document.getElementById('byok-cus-' + role.id);
      return c ? c.value.trim() : '';
    }
    return sel.value;
  }

  function updateStatus() {
    var el = document.getElementById('byok-status');
    if (!el) return;
    var k = getKey();
    if (k) {
      var short = k.slice(0, 8) + '…' + k.slice(-4);
      var active = ROLES.filter(function (r) { return getRoleModel(r); })
        .map(function (r) { return r.label.replace(/^[^\s]+\s/, ''); });
      el.className = 'byok-status';
      el.textContent = 'Key active: ' + short +
        (active.length ? ' · custom: ' + active.join(', ') : ' · all roles: server default');
    } else {
      el.className = 'byok-status off';
      el.textContent = 'No key set — server defaults for all roles';
    }
  }

  function updateBtn() {
    var btn = document.getElementById('byok-btn');
    if (!btn) return;
    var on = !!getKey();
    if (on) btn.classList.add('byok-on'); else btn.classList.remove('byok-on');
    btn.title = on ? 'OpenRouter key active — click to change' : 'Use your own OpenRouter key';
  }

  /* ── Modal lifecycle ──────────────────────────────────────── */
  function openPanel() {
    var inp = document.getElementById('byok-inp');
    if (inp) inp.value = getKey();

    ROLES.forEach(function (role) {
      var saved = getRoleModel(role);
      var sel = document.getElementById('byok-sel-' + role.id);
      var cus = document.getElementById('byok-cus-' + role.id);
      if (!sel || !cus) return;
      var isKnown = saved && MODELS.some(function (m) { return m.id === saved; });
      if (saved && !isKnown) {
        sel.value = '__custom__';
        cus.value = saved;
        cus.style.display = '';
      } else {
        sel.value = saved || '';
        cus.style.display = 'none';
      }
    });

    updateStatus();
    var ov = document.getElementById('byok-overlay');
    if (ov) { ov.classList.add('open'); setTimeout(function () { if (inp) inp.focus(); }, 50); }
  }

  function closePanel() {
    var ov = document.getElementById('byok-overlay');
    if (ov) ov.classList.remove('open');
  }

  /* ── DOM injection ────────────────────────────────────────── */
  function injectCSS() {
    if (document.getElementById('byok-css')) return;
    var s = document.createElement('style');
    s.id = 'byok-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function buildRoleRow(role) {
    var saved = getRoleModel(role);
    var isCustom = saved && !MODELS.some(function (m) { return m.id === saved; });
    return [
      '<div class="byok-row">',
      '  <label>' + role.label + '</label>',
      '  <div class="byok-desc">' + role.desc + '</div>',
      '  <div class="byok-envnote">' + role.envNote + '</div>',
      '  <select id="byok-sel-' + role.id + '" class="byok-sel" data-role="' + role.id + '">',
      makeModelOptions(saved),
      '  </select>',
      '  <input id="byok-cus-' + role.id + '" class="byok-custom" type="text"',
      '    style="display:' + (isCustom ? '' : 'none') + '"',
      '    placeholder="e.g. openai/o3-mini" spellcheck="false" value="' + (isCustom ? saved : '') + '" />',
      '</div>',
    ].join('');
  }

  function injectModal() {
    if (document.getElementById('byok-overlay')) return;

    var roleRows = ROLES.map(buildRoleRow).join('');

    var html = [
      '<div id="byok-overlay" role="dialog" aria-modal="true" aria-label="OpenRouter key settings">',
      '  <div id="byok-box">',
      '    <h3>🔑 Your OpenRouter Key</h3>',
      '    <p class="byok-hint">Your key is stored in this browser only. Sent over the encrypted',
      '    connection with each query — never logged or stored on the server.<br>',
      '    Leave any role blank to use the server\'s configured model for that role.</p>',
      '    <div class="byok-row">',
      '      <label>OpenRouter API Key</label>',
      '      <div class="byok-key-wrap">',
      '        <input id="byok-inp" type="password" autocomplete="off" spellcheck="false"',
      '               placeholder="sk-or-v1-…" />',
      '        <button class="byok-toggle" id="byok-show" title="Show / hide key">👁</button>',
      '      </div>',
      '    </div>',
      '    <hr class="byok-divider">',
      '    <div class="byok-section-hdr">Per-role model selection</div>',
      roleRows,
      '    <div id="byok-status" class="byok-status off">No key set — server defaults for all roles</div>',
      '    <div class="byok-actions">',
      '      <button class="byok-cancel" id="byok-cancel">Cancel</button>',
      '      <button class="byok-clear"  id="byok-clear">Clear all</button>',
      '      <button class="byok-save"   id="byok-save">Save</button>',
      '    </div>',
      '  </div>',
      '</div>',
    ].join('');

    document.body.insertAdjacentHTML('beforeend', html);

    /* show/hide key */
    document.getElementById('byok-show').addEventListener('click', function () {
      var i = document.getElementById('byok-inp');
      if (i) i.type = i.type === 'password' ? 'text' : 'password';
    });

    /* per-role selector → toggle custom input */
    ROLES.forEach(function (role) {
      var sel = document.getElementById('byok-sel-' + role.id);
      if (!sel) return;
      sel.addEventListener('change', function () {
        var c = document.getElementById('byok-cus-' + role.id);
        if (c) c.style.display = this.value === '__custom__' ? '' : 'none';
      });
    });

    /* close on overlay click */
    document.getElementById('byok-overlay').addEventListener('click', function (e) {
      if (e.target === this) closePanel();
    });

    document.getElementById('byok-cancel').addEventListener('click', closePanel);

    document.getElementById('byok-save').addEventListener('click', function () {
      var k = (document.getElementById('byok-inp').value || '').trim();
      lsSet(LS_KEY, k);
      ROLES.forEach(function (role) {
        lsSet(role.lsKey, getRoleInputVal(role));
      });
      closePanel();
      updateBtn();
    });

    document.getElementById('byok-clear').addEventListener('click', function () {
      lsSet(LS_KEY, '');
      ROLES.forEach(function (role) { lsSet(role.lsKey, ''); });
      var i = document.getElementById('byok-inp');
      if (i) i.value = '';
      ROLES.forEach(function (role) {
        var sel = document.getElementById('byok-sel-' + role.id);
        var cus = document.getElementById('byok-cus-' + role.id);
        if (sel) sel.value = '';
        if (cus) { cus.value = ''; cus.style.display = 'none'; }
      });
      updateStatus();
      updateBtn();
      closePanel();
    });

    var btn = document.getElementById('byok-btn');
    if (btn) btn.addEventListener('click', openPanel);
  }

  /* ── Public API ───────────────────────────────────────────── */
  window.biochirpBYOK = {
    isActive: function () { return !!getKey(); },

    buildPayloadFields: function () {
      var k = getKey();
      if (!k) return {};
      var out = { openrouter_api_key: k };
      ROLES.forEach(function (role) {
        var m = getRoleModel(role);
        if (m) out[role.payload] = m;
      });
      return out;
    },

    init: function () {
      injectCSS();
      injectModal();
      updateBtn();
    },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { window.biochirpBYOK.init(); });
  } else {
    window.biochirpBYOK.init();
  }
})();
