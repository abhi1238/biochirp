// BioChirp WS auth shim.
//
// Wraps `window.WebSocket` so that any same-origin WS open is preceded by
// a fetch to /auth/token; the returned token is appended as ?token=<...>
// before the real socket is constructed.
//
// MUST load BEFORE chat-main.js (or any other script that calls
// `new WebSocket(...)`). It's a defensive shim only:
//   - If /auth/token fails (404, 5xx, network) we open the WS WITHOUT a
//     token. The server's gate is itself off-by-default, so existing
//     deployments stay working until you flip BIOCHIRP_WS_AUTH_REQUIRED=1.
//   - Cross-origin WS calls are passed through unchanged.
//
// The wrapper exposes the WebSocket interface chat code uses (readyState,
// onopen/onmessage/onclose/onerror setters, send, close). It does NOT
// implement EventTarget.addEventListener — chat-main only uses the legacy
// on* properties, which is what the spec guarantees works on every WS.
(function () {
  if (typeof window === "undefined" || !window.WebSocket || !window.fetch) {
    return;
  }
  if (window.__BC_WS_SHIM_INSTALLED) return;
  window.__BC_WS_SHIM_INSTALLED = true;

  var OrigWS = window.WebSocket;
  var tokenPromise = null;

  function getToken() {
    if (tokenPromise) return tokenPromise;
    tokenPromise = fetch("/auth/token", {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { return j && j.token ? String(j.token) : null; })
      .catch(function () { return null; });
    return tokenPromise;
  }
  // Kick off the token fetch as soon as the shim loads so it's in flight
  // by the time chat-main calls `new WebSocket()`.
  getToken();

  function appendToken(url, token) {
    if (!token) return url;
    var sep = url.indexOf("?") >= 0 ? "&" : "?";
    return url + sep + "token=" + encodeURIComponent(token);
  }

  function isSameOrigin(url) {
    try {
      var u = new URL(url, location.href);
      return u.host === location.host;
    } catch (_) {
      return false;
    }
  }

  function PatchedWebSocket(url, protocols) {
    if (!(this instanceof PatchedWebSocket)) {
      // Match `new` semantics defensively (chat-main always uses `new`).
      return new PatchedWebSocket(url, protocols);
    }
    if (!isSameOrigin(url)) {
      return new OrigWS(url, protocols);
    }

    var state = {
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      real: null,
      sendQueue: [],          // sends issued before the real WS opens
      closeQueued: null,      // [code, reason] if .close() called pre-open
    };
    var wrapper = this;

    function attach(real) {
      state.real = real;
      real.onopen = function (e) {
        if (state.onopen) state.onopen.call(wrapper, e);
      };
      real.onmessage = function (e) {
        if (state.onmessage) state.onmessage.call(wrapper, e);
      };
      real.onclose = function (e) {
        if (state.onclose) state.onclose.call(wrapper, e);
      };
      real.onerror = function (e) {
        if (state.onerror) state.onerror.call(wrapper, e);
      };
      for (var i = 0; i < state.sendQueue.length; i++) {
        try { real.send(state.sendQueue[i]); } catch (_) { /* drop */ }
      }
      state.sendQueue.length = 0;
      if (state.closeQueued) {
        try { real.close.apply(real, state.closeQueued); } catch (_) {}
        state.closeQueued = null;
      }
    }

    getToken().then(function (token) {
      attach(new OrigWS(appendToken(url, token), protocols));
    }).catch(function () {
      // Token fetch rejected — open without a token. Server gate (when
      // enabled) will close 1008 and the page reconnect loop will retry.
      attach(new OrigWS(url, protocols));
    });

    Object.defineProperty(wrapper, "readyState", {
      get: function () {
        return state.real ? state.real.readyState : OrigWS.CONNECTING;
      },
    });
    Object.defineProperty(wrapper, "url", {
      get: function () { return state.real ? state.real.url : url; },
    });
    Object.defineProperty(wrapper, "bufferedAmount", {
      get: function () { return state.real ? state.real.bufferedAmount : 0; },
    });
    Object.defineProperty(wrapper, "protocol", {
      get: function () { return state.real ? state.real.protocol : ""; },
    });
    Object.defineProperty(wrapper, "extensions", {
      get: function () { return state.real ? state.real.extensions : ""; },
    });
    Object.defineProperty(wrapper, "binaryType", {
      get: function () { return state.real ? state.real.binaryType : "blob"; },
      set: function (v) { if (state.real) state.real.binaryType = v; },
    });
    Object.defineProperty(wrapper, "onopen", {
      get: function () { return state.onopen; },
      set: function (fn) { state.onopen = fn; },
    });
    Object.defineProperty(wrapper, "onmessage", {
      get: function () { return state.onmessage; },
      set: function (fn) { state.onmessage = fn; },
    });
    Object.defineProperty(wrapper, "onclose", {
      get: function () { return state.onclose; },
      set: function (fn) { state.onclose = fn; },
    });
    Object.defineProperty(wrapper, "onerror", {
      get: function () { return state.onerror; },
      set: function (fn) { state.onerror = fn; },
    });

    wrapper.send = function (data) {
      if (state.real && state.real.readyState === OrigWS.OPEN) {
        state.real.send(data);
      } else if (state.real) {
        // Real WS exists but not OPEN — let the browser throw exactly
        // as it would without the shim.
        state.real.send(data);
      } else {
        state.sendQueue.push(data);
      }
    };
    wrapper.close = function (code, reason) {
      if (state.real) state.real.close(code, reason);
      else state.closeQueued = [code, reason];
    };
  }

  PatchedWebSocket.CONNECTING = OrigWS.CONNECTING;
  PatchedWebSocket.OPEN = OrigWS.OPEN;
  PatchedWebSocket.CLOSING = OrigWS.CLOSING;
  PatchedWebSocket.CLOSED = OrigWS.CLOSED;
  PatchedWebSocket.prototype = OrigWS.prototype;

  window.WebSocket = PatchedWebSocket;
})();
