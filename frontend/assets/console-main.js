/* BioChirp db_api.html — generic REST-console interaction logic.
 *
 * Identical Try-it / cURL / copy / theme mechanics for every DB console
 * (previously duplicated verbatim across ttd_api.html, ctd_api.html,
 * hcdt_api.html, string_api.html, msigdb_chat.html). Reads
 * window.__BC_API_CONFIG (published by console-bootstrap.js once
 * /configs/db_apis.json resolves) for the one thing that actually differs
 * per DB: API_BASE, the POST path, the example payload, and the timeout.
 */
(function bcApiMain(){
  "use strict";

  let API_BASE = '';
  let POST_PATH = '';
  let SINGLE_EX = {};
  let POST_TIMEOUT_MS = 45000;

  // ---------- DOM helpers ----------
  const $ = (s)=>document.querySelector(s);
  const statusDot = $('#statusDot');
  const statusText= $('#statusText');
  const statusMeta= $('#statusMeta');
  const connText  = $('#connectionStatus');
  const protoWarn = $('#protoWarn');
  const corsWarn  = $('#corsWarn');
  const fileWarn  = $('#fileWarn');
  const netWarn   = $('#netWarn');
  const pageProto = $('#pageProto');
  const apiProto  = $('#apiProto');
  const endpointLabel = $('#endpointLabel');
  const quickAsks = $('#quickAsks');

  const fullUrl = (path)=> API_BASE + (path.startsWith('/') ? path : '/'+path);
  const pretty = (o)=>{ try{ return JSON.stringify(o,null,2);}catch{ return String(o);} };

  function setConnStatus(txt, state){
    if (connText) connText.textContent = txt;
    statusDot?.classList.remove('online','offline','degraded');
    if (state === true)      statusDot?.classList.add('online');
    else if (state === false)statusDot?.classList.add('offline');
    else                     statusDot?.classList.add('degraded');
  }
  const isMixedContent = ()=> {
    try { return (location.protocol === 'https:' && new URL(API_BASE, location.origin).protocol === 'http:'); }
    catch { return false; }
  };
  function hideWarnings(){ [protoWarn,corsWarn,fileWarn,netWarn].forEach(x=>{ if(x) x.style.display='none'; }); }

  function parseJsonText(text){ if(!text || !text.trim()) return {}; return JSON.parse(text); }

  // Build POST path with optional connection_id
  function buildPostPath(basePath){
    const cid = ($('#connId')?.value || '').trim();
    return cid ? `${basePath}?connection_id=${encodeURIComponent(cid)}` : basePath;
  }

  // ---- fetch with timeout/CORS handling ----
  async function doFetch(urlOrPath, init={}, timeoutMs=45000){
    const url = urlOrPath.startsWith('http') ? urlOrPath : fullUrl(urlOrPath);
    const ctrl = new AbortController();
    const t = setTimeout(()=>ctrl.abort(), timeoutMs);
    try{
      const res = await fetch(url, { cache:'no-store', mode:'cors', signal:ctrl.signal, ...init });
      clearTimeout(t);
      const ct = res.headers.get('content-type') || '';
      const body = ct.includes('application/json') ? await res.json() : await res.text();
      if (!res.ok) return { ok:false, status:res.status, body };
      return { ok:true, status:res.status, body };
    }catch(err){
      clearTimeout(t);
      const msg = String(err);
      if (msg.includes('TypeError: Failed to fetch')){
        if (location.protocol === 'file:') { fileWarn && (fileWarn.style.display='block'); }
        else { corsWarn && (corsWarn.style.display='block'); }
      }else if (msg.includes('AbortError')){ netWarn && (netWarn.style.display='block'); }
      if (isMixedContent() && protoWarn) protoWarn.style.display='block';
      return { ok:false, body:{ error:msg } };
    }
  }

  // ---- heartbeat ----
  let pingInFlight = false;
  const fmtTime = (d=new Date())=> d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  async function ping(){
    if (pingInFlight) return; pingInFlight = true;
    try{
      const r = await doFetch('/health');
      if (r.ok){ statusText.textContent='Online'; statusMeta.textContent=`HTTP ${r.status} • ${fmtTime()}`; setConnStatus('Online', true); }
      else      { statusText.textContent='Degraded'; statusMeta.textContent=`${(r.body && (r.body.error||r.status)) || 'Error'} • ${fmtTime()}`; setConnStatus('Degraded', null); }
    }catch{
      statusText.textContent='Offline'; statusMeta.textContent=`Network/CORS error • ${fmtTime()}`; setConnStatus('Offline', false);
    }finally{ pingInFlight = false; }
  }

  // ---- actions ----
  async function runGet(path, outSel){
    const out = document.querySelector(outSel); if (out) out.textContent='…';
    try{ const r = await doFetch(path); if(out) out.textContent = pretty(r.body); }
    catch(e){ if(out) out.textContent = pretty({ error:String(e) }); }
  }
  async function runPost(path, inSel, outSel){
    const out = document.querySelector(outSel); if (out) out.textContent='…';
    let payload;
    try{ payload = parseJsonText(document.querySelector(inSel).value || '{}'); }
    catch{ if(out) out.textContent = pretty({ error:'Invalid JSON in request body' }); return; }
    try{
      const postPath = buildPostPath(path);
      const r = await doFetch(postPath, { method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(payload) }, POST_TIMEOUT_MS);
      if(out) out.textContent = pretty(r.body);
    }catch(e){
      if(out) out.textContent = pretty({ error:String(e?.message||e) });
    }
  }

  function curlFor(kind, path){
    const base = fullUrl(path);
    const cid = ($('#connId')?.value || '').trim();
    const url = cid ? `${base}?connection_id=${encodeURIComponent(cid)}` : base;
    if (kind === 'GET')  return `curl -s ${url}`;
    if (kind === 'POST'){
      const payload = ($('#in-body')?.value) ? $('#in-body').value : '{}';
      return `printf %s '${payload.replace(/'/g, "'\\''")}' | curl -s -X POST ${url} -H 'content-type: application/json' --data-binary @-`;
    }
    return '';
  }
  const copyText = (txt)=>{ try{ navigator.clipboard.writeText(txt); }catch{} };

  function wireCopyAndCurlButtons(){
    document.querySelectorAll('[data-copy]').forEach(btn=>{
      btn.addEventListener('click', ()=>{
        copyText(btn.getAttribute('data-copy') || '');
        const old = btn.textContent; btn.textContent='Copied'; setTimeout(()=>btn.textContent=old, 900);
      });
    });
    document.querySelectorAll('[data-copy-target]').forEach(btn=>{
      btn.addEventListener('click', ()=>{
        const sel = btn.getAttribute('data-copy-target');
        const el = document.querySelector(sel);
        copyText(el ? el.textContent : '');
        const old = btn.textContent; btn.textContent='Copied'; setTimeout(()=>btn.textContent=old, 900);
      });
    });
    document.querySelectorAll('[data-curl]').forEach(btn=>{
      const [method, path] = btn.dataset.curl.split(' ');
      btn.addEventListener('click',()=>{
        $('#curlText').textContent = curlFor(method, path);
        $('#curlModal').style.display='block';
        window.scrollTo({ top: $('#curlModal').offsetTop - 20, behavior:'smooth' });
      });
    });
  }

  // Theme toggle + persist (registered immediately — doesn't depend on config)
  $('#themeToggle')?.addEventListener('click', ()=>{
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try{ localStorage.setItem('biochirp_theme', next); }catch{}
  });
  (function(){ try{
    const saved = localStorage.getItem('biochirp_theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  }catch{} })();

  // ---- boot (needs both the config AND the DOM) ----
  let domReady = false, cfgReady = false;
  function maybeStart(){ if (domReady && cfgReady) start(); }

  function start(){
    const conf = window.__BC_API_CONFIG;
    API_BASE = conf.apiBase;
    POST_PATH = conf.postPath;
    SINGLE_EX = conf.cfg.exampleRequest;
    POST_TIMEOUT_MS = conf.postTimeoutMs;

    if (endpointLabel) endpointLabel.textContent = API_BASE;
    if (pageProto) pageProto.textContent = location.protocol.replace(':','');
    try{ if (apiProto) apiProto.textContent = new URL(API_BASE, location.origin).protocol.replace(':',''); }catch{}
    hideWarnings(); ping();
    setInterval(()=>{ if(!document.hidden) ping(); }, 12000);
    document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) ping(); });
    window.addEventListener('online',  ()=> ping());
    window.addEventListener('offline', ()=> { statusText.textContent='Offline'; statusMeta.textContent=`Offline • ${fmtTime()}`; setConnStatus('Offline', false); });

    runGet('/', '#out-root'); runGet('/health', '#out-health');

    $('#pingBtn')?.addEventListener('click', ping);
    $('#postCurlBtn')?.addEventListener('click', ()=>{
      $('#curlText').textContent = curlFor('POST', POST_PATH);
      $('#curlModal').style.display='block';
      window.scrollTo({ top: $('#curlModal').offsetTop - 20, behavior:'smooth' });
    });
    $('#postTryBtn')?.addEventListener('click', ()=> runPost(POST_PATH, '#in-body', '#out-body'));
    document.querySelectorAll('[data-try]').forEach(btn=>{
      const [method, path] = btn.dataset.try.split(' ');
      btn.addEventListener('click',()=>{
        if (method==='GET' && path==='/')       runGet('/', '#out-root');
        if (method==='GET' && path==='/health') runGet('/health', '#out-health');
      });
    });
    document.addEventListener('keydown', (e)=>{
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter'){
        runPost(POST_PATH, '#in-body', '#out-body');
      }
    });

    $('#fillExample')?.addEventListener('click', ()=>{
      $('#in-body').value = JSON.stringify(SINGLE_EX, null, 2);
      window.scrollTo({ top: document.body.scrollHeight, behavior:'smooth' });
    });
    $('#copySingleEx')?.addEventListener('click', ()=>{
      const txt = $('#example-input').textContent || '';
      copyText(txt);
      const btn = $('#copySingleEx'); const old = btn.textContent; btn.textContent='Copied'; setTimeout(()=>btn.textContent=old, 900);
    });

    wireCopyAndCurlButtons();

    if (quickAsks){
      quickAsks.addEventListener('click', (e)=>{
        const btn = e.target.closest('.qa-card');
        if (!btn) return;
        const preset = btn.getAttribute('data-preset');
        if (!preset) return;
        let obj = null;
        try{ obj = JSON.parse(preset); }catch{}
        if (!obj) return;
        const input = $('#in-body');
        if (input) input.value = JSON.stringify(obj, null, 2);
        const post = $('#postH');
        if (post) post.scrollIntoView({ behavior:'smooth', block:'start' });
      });
    }

    $('#curlClose')?.addEventListener('click',()=> $('#curlModal').style.display='none');
    $('#curlCopy')?.addEventListener('click',()=> copyText($('#curlText').textContent || ''));
  }

  document.addEventListener('DOMContentLoaded', () => { domReady = true; maybeStart(); });
  document.addEventListener('bc-api-config-ready', () => { cfgReady = true; maybeStart(); });
  if (window.__BC_API_CONFIG) { cfgReady = true; }
})();
