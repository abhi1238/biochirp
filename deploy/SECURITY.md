# BioChirp WS / MCP cost-runaway protection

Two-layer protection against the P0 risk that an unauthenticated client opens
hundreds of chat WebSockets and runs up the LLM bill:

| Layer | Where | What it stops |
|-------|-------|---------------|
| A. nginx rate limits | edge | distributed open-storms, reconnect loops |
| B. HMAC token gate   | FastAPI WS handshake | clients that bypass nginx (direct backend hits, NAT-shared traffic above the per-IP burst) |

The two layers are independent and ship in a safe **off-by-default** posture
for Layer B so the gate can be deployed before the frontend ships the
matching `/auth/token` fetch. Flip `BIOCHIRP_WS_AUTH_REQUIRED=1` to enforce.

---

## Layer A — nginx rate limits

**Files**
- `deploy/nginx-rate-limits.conf` — `limit_*_zone` declarations (must live in
  `http{}` scope; install via `/etc/nginx/conf.d/`).
- `ws_common.conf` — included by every chat-route location block; now also
  carries the matching `limit_req` / `limit_conn` directives.
- `deploy/nginx-mcp.conf` — already wires limits on `/mcp`, `/mcp/sse`,
  `/mcp/messages/`. Verify it's `include`d from the apex config inside the
  `listen 443` block.

**Install**

```bash
sudo bash scripts/install_nginx_security.sh
```

Idempotent. Backs up any existing target before overwriting. Runs `nginx -t`
and rolls back if validation fails before reloading.

**Tuned values** (chat WS; see `ws_common.conf`):

| Directive | Value | Meaning |
|-----------|-------|---------|
| `limit_req zone=biochirp_ws burst=5 nodelay` | 12 req/min refill, 5-burst | Bounds WS-handshake rate per IP. Steady state ≈ 1 open every 5s. `limit_req` counts the HTTP upgrade only — established sessions stream freely. |
| `limit_conn biochirp_conn 3` | 3 concurrent | Max 3 open WS per IP. 6th tab from one IP gets HTTP 503. |

**Caveat — NAT / institutional shared IP.** If many users sit behind one
gateway IP (IIITD campus), they compete for the 3-connection budget. Loosen
`limit_conn` to 8–16 if you see legitimate `limit_conn_zone` warnings in
`/var/log/nginx/error.log`. Or move auth enforcement (Layer B) on and rely
on per-token, not per-IP.

**Monitor**

```bash
sudo tail -F /var/log/nginx/error.log | grep -E 'limit_(req|conn)'
```

---

## Layer B — HMAC token gate

**Files**
- `app/utils/ws_auth.py` — token mint/verify, IP-bound, 15-min TTL.
- `bio_chat_service/app/main.py` — `GET /auth/token` route + `gate_ws()`
  check before both `/bio_chat` and `/bio_chat_v2` accept().
- `app/per_db_chat/_main.py` — same wiring for all per-DB chat services.
- `frontend/assets/ws-auth-shim.js` — wraps `window.WebSocket` to fetch
  `/auth/token` and append `?token=` before opening.

**How it stays compatible while disabled**

`BIOCHIRP_WS_AUTH_REQUIRED` defaults to `0`. While unset:
- `/auth/token` still returns a valid token (so the frontend can already
  ship the shim — it's a no-op).
- `gate_ws()` returns `True` immediately without inspecting the token.
- The shim opens the WS with `?token=` appended; the server ignores it.

**Enable enforcement**

1. **Pin the secret** so tokens survive a server restart:

   ```bash
   # Option A — env var on every chat service container
   echo "BIOCHIRP_WS_AUTH_SECRET=$(openssl rand -hex 32)" >> .env

   # Option B — a file at /var/lib/biochirp/ws_auth_secret (0600, written
   # automatically on first use). Mount it as a volume into every chat
   # container so all replicas share the same secret.
   ```

   *If neither is set*, each worker generates an ephemeral in-process secret
   on first call; tokens issued by one worker won't validate against another.
   Fine for dev, broken for prod with `>1` worker.

2. **Flip the gate on:**

   ```bash
   # Add to .env (already shared via x-chat-env-base anchor in compose):
   BIOCHIRP_WS_AUTH_REQUIRED=1
   ```

   Recreate the chat containers:

   ```bash
   docker compose up -d biochirp_bio_chat $(docker compose config --services | grep _chat$)
   ```

3. **Verify** — open the chat page in two browsers:
   - Browser → `/auth/token` → 200 with `{"token": "...", "expires_at": ...}`
   - WS handshake → 101 Switching Protocols
   - Direct WS open without token → server closes with code 1008
     (`websocat 'wss://biochirp.iiitd.edu.in/bio_chat/'` should fail).

**Token shape**

`<base64url(ip|exp)>.<base64url(hmac_sha256(secret, ip|exp))>`

- `ip` is the client IP from `X-Forwarded-For` (first hop) or the peer.
- `exp` is a Unix epoch seconds, default `now + 900` (15 min).
- Tokens are not single-use — they can be replayed by the same IP for
  their lifetime. Acceptable for chat (browser keeps the WS open) and
  not worth the storage cost of a revocation list.

---

## MCP endpoint

`https://biochirp.iiitd.edu.in/mcp/sse` is **deliberately left as Layer A
only** because it's a documented public endpoint registered with claude.ai
connectors — turning on bearer auth without coordinating a token-distribution
flow would break every external user (see `memory/mcp_connector_deployment.md`).

The bearer scaffolding is already there in
`mcp_server/http_server.py:_OptionalBearerAuth`. When you're ready:

```bash
export BIOCHIRP_MCP_TOKEN=$(openssl rand -hex 32)
sudo systemctl restart biochirp-mcp.production
```

Then update the manifest in `mcp_server/web/` and the install page so
external users get the new token before their connectors break.

---

## Smoke tests

```bash
# Token endpoint round-trip (token should be ~110 chars, two base64url parts).
curl -fsS https://biochirp.iiitd.edu.in/auth/token | jq .

# WS handshake with no token — should accept while flag is OFF.
websocat 'wss://biochirp.iiitd.edu.in/bio_chat/'

# Flip BIOCHIRP_WS_AUTH_REQUIRED=1, then:
websocat 'wss://biochirp.iiitd.edu.in/bio_chat/'             # closes 1008
TOKEN=$(curl -fsS https://biochirp.iiitd.edu.in/auth/token | jq -r .token)
websocat "wss://biochirp.iiitd.edu.in/bio_chat/?token=${TOKEN}"   # accepts

# Rate-limit smoke — burst 10 handshakes from one IP; expect ~5 to succeed.
for i in $(seq 1 10); do
  curl -sS -o /dev/null -w "%{http_code}\n" -H "Connection: Upgrade" \
       -H "Upgrade: websocket" -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
       -H "Sec-WebSocket-Version: 13" \
       https://biochirp.iiitd.edu.in/bio_chat/ &
done
wait
# Look for 503s in the output.
```
