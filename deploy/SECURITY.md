# BioChirp WS / MCP cost-runaway protection

The risk this guards against: an unauthenticated client opens many chat WebSockets or MCP connections and runs up the LLM bill behind them. Currently one layer of protection is actually implemented and shipped:

| Layer | Where | What it stops | Status |
|-------|-------|---------------|--------|
| A. nginx rate limits | edge | distributed open-storms, reconnect loops | **Implemented** |
| B. Per-connection auth gate | application layer | clients that bypass nginx entirely (direct backend hits, NAT-shared traffic above the per-IP burst) | **Not implemented for chat WebSockets** — see below |

---

## Layer A — nginx rate limits

**Files**
- `deploy/nginx-rate-limits.conf` — `limit_*_zone` declarations (must live in
  `http{}` scope; install via `/etc/nginx/conf.d/`).
- `ws_common.conf` — included by every chat-route location block; carries
  the matching `limit_req` / `limit_conn` directives.

If you're also reverse-proxying the MCP server (see [`DEPLOY.md`](../DEPLOY.md)), apply the same `limit_req`/`limit_conn` directives to its `location` blocks — there's no separate `deploy/nginx-mcp.conf` shipped, so add them to whatever nginx config you write for that.

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
gateway IP, they compete for the 3-connection budget. Loosen
`limit_conn` to 8–16 if you see legitimate `limit_conn_zone` warnings in
`/var/log/nginx/error.log`.

**Monitor**

```bash
sudo tail -F /var/log/nginx/error.log | grep -E 'limit_(req|conn)'
```

---

## Layer B — application-level auth

**Chat WebSockets** (`/{db}_chat/`, e.g. `/ttd_chat/`, served by `app/per_db_tool/schema_kg_chat.build_chat_router()`) have no token-gate today — nginx rate limiting (Layer A) is the only protection in front of them. If you need per-connection auth here, it would need to be built against `schema_kg_chat.py`'s router; nothing in the current codebase implements it.

**The MCP server** (`mcp_server/http_server.py`) is different — it already has a real, working bearer-token gate: `_OptionalBearerAuth`, applied to `/sse`, `/streamable`, and `/messages/`. It's off by default (empty `BIOCHIRP_MCP_TOKEN`). To turn it on:

```bash
export BIOCHIRP_MCP_TOKEN=$(openssl rand -hex 32)
# then restart however you're running mcp_server.http_server — see DEPLOY.md
```

Requests to those three paths then need `Authorization: Bearer <token>`; discovery/install pages (`/connector`, `/mcp/manifest.json`, `/.well-known/mcp`, `/health`) stay public regardless. If you're running a public MCP endpoint that other people's Claude Desktop configs already point at, coordinate a token-distribution plan before flipping this on — every existing connector breaks the moment you do.

---

## Smoke tests

```bash
# Rate-limit smoke test against a real chat WS route — burst 10 handshakes
# from one IP against a running per-DB tool (e.g. ttd on 8012); expect
# roughly 5 to succeed given the burst=5 setting above.
for i in $(seq 1 10); do
  curl -sS -o /dev/null -w "%{http_code}\n" -H "Connection: Upgrade" \
       -H "Upgrade: websocket" -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
       -H "Sec-WebSocket-Version: 13" \
       "https://your-domain/ttd_chat/" &
done
wait
# Look for 503s in the output.

# MCP bearer-token gate, once BIOCHIRP_MCP_TOKEN is set:
curl -s -o /dev/null -w "%{http_code}\n" https://your-domain/mcp/sse        # 401, no token
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer wrong"   \
     https://your-domain/mcp/sse                                            # 403, wrong token
```
