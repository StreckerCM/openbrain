# OpenBrain

A shared knowledge base for Strecker development projects. Stores structured knowledge entries, project metadata, shared resources, and persistent agent memories with vector embeddings for semantic search. Agents access it via an **MCP gateway** (Model Context Protocol) or a **PostgREST API**.

## Architecture

```
Claude Code / Agent
        ↓ stdio
  supergateway (local)          ← converts stdio ↔ Streamable HTTP
        ↓ Streamable HTTP
  Nginx Reverse Proxy
        ↓
  mcp-gateway (Python FastMCP)  ← 12 domain-specific tools
        ↓
  PostgreSQL + pgvector
```

The **mcp-gateway** is a custom Python server built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk). It exposes 12 domain-specific tools for managing knowledge, shared resources, projects, and memories — with built-in semantic search via OpenAI embeddings.

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

This launches six services:

| Service | Port | Description |
|---------|------|-------------|
| **db** | 5433 | PostgreSQL 17 with pgvector extension |
| **mcp-gateway** | 3007 | Python FastMCP server — Streamable HTTP MCP endpoint |
| **postgrest** | 3006 | REST API over the database |
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

You should see 12 tools across four domains: knowledge, shared resources, projects, and memories.

## Available MCP Tools

### Knowledge (3 tools)

| Tool | Description |
|------|-------------|
| `add_knowledge` | Add a knowledge entry (project, title, content, category, tags) |
| `search_knowledge` | Semantic or text search across knowledge entries |
| `list_knowledge` | Browse and filter knowledge entries |

### Shared Resources (3 tools)

| Tool | Description |
|------|-------------|
| `add_shared_resource` | Add a cross-project resource (type, name, description, url) |
| `search_shared_resources` | Semantic or text search across shared resources |
| `list_shared_resources` | Browse and filter shared resources |

### Projects (3 tools)

| Tool | Description |
|------|-------------|
| `add_project` | Register a project (name, description, repo_url, tech_stack) |
| `list_projects` | List all projects, optionally filtered by technology |
| `get_project` | Get full details for a specific project |

### Memories (3 tools)

| Tool | Description |
|------|-------------|
| `save_memory` | Store a persistent memory (type: user, feedback, project, reference) |
| `recall_memory` | Semantic or text search across memories |
| `list_memories` | Browse and filter stored memories |

## Database Schema

### `knowledge` — Project-specific entries

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `project` | text | Project name |
| `category` | text | Entry category (default: `general`) |
| `title` | text | Short title |
| `content` | text | Full content |
| `tags` | text[] | Searchable tags |
| `embedding` | vector(1536) | Auto-generated by embedder |
| `created_at` | timestamptz | Auto-set |
| `updated_at` | timestamptz | Auto-set |

### `shared_resources` — Cross-project references

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `resource_type` | text | Category (e.g. `library`, `service`, `tool`) |
| `name` | text | Short name |
| `description` | text | Full description |
| `url` | text | Optional reference URL |
| `projects` | text[] | Applicable projects (empty = all) |
| `metadata` | jsonb | Flexible extra data |
| `embedding` | vector(1536) | Auto-generated |

### `projects` — Project registry

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `name` | text | Unique project name |
| `description` | text | Project description |
| `repo_url` | text | Repository URL |
| `tech_stack` | text[] | Technologies used |
| `notes` | text | Freeform notes |

### `memories` — Persistent agent memory

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `memory_type` | text | One of: `user`, `feedback`, `project`, `reference` |
| `name` | text | Short name |
| `description` | text | One-line description for relevance matching |
| `content` | text | Full memory content |
| `project` | text | Optional project scope |
| `embedding` | vector(1536) | Auto-generated |
| `created_at` | timestamptz | Auto-set |
| `updated_at` | timestamptz | Auto-set |

## Network Access

Access is restricted to:

| Network | CIDR |
|---------|------|
| LAN | `192.168.1.0/24` |
| Tailscale | `100.72.222.0/24`, `100.87.233.84` |
| WireGuard | `10.0.0.0/24` |

## Additional Documentation

See [docs/readme.md](docs/readme.md) for the full agent reference including PostgREST API examples, semantic search, and the embedder service details.
