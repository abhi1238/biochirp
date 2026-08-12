# Deploying BioChirp yourself

Two independent things you can deploy: the full `docker compose` stack (chat UIs + all 11 databases), and/or the standalone MCP server (for Claude Desktop / claude.ai connectors). You can run either alone or both together.

## The full stack

This is covered in [README.md §5](README.md#5-bring-up) — `docker compose up` after supplying `.env` and the data described in [README.md §4](README.md#4-data-you-need-to-supply). Nothing else is needed; there's no separate packaging or release step for this path.

## The MCP server, standalone

The MCP server (`mcp_server/http_server.py`) is not part of `docker-compose.yml` — it's meant to run as its own process, typically behind an nginx reverse proxy so it's reachable at a stable public URL. This is genuinely optional: the full stack above works without it, and the MCP server needs the full stack's per-DB tool containers running (it proxies queries to them by port — see `mcp_server/server.py`'s DB catalogue).

### Run it directly (simplest)

```bash
python -m mcp_server.http_server --host 127.0.0.1 --port 8765
```

Env vars it reads (all optional):

| Var | Effect |
|---|---|
| `BIOCHIRP_MCP_HOST` / `BIOCHIRP_MCP_PORT` | Override the `--host`/`--port` defaults (`0.0.0.0`/`8765`) |
| `BIOCHIRP_MCP_TOKEN` | If set, requires `Authorization: Bearer <token>` on `/sse`, `/streamable`, and `/messages/`. Discovery/install pages stay public. Unset by default. |
| `MCP_PUBLIC_PREFIX` | Path prefix the SSE handshake tells clients to POST back to (default `/mcp`) — must match whatever prefix your reverse proxy uses |

Verify it's up: `curl http://127.0.0.1:8765/health` → `{"status":"ok","service":"biochirp-mcp","version":"2.0.0"}`.

### Run it as a systemd service

There's no unit file shipped in this repo — here's a minimal one to adapt:

```ini
# /etc/systemd/system/biochirp-mcp.service
[Unit]
Description=BioChirp MCP server
After=network.target

[Service]
Type=simple
User=biochirp
WorkingDirectory=/path/to/biochirp
EnvironmentFile=/path/to/biochirp/.env
ExecStart=/path/to/venv/bin/python -m mcp_server.http_server --host 127.0.0.1 --port 8765
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now biochirp-mcp
```

### Reverse-proxy it with nginx

The server's own routes (from `mcp_server/http_server.py`) are unprefixed — `/sse`, `/streamable`, `/messages/`, `/health`, `/connector`, `/mcp/manifest.json`, `/.well-known/mcp`, `/view.html`, `/csv/{db}/{filename}`. A public deployment typically wants everything under a `/mcp` path prefix except discovery/install pages, which stay at the root. There's no `deploy/nginx-mcp.conf` shipped — here's a minimal snippet to adapt inside your TLS `server {}` block:

```nginx
location = /mcp {
    proxy_pass http://127.0.0.1:8765/streamable;
    include /etc/nginx/snippets/ws_common.conf;  # see ws_common.conf at repo root
}
location = /mcp/sse {
    proxy_pass http://127.0.0.1:8765/sse;
    include /etc/nginx/snippets/ws_common.conf;
    proxy_buffering off;  # required for SSE streaming
}
location = /mcp/messages/ {
    proxy_pass http://127.0.0.1:8765/messages/;
}
location = /mcp/manifest.json { proxy_pass http://127.0.0.1:8765/mcp/manifest.json; }
location = /.well-known/mcp   { proxy_pass http://127.0.0.1:8765/.well-known/mcp; }
location ^~ /connector        { proxy_pass http://127.0.0.1:8765; }
location = /view.html         { proxy_pass http://127.0.0.1:8765/view.html; }
location ^~ /csv/             { proxy_pass http://127.0.0.1:8765; }
```

Apply nginx rate limits for these routes too — see [`deploy/SECURITY.md`](deploy/SECURITY.md).

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Then point a Claude Desktop config at it:

```json
{
  "mcpServers": {
    "biochirp": { "type": "sse", "url": "https://your-domain/mcp/sse" }
  }
}
```

See [`CONNECTOR_FROM_WEB.md`](CONNECTOR_FROM_WEB.md) for the full connector picture (what's actually implemented vs. what a real deployment of this reverse-proxy setup would add).

## Citation metadata

[`CITATION.cff`](CITATION.cff) and [`.zenodo.json`](.zenodo.json) are kept in the repo for anyone who wants to mint a Zenodo DOI via GitHub's Zenodo integration (Zenodo account → toggle the repo on → cut a GitHub release). There's no pre-built release bundle in this repo to attach — that's a step you'd do yourself if and when you want one.
