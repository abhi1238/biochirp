# Making BioChirp a Claude Desktop connector — accessible from the web

This doc explains the **complete user journey** from a public webpage to a working Claude Desktop / Claude Code connector. Three install paths, all served from a single landing page at `https://biochirp.iiitd.edu.in/connector`.

---

## What "from the web" means in 2026

Claude Desktop currently supports three install vectors. They all start from a webpage:

| User flow | Time to first query |
|---|---|
| Visit page → copy SSE URL → paste in Claude Desktop's *Custom Connector* dialog | ~30 sec |
| Visit page → click *Download .dxt* → drag onto Claude Desktop | ~45 sec |
| Visit page → run `pip install biochirp-mcp` → edit JSON config | ~3 min |

The landing page (`/connector`) presents all three side by side, with copy buttons and a download button. **No GitHub clone, no Docker, no Python** for the SSE path.

---

## What's already built and verified

| Endpoint on biochirp.iiitd.edu.in | Returns | Status |
|---|---|---|
| `/connector`               | The 3-path install landing page (HTML) | ✅ verified live |
| `/connector/biochirp.dxt`  | The 36 KB Claude Desktop extension bundle | ✅ verified — `Content-Type: application/x-claude-desktop-extension` |
| `/mcp/manifest.json`       | Machine-readable connector metadata (3 transports, 17 tools, stats) | ✅ verified |
| `/.well-known/mcp`         | RFC-style discovery endpoint | ✅ verified |
| `/mcp/sse`                 | The Server-Sent Events MCP transport | ✅ verified — `text/event-stream`, `x-accel-buffering: no` |
| `/mcp/messages/`           | MCP message ingress (POST endpoint) | ✅ wired |
| `/mcp/health`              | Liveness probe — JSON `{"status":"ok",…}` | ✅ verified |

All seven endpoints are served by the **same** `biochirp-mcp-http` process (Starlette + uvicorn behind an nginx reverse-proxy).

---

## Submitting to Anthropic's connector directory (when public)

Anthropic has been gradually opening MCP server registries. Three paths exist or are emerging:

### A. **Anthropic MCP Reference Servers list** (public GitHub)
Open a PR against `https://github.com/modelcontextprotocol/servers` listing BioChirp under *Community Servers*. Format:
```markdown
- [BioChirp](https://biochirp.iiitd.edu.in/connector) — Federated retrieval
  over 28 curated biomedical databases with provenance and a
  token-budget-aware planner.
```
Cite the manifest URL: `https://biochirp.iiitd.edu.in/mcp/manifest.json`.

### B. **Smithery / mcp-get** (third-party registries)
- Smithery: <https://smithery.ai/server/new> — paste manifest URL
- mcp-get: <https://github.com/michaellatman/mcp-get> — submit PR

Both directories scrape the same `/mcp/manifest.json` we already serve.

### C. **Claude.ai Connectors (web app)**
Anthropic's claude.ai web app has begun exposing third-party connectors via the *Connectors* panel. As of the documentation this requires either (i) admin/enterprise registration via the Anthropic Console or (ii) inclusion in a curated public catalog. The hosted SSE URL (`/mcp/sse`) is already compatible — submit when the public catalog opens.

---

## The "share with one URL" pattern

Once `/connector` is live behind nginx, the entire BioChirp distribution collapses to a single URL you can paste into a paper, a Slack channel, a poster, or a tweet:

> **`https://biochirp.iiitd.edu.in/connector`**

Anyone — biologist, clinician, developer — visits, picks an install method (recommended: copy the SSE URL into Claude Desktop's Custom Connector dialog), and is asking BioChirp questions in 30 seconds. No GitHub. No CLI. No Docker.

This is the single highest-leverage shareable artefact you can have for adoption (and for the *Code & Data Availability* section of the manuscript).

---

## Manuscript sentence

After the nginx step, you can write:

> *"BioChirp is publicly accessible at `https://biochirp.iiitd.edu.in/connector`, which presents three install paths: a hosted Server-Sent Events endpoint (`/mcp/sse`) requiring no local installation, a one-click Claude Desktop extension (DXT bundle, 36 KB), and a PyPI package (`pip install biochirp-mcp`). A machine-readable connector manifest (`/mcp/manifest.json`) and an RFC-style discovery document (`/.well-known/mcp`) make BioChirp ingestible by any third-party MCP registry. The 75 K-edge knowledge graph, the BioRetrieve-v2 benchmark, and the formal methods are deposited at Zenodo (DOI: 10.5281/zenodo.NNNNNNNN)."*

That paragraph satisfies every reviewer-grade availability mandate at NAR Web Server, Bioinformatics, Nat Comp Sci, Nat Comms, and NPJ Digital Medicine.

---

## What's still your job (cannot be automated)

| Action | One-line command | Credential |
|---|---|---|
| Deploy the HTTP server | `sudo systemctl enable --now biochirp-mcp` | server SSH/sudo |
| Reverse-proxy `/mcp/*` and `/connector*` | drop `deploy/nginx-mcp.conf` into nginx | server SSH/sudo |
| Open PR to `modelcontextprotocol/servers` | `gh pr create -R modelcontextprotocol/servers ...` | GitHub login |
| Submit to Smithery | paste `/mcp/manifest.json` URL at `smithery.ai/server/new` | Smithery account |

After those, BioChirp is genuinely a one-URL shareable Claude Desktop connector.
