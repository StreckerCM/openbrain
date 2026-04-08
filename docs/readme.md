# OpenBrain — Agent Reference

OpenBrain is a shared knowledge base for the Strecker development projects. It stores structured knowledge entries, project metadata, shared resources, and persistent agent memories, with vector embeddings for semantic search. Agents can read and write to it via two interfaces: the MCP gateway or the PostgREST API.

---

## Base URL

```
https://brain.streckercm.com
```

Access is restricted to LAN (`192.168.1.0/24`), Tailscale (`100.72.222.0/24`, `100.87.233.84`), and WireGuard (`10.0.0.0/24`).

---

## MCP Gateway

The MCP gateway is a custom Python server built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk). It exposes 12 domain-specific tools for managing knowledge, shared resources, projects, and memories — with built-in semantic search via OpenAI embeddings.

**Endpoint:** `https://brain.streckercm.com/mcp/`
> Trailing slash is required.

**Transport:** Streamable HTTP (not SSE)

### Connecting (Claude Code `mcp.json`)

Use [supergateway](https://github.com/supercorp-ai/supergateway) to bridge stdio ↔ Streamable HTTP:

```json
{
  "mcpServers": {
    "openbrain": {
      "command": "npx",
      "args": [
        "-y",
        "supergateway",
        "--streamableHttp",
        "https://brain.streckercm.com/mcp/"
      ]
    }
  }
}
```

> **Note:** Do not use `"type": "http"` — Claude Code's native HTTP transport triggers OAuth discovery, which this server does not support. Use supergateway instead.

### Available MCP Tools

#### Knowledge (3 tools)

| Tool | Description |
|------|-------------|
| `add_knowledge` | Add a knowledge entry. Args: `project`, `title`, `content`, `category` (default: "general"), `tags` |
| `search_knowledge` | Semantic or text search. Args: `query`, `project`, `category`, `limit` (default: 10) |
| `list_knowledge` | Browse/filter entries. Args: `project`, `category`, `tags`, `limit` (default: 20) |

#### Shared Resources (3 tools)

| Tool | Description |
|------|-------------|
| `add_shared_resource` | Add a cross-project resource. Args: `resource_type`, `name`, `description`, `url`, `projects`, `metadata` |
| `search_shared_resources` | Semantic or text search. Args: `query`, `resource_type`, `project`, `limit` (default: 10) |
| `list_shared_resources` | Browse/filter resources. Args: `resource_type`, `project`, `limit` (default: 20) |

#### Projects (3 tools)

| Tool | Description |
|------|-------------|
| `add_project` | Register a project. Args: `name`, `description`, `repo_url`, `tech_stack`, `notes` |
| `list_projects` | List all projects. Args: `tech` (optional tech filter) |
| `get_project` | Get full project details. Args: `name` |

#### Memories (3 tools)

| Tool | Description |
|------|-------------|
| `save_memory` | Store a persistent memory. Args: `memory_type` (user/feedback/project/reference), `name`, `content`, `description`, `project` |
| `recall_memory` | Semantic or text search. Args: `query`, `memory_type`, `project`, `limit` (default: 10) |
| `list_memories` | Browse/filter memories. Args: `memory_type`, `project`, `limit` (default: 20) |

All search tools use **semantic similarity** (cosine distance on OpenAI `text-embedding-3-small` embeddings) when the OPENAI_API_KEY is configured. If embeddings are unavailable, they fall back to text-based `ILIKE` search.

---

## PostgREST API

A REST API that exposes all tables directly.

**Base URL:** `https://brain.streckercm.com/pgapi/`

### Reading

```http
# All knowledge for a project
GET /pgapi/knowledge?project=eq.DownholePro

# Filter by category
GET /pgapi/knowledge?project=eq.DownholePro&category=eq.algorithms

# Filter by tag (contains)
GET /pgapi/knowledge?tags=cs.{ISCWSA}

# Specific record
GET /pgapi/knowledge?id=eq.42

# All projects
GET /pgapi/projects

# Shared resources for a specific project
GET /pgapi/shared_resources?projects=cs.{DownholePro}

# Memories by type
GET /pgapi/memories?memory_type=eq.feedback

# Memories scoped to a project
GET /pgapi/memories?project=eq.DownholePro

# Select specific columns
GET /pgapi/knowledge?project=eq.DownholePro&select=id,title,category,tags
```

### Writing

```http
# Insert a knowledge entry
POST /pgapi/knowledge
Content-Type: application/json

{
  "project": "DownholePro",
  "category": "algorithms",
  "title": "My Entry Title",
  "content": "Full content of the entry...",
  "tags": ["tag1", "tag2"]
}

# Insert a memory
POST /pgapi/memories
Content-Type: application/json

{
  "memory_type": "feedback",
  "name": "Preferred test style",
  "content": "Use integration tests, not mocks",
  "project": "DownholePro"
}

# Update an entry
PATCH /pgapi/knowledge?id=eq.42
Content-Type: application/json

{
  "content": "Updated content..."
}

# Delete an entry
DELETE /pgapi/knowledge?id=eq.42
```

> **Note:** The `embedding` field is automatically populated by the embedder service within 30 seconds of insert/update. Do not set it manually.

---

## Database Schema

### `knowledge` — Project-specific knowledge entries

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Auto-increment primary key |
| `project` | text | Project name (e.g. `DownholePro`, `GeoMagSharp`, `XactSpot-MSA`) |
| `category` | text | Entry category (e.g. `algorithms`, `constants`, `error-models`) |
| `title` | text | Short descriptive title |
| `content` | text | Full content of the entry |
| `tags` | text[] | Array of searchable tags |
| `embedding` | vector(1536) | Auto-generated semantic embedding (do not set manually) |
| `created_at` | timestamptz | Auto-set on insert |
| `updated_at` | timestamptz | Auto-set on insert |

### `projects` — Project registry

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Auto-increment primary key |
| `name` | text | Unique project name |
| `description` | text | Project description |
| `repo_url` | text | Repository URL |
| `tech_stack` | text[] | Technologies used |
| `notes` | text | Freeform notes |
| `created_at` | timestamptz | Auto-set on insert |
| `updated_at` | timestamptz | Auto-set on insert |

### `shared_resources` — Cross-project shared references

Resources used by more than one project (standards, constants, algorithms, reference documents).

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Auto-increment primary key |
| `resource_type` | text | Category (e.g. `constants`, `algorithms`, `error-models`, `library`) |
| `name` | text | Short name |
| `description` | text | Full content/description |
| `url` | text | Optional reference URL |
| `projects` | text[] | Which projects this applies to (empty = all) |
| `metadata` | jsonb | Flexible extra data |
| `embedding` | vector(1536) | Auto-generated semantic embedding |
| `created_at` | timestamptz | Auto-set on insert |
| `updated_at` | timestamptz | Auto-set on insert |

### `memories` — Persistent agent memory

Stores information agents need to recall across sessions: user preferences, feedback, project context, and reference pointers.

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Auto-increment primary key |
| `memory_type` | text | One of: `user`, `feedback`, `project`, `reference` |
| `name` | text | Short name for the memory |
| `description` | text | One-line description (used for relevance matching) |
| `content` | text | Full memory content |
| `project` | text | Optional project scope (null = global) |
| `embedding` | vector(1536) | Auto-generated semantic embedding |
| `created_at` | timestamptz | Auto-set on insert |
| `updated_at` | timestamptz | Auto-set on insert |

---

## Semantic Search

All search tools (`search_knowledge`, `search_shared_resources`, `recall_memory`) use cosine similarity on pre-computed embeddings. Embeddings use OpenAI `text-embedding-3-small` (1536 dimensions).

When using the MCP tools, semantic search is automatic — just pass a natural language query. The server generates an embedding for your query and finds the most similar entries.

**Fallback:** If the OpenAI API key is not configured or the API is unreachable, search tools fall back to text-based `ILIKE` matching.

**Via PostgREST (SQL function):**

```sql
SELECT * FROM search_knowledge(
    query_embedding := '<your_1536_dim_vector>',
    match_count := 10,
    filter_project := 'DownholePro'
);

SELECT * FROM search_memories(
    query_embedding := '<your_1536_dim_vector>',
    match_count := 10,
    filter_type := 'feedback',
    filter_project := NULL
);
```

> In practice, use the MCP tools for search — they handle embedding generation automatically. The SQL functions are for direct database access.

---

## Embedder Service

A background Python service polls the database every 30 seconds and generates embeddings for rows where `embedding IS NULL`. It processes all four tables:

| Table | Text columns used for embedding |
|-------|-------------------------------|
| `knowledge` | title, content, category, project |
| `shared_resources` | name, description, resource_type |
| `memories` | name, description, content, memory_type |

Uses OpenAI `text-embedding-3-small` (1536 dimensions). After inserting records via any method, embeddings will be available within ~30 seconds.

---

## Adminer (Database UI)

A web-based database browser is available at the adminer port (3008 by default, or via reverse proxy).

Login:
- **System:** PostgreSQL
- **Server:** `db`
- **Username:** `openbrain`
- **Password:** `openbrain-db-2026`
- **Database:** `openbrain`
