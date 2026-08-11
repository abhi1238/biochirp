// Event delegation runtime for BioChirp chat pages.
//
// Replaces inline `onclick="X(this)"` handlers, which require CSP
// `script-src 'unsafe-inline'` (or 'unsafe-hashes') and increase XSS
// blast-radius. After this script is loaded, elements declare their
// behaviour via attributes:
//
//   <button data-action="copy-csv">…</button>
//   <button data-action="toggle-card">…</button>
//   <div    data-action="ask-preset" data-preset="What genes …?">…</div>
//
// A single delegated click listener resolves data-action to a function
// on `window` (via the ACTION_MAP below) and invokes it with the clicked
// element as argument.
//
// Adding a new action: declare the function on window in the page's
// existing <script> block (it already does — every chat page has
// toggleCard, copyCSVFromCard, etc. at the top level), then add the
// (action-name → window-function-name) mapping below.

(function () {
  if (window.__biochirpDelegationInstalled) return;
  window.__biochirpDelegationInstalled = true;

  // action-name (kebab) → name of the global function to call.
  // The function receives the clicked element (event.currentTarget anchor).
  const ACTION_MAP = {
    "toggle-card":          "toggleCard",
    "copy-csv":             "copyCSVFromCard",
    "download-csv":         "downloadCSVFromCard",
    "download-filtered-csv":"downloadFilteredCSVFromCard",
    "load-full-table":      "loadFullTableFromCard",
    "send-message":         "sendMessage",
    "manual-reconnect":     "manualReconnect",
    "ask-preset":           "askPreset",         // reads data-preset
    "share-snapshot":       "createShareSnapshot",
    "close-share-modal":    "closeShareModal",
    "copy-share-url":       "copyShareUrl",
    // Per-turn actions added 2026-05-19 (issue #21). The action handlers
    // live in chat-main.js because they need access to the turn DOM that
    // chat-main builds; share.js only wires the conversation-level button.
    "share-turn":           "shareTurnAction",
    "copy-bibtex":          "copyBibtexAction",
    // Few-shot feedback (thumbs-up/down). Handler in chat-main.js; reads
    // data-verdict="up"|"down" from the clicked button.
    "feedback-vote":        "feedbackVoteAction",
  };

  document.addEventListener("click", function (ev) {
    // Walk up to the nearest element with [data-action], so clicks on
    // child icons/text still resolve to the button's action.
    const el = ev.target.closest("[data-action]");
    if (!el) return;

    const action = el.getAttribute("data-action");
    const fnName = ACTION_MAP[action];
    if (!fnName) return;

    const fn = window[fnName];
    if (typeof fn !== "function") {
      console.warn("biochirp delegation: missing function", fnName, "for action", action);
      return;
    }

    // ask-preset carries the preset query in a data-preset attribute so we
    // never have to interpolate user-supplied (or HTML-fragile) strings
    // into an attribute.
    if (action === "ask-preset") {
      fn(el.getAttribute("data-preset") || "");
    } else {
      fn(el);
    }
  });
})();
