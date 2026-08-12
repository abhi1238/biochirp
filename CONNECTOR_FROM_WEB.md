# Making BioChirp a Claude Desktop / claude.ai connector

BioChirp's MCP server (`mcp_server/http_server.py`) can be reached over SSE from any MCP client, including Claude Desktop and claude.ai's Connectors panel, once it's reverse-proxied to a public URL — see [`DEPLOY.md`](DEPLOY.md) for actually standing it up.

## What the server actually exposes

Verified directly against `mcp_server/http_server.py`'s route table and `mcp_server/web/manifest.json`:

| Route | Returns |
|---|---|
| `/sse` | The Server-Sent Events MCP transport |
| `/streamable` | The Streamable-HTTP MCP transport (claude.ai connector) |
| `/messages/` | MCP message ingress (POST, used by the SSE transport) |
| `/health` | Liveness probe — `{"status":"ok","service":"biochirp-mcp","version":"2.0.0"}` |
| `/connector`, `/connector/install.html` | Install/landing page (served from `mcp_server/web/install.html`) |
| `/mcp/manifest.json` | Machine-readable connector manifest — currently declares **2 transports** (streamable-http, sse) and **12 tools** (one per database + `web_search_live`) |
| `/.well-known/mcp` | RFC-style discovery document |
| `/view.html`, `/connector/view.html` | CSV result viewer page |
| `/csv/{db}/{filename}` | Proxies a per-DB tool's downloadable result CSV through the public endpoint |

All served by the single `mcp_server.http_server:app` process (Starlette + uvicorn).

There is currently no `.dxt` bundle download route and no published PyPI package for this server — if you want either of those install paths, they'd need to be built; right now the only working install path is pointing an MCP client at the SSE or streamable-HTTP URL directly.

## Claude Desktop config

```json
{
  "mcpServers": {
    "biochirp": {
      "type": "sse",
      "url": "https://your-domain/mcp/sse"
    }
  }
}
```

## Submitting to third-party MCP registries

Registries that ingest a manifest URL (Smithery, mcp-get, the community servers list at `github.com/modelcontextprotocol/servers`) can point at `https://your-domain/mcp/manifest.json` once your instance is publicly reachable. When writing a description for one of these, use the real numbers: **11 curated biomedical databases** (see [README.md §1](README.md#1-the-11-databases)) plus a live web-search fallback, not a larger historical figure — check `mcp_server/web/manifest.json` for the current, authoritative description text.
