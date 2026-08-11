// Registers a page visit with the orchestrator metrics endpoint.
// Include with: <script src="/assets/tracker.js" data-page="page-name"></script>
(function () {
  const script = document.currentScript || document.querySelector('script[data-page]');
  const page = (script && script.getAttribute('data-page')) || document.title || 'unknown';
  fetch('/track-visit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ page }),
    keepalive: true,
  }).catch(() => {});
})();
