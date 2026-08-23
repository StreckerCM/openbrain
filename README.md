# OpenBrain

A shared knowledge base for Strecker development projects. Stores structured knowledge entries, project metadata, and persistent agent memories with vector embeddings for semantic search. Agents access it via an **MCP gateway** (Model Context Protocol) or a **PostgREST API**.

## Architecture

```
Claude Code / Agent
        ↓ stdio
  supergateway (local)          ← converts stdio ↔ Streamable HTTP
        ↓ Streamable HTTP
  Nginx Reverse Proxy
        ↓
  mcp-gateway (Python FastMCP)  ← 18 domain-specific tools
        ↓
  PostgreSQL + pgvector
```

The **mcp-gateway** is a custom Python server built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk). It exposes 18 domain-specific tools for managing knowledge, projects, memories, lifecycle (archive/unarchive), and cross-project linking — with built-in semantic search via OpenAI embeddings.

A separate **embedder** service runs in the background, polling every 30 seconds to generate vector embeddings for any new or updated rows.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [Node.js](https://nodejs.org/) 18+ (for client-side MCP connection via supergateway)
- An [OpenAI API key](https://platform.openai.com/api-keys) (for embeddings)
- Network access to the host running the stack (LAN, Tailscale, or WireGuard)

## Server Setup

### 1. Clone and configure

```bash
git clone <your-repo-url>
cd openbrain
```

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

```env
OPENAI_API_KEY=sk-your-key-here
POSTGRES_PASSWORD=change-me
PGRST_JWT_SECRET=<output of: openssl rand -hex 32>

# Optional — Sentry error tracking (https://sentry.io)
SENTRY_DSN=
```

`POSTGRES_DB` and `POSTGRES_USER` default to `openbrain` if not set. `SENTRY_DSN` is optional — if set, error tracking is enabled across mcp-gateway, embedder, and the web UI.

### 2. Start the stack

```bash
docker compose up -d --build
```

This launches eight services:

| Service | Port | Description |
|---------|------|-------------|
| **db** | 5433 | PostgreSQL 17 with pgvector extension |
| **mcp-gateway** | 3007, 3011 | Python FastMCP server — 3007 serves `/mcp` (public-facing, OAuth), 3011 serves the write REST API for the web UI (private only) |
| **web-ui** | 3010 | Dashboard SPA — browse, search, create, edit, archive, delete |
| **postgrest** | 3006 | REST API over the database (read layer for web UI) |
| **embedder** | — | Background service that generates vector embeddings every 30s |
| **adminer** | 3008 | Web-based database browser |
| **docs** | — | Nginx serving the docs directory |
| **cloudflared** | — | Tunnel sidecar that exposes `mcp-gateway`'s `/mcp` port to the public internet |

> **Note:** `docker compose up` requires `CLOUDFLARED_CREDENTIALS_FILE` to point at a
> real file, even for a local-only stack — compose fails fast with `Set
> CLOUDFLARED_CREDENTIALS_FILE in .env` otherwise. See [Remote access](#remote-access)
> for what that file is; point it at any placeholder file if you don't need the tunnel
> running locally.

### 3. Verify

```bash
# Check all services are running
docker compose ps

# Test the MCP gateway (should return a JSON-RPC response).
# MCP_AUTH_ENABLED defaults to true, so this returns 401 unless you add
# -H "Authorization: Bearer <token>" — see Remote access below.
curl -X POST http://localhost:3007/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Test PostgREST
curl http://localhost:3006/projects
```

## Client Setup (Connecting Claude Code)

Claude Code communicates with MCP servers over **stdio**. Since the OpenBrain MCP gateway speaks **Streamable HTTP**, you need [supergateway](https://github.com/supercorp-ai/supergateway) running locally to bridge the two protocols.

### Option A: Using npx (recommended)

Add this to your Claude Code MCP config (`~/.claude/mcp.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "openbrain": {
      "command": "npx",
      "args": [
        "-y",
        "supergateway",
        "--streamableHttp",
        "https://brain.streckercm.com/mcp"
      ]
    }
  }
}
```

> **Windows (nvm-windows):** If Node isn't in your shell PATH, use the full path and add an `env` block:
> ```json
> "command": "C:\\nvm4w\\nodejs\\npx.cmd",
> "env": { "PATH": "C:\\nvm4w\\nodejs;${PATH}" }
> ```

### Option B: Global install

```bash
npm install -g supergateway
```

Then configure:

```json
{
  "mcpServers": {
    "openbrain": {
      "command": "supergateway",
      "args": [
        "--streamableHttp",
        "https://brain.streckercm.com/mcp"
      ]
    }
  }
}
```

### Option C: Local development (direct to Docker)

If you're running the stack locally, point to `localhost` instead:

```json
{
  "mcpServers": {
    "openbrain": {
      "command": "npx",
      "args": [
        "-y",
        "supergateway",
        "--streamableHttp",
        "http://localhost:3007/mcp"
      ]
    }
  }
}
```

> **Note:** This local setup uses supergateway to bridge stdio to the gateway's Streamable HTTP endpoint. If you're connecting to the public OAuth-protected endpoint instead, use Claude Code's native `"type": "http"` transport with a bearer token — see [Remote access](#remote-access) — rather than supergateway.

### Verify the connection

After configuring, restart Claude Code and check that the MCP tools are available:

```
/mcp
```

You should see 18 tools across five domains: knowledge, projects, memories, lifecycle, and links.

## Available MCP Tools

### Knowledge (4 tools)

| Tool | Description |
|------|-------------|
| `add_knowledge` | Add a knowledge entry (title, content, project, category, tags, url) |
| `search_knowledge` | Semantic or text search across knowledge entries |
| `list_knowledge` | Browse and filter knowledge entries |
| `archive_knowledge` | Archive a knowledge entry and its project links |

### Projects (5 tools)

| Tool | Description |
|------|-------------|
| `add_project` | Register a project (name, description, repo_url, tech_stack, orphan_policy) |
| `update_project` | Update an existing project's details |
| `list_projects` | List all projects, optionally filtered by technology |
| `get_project` | Get full details for a specific project |
| `archive_project` | Archive a project, cascade links, handle orphans per policy |

### Memories (4 tools)

| Tool | Description |
|------|-------------|
| `save_memory` | Store a persistent memory (type: user, feedback, project, reference) |
| `recall_memory` | Semantic or text search across memories |
| `list_memories` | Browse and filter stored memories |
| `archive_memory` | Archive a memory and its project links |

### Lifecycle (3 tools)

| Tool | Description |
|------|-------------|
| `unarchive_knowledge` | Restore an archived knowledge entry to active |
| `unarchive_memory` | Restore an archived memory to active |
| `unarchive_project` | Restore an archived project to active |

### Links (2 tools)

| Tool | Description |
|------|-------------|
| `link_to_project` | Associate a knowledge entry or memory with a project |
| `unlink_from_project` | Remove association between an entity and a project |

## Database Schema

### `knowledge` — Factual and reference content

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `project` | text | Provenance — which project created this entry (default: `general`) |
| `category` | text | Entry category (default: `general`) |
| `title` | text | Short title |
| `content` | text | Full content |
| `url` | text | Optional reference URL |
| `tags` | text[] | Searchable tags |
| `status` | text | `active` or `archived` (default: `active`) |
| `embedding` | vector(1536) | Auto-generated by embedder |
| `created_at` | timestamptz | Auto-set |
| `updated_at` | timestamptz | Auto-set |

### `projects` — Project registry

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `name` | text | Unique project name |
| `description` | text | Project description |
| `repo_url` | text | Repository URL |
| `tech_stack` | text[] | Technologies used |
| `notes` | text | Freeform notes |
| `status` | text | `active`, `archived`, or `system` (default: `active`) |
| `orphan_policy` | text | `archive` or `reassign` (NULL = use env var default) |
| `created_at` | timestamptz | Auto-set |
| `updated_at` | timestamptz | Auto-set |

A system project named `general` (status `system`) is created automatically and cannot be archived. It serves as the default for non-project-specific knowledge and memories.

### `memories` — Persistent agent memory

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `memory_type` | text | One of: `user`, `feedback`, `project`, `reference` |
| `name` | text | Short name |
| `description` | text | One-line description for relevance matching |
| `content` | text | Full memory content |
| `project` | text | Provenance — which project created this (default: `general`) |
| `status` | text | `active` or `archived` (default: `active`) |
| `embedding` | vector(1536) | Auto-generated |
| `created_at` | timestamptz | Auto-set |
| `updated_at` | timestamptz | Auto-set |

### `project_links` — Junction table

Associates knowledge entries and memories with projects. Enables many-to-many relationships: one knowledge entry can be linked to multiple projects, and one project can have many entries.

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `project_id` | int | FK → projects(id) |
| `knowledge_id` | int | FK → knowledge(id), nullable |
| `memory_id` | int | FK → memories(id), nullable |
| `status` | text | `active` or `archived` |
| `created_at` | timestamptz | Auto-set |
| `archived_at` | timestamptz | Set when link is archived |

Exactly one of `knowledge_id` or `memory_id` must be non-null per row.

## Web UI Dashboard

The **web-ui** service at port `3010` provides a full management dashboard for the knowledge base. Open `http://localhost:3010` in a browser after starting the stack.

**Features:**
- Browse, search, create, edit, and archive knowledge entries, memories, and projects
- Semantic search (OpenAI embeddings) with text search fallback
- Project link management (link/unlink entities across projects)
- Dashboard with stats, recent activity, and orphan alerts
- Archive view with bulk restore and permanent delete
- Dark theme, responsive sidebar with mobile hamburger menu

**Tech stack:** Preact + HTM (no build step), marked.js for markdown rendering, Nginx reverse proxy. Reads go through PostgREST, writes through mcp-gateway REST endpoints.

**Sentry error tracking (optional):** Set `SENTRY_DSN` in your `.env` file to enable error tracking across mcp-gateway, embedder, and the web UI frontend.

## Network Access

Access is restricted to:

| Network | CIDR |
|---------|------|
| LAN | `192.168.1.0/24` |
| Tailscale | `100.72.222.0/24`, `100.87.233.84` |
| WireGuard | `10.0.0.0/24` |

This restricts every service except the MCP endpoint. See [Remote access](#remote-access)
for the OAuth-protected public path in front of `/mcp`.

## Remote access

The MCP endpoint at `https://openbrain-mcp.streckercm.com/mcp` is reachable from the
public internet through a Cloudflare Tunnel and requires an OAuth 2.1 bearer token.
Everything else — the web UI, PostgREST, Adminer, and the write REST API — stays on
the LAN and tailnet only.

The gateway runs two listeners: a public one serving `/mcp` and the metadata routes,
and a private one serving `/api/*`. The public listener 404s anything outside those two
paths, so an ingress rule that only ever targets the public port cannot reach the write
API whatever path it allows. That guarantee comes from the listener's own behavior, not
from network isolation — `cloudflared` and `mcp-gateway` sit on the same Docker network,
and nothing stops the tunnel reaching the private port except that no ingress rule names
it.

Authentication is enforced inside the gateway, not at the edge. A request arriving over
the Cloudflare tunnel, the tailnet, or the LAN faces the same check. This is why the
hostname can safely appear in local DNS.

### Authentik setup

Create an OAuth2/OpenID provider named `OpenBrain MCP`:

| Setting | Value |
|---|---|
| Client type | Confidential |
| Redirect URIs | The callback URLs claude.ai and ChatGPT present during connector setup |
| Signing key | An RS256 certificate |
| Scopes | `openid`, `profile`, `email`, `openbrain:read`, `openbrain:write` |
| Subject mode | Based on user ID |

Create an application with slug `openbrain-mcp`, and bind a policy restricting it to your
own account or a dedicated group — without one, every Authentik user can mint a working
token.

Confirm that issued tokens carry `https://openbrain-mcp.streckercm.com/mcp` in `aud`. If
they do not, add a scope mapping that sets it; the gateway rejects tokens whose audience
is not this server.

**`MCP_REQUIRED_SCOPES` is space-separated**, not comma-separated — for example
`openbrain:read openbrain:write` — and defaults to `openbrain:read`. Setting it to an
empty or whitespace-only value makes the required-scope set empty too, and the gateway
then admits *any* authenticated token regardless of what scopes it carries. The audience
check still gates who can obtain a usable token at all, so this isn't a full bypass — but
it's an easy value to type by accident if you don't want scope enforcement, and the
result should not be a surprise. Leave it set to at least `openbrain:read`.

### Browser clients

In claude.ai or ChatGPT, add a custom connector pointing at
`https://openbrain-mcp.streckercm.com/mcp` and enter the Client ID and Secret from
Authentik under Advanced settings. Dynamic client registration is not used.

### Headless agents

Agents with no browser use a static token instead. Add it to `MCP_STATIC_TOKENS` in
`.env` — a comma-separated list, so each agent can carry its own token — then configure
the client:

```json
{
  "mcpServers": {
    "openbrain": {
      "type": "http",
      "url": "https://openbrain-mcp.streckercm.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN_HERE" }
    }
  }
}
```

Keep the real token out of the tracked `.mcp.json`. Add the server at local scope
(`claude mcp add-json`, the default scope, not `--scope project`) so it lives in
`~/.claude.json` instead, or reference `${MCP_TOKEN}` in `headers` and set that
environment variable outside the file.

A static token receives **both** `openbrain:read` and `openbrain:write` — there is no
per-token scope — and it **never expires**. Anyone holding one has full read/write access
to the knowledge base for as long as it stays in `MCP_STATIC_TOKENS`. Treat it like a
password. Rotate one by editing `MCP_STATIC_TOKENS` and running
`docker compose restart mcp-gateway`.

### Running without authentication

On a fully private deployment, set `MCP_AUTH_ENABLED=false`. The gateway refuses to start
if auth is enabled and the issuer, JWKS URL, or resource URI is unset, rather than
falling back to serving unauthenticated.

## Additional Documentation

See [docs/readme.md](docs/readme.md) for the full agent reference including PostgREST API examples, semantic search, and the embedder service details.
