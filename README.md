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
```

`POSTGRES_DB` and `POSTGRES_USER` default to `openbrain` if not set.

### 2. Start the stack

```bash
docker compose up -d --build
```

This launches seven services:

| Service | Port | Description |
|---------|------|-------------|
| **db** | 5433 | PostgreSQL 17 with pgvector extension |
| **mcp-gateway** | 3007 | Python FastMCP server — MCP endpoint + REST API for web UI |
| **web-ui** | 3010 | Dashboard SPA — browse, search, create, edit, archive, delete |
| **postgrest** | 3006 | REST API over the database (read layer for web UI) |
| **embedder** | — | Background service that generates vector embeddings every 30s |
| **adminer** | 3008 | Web-based database browser |
| **docs** | — | Nginx serving the docs directory |

> **Note:** The compose file references an external `nginxproxymanager_default` network for reverse proxy integration. If you're not using Nginx Proxy Manager, remove the `networks: nginxproxymanager_default` references from `docker-compose.yml` and access services directly on their mapped ports.

### 3. Verify

```bash
# Check all services are running
docker compose ps

# Test the MCP gateway (should return a JSON-RPC response)
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

> **Important:** Do not use `"type": "http"` — Claude Code's native HTTP transport triggers OAuth discovery, which this server does not support. Use supergateway to bridge stdio ↔ Streamable HTTP instead.

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

## Additional Documentation

See [docs/readme.md](docs/readme.md) for the full agent reference including PostgREST API examples, semantic search, and the embedder service details.
