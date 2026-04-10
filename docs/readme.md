# OpenBrain — Agent Reference

OpenBrain is a shared knowledge base for the Strecker development projects. It stores structured knowledge entries, project metadata, and persistent agent memories, with vector embeddings for semantic search. Agents can read and write to it via two interfaces: the MCP gateway or the PostgREST API.

---

## Base URL

```
https://brain.streckercm.com
```

Access is restricted to LAN (`192.168.1.0/24`), Tailscale (`100.72.222.0/24`, `100.87.233.84`), and WireGuard (`10.0.0.0/24`).

---

## MCP Gateway

The MCP gateway is a custom Python server built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk). It exposes 18 domain-specific tools for managing knowledge, projects, memories, lifecycle (archive/unarchive), and cross-project linking — with built-in semantic search via OpenAI embeddings.

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

#### Knowledge (4 tools)

| Tool | Description |
|------|-------------|
| `add_knowledge` | Add a knowledge entry. Args: `title`, `content`, `project` (default: "general"), `category` (default: "general"), `tags`, `url` |
| `search_knowledge` | Semantic or text search. Args: `query`, `project`, `category`, `include_archived`, `limit` (default: 10) |
| `list_knowledge` | Browse/filter entries. Args: `project`, `category`, `tags`, `include_archived`, `limit` (default: 20) |
| `archive_knowledge` | Archive an entry + cascade links. Args: `id` |

#### Projects (5 tools)

| Tool | Description |
|------|-------------|
| `add_project` | Register a project. Args: `name`, `description`, `repo_url`, `tech_stack`, `notes`, `orphan_policy` |
| `update_project` | Update project fields. Args: `name`, `description`, `repo_url`, `tech_stack`, `notes`, `orphan_policy` |
| `list_projects` | List all projects. Args: `tech`, `include_archived` |
| `get_project` | Get full project details. Args: `name` |
| `archive_project` | Archive project + cascade links + orphan handling. Args: `name` |

#### Memories (4 tools)

| Tool | Description |
|------|-------------|
| `save_memory` | Store a persistent memory. Args: `memory_type` (user/feedback/project/reference), `name`, `content`, `description`, `project` (default: "general") |
| `recall_memory` | Semantic or text search. Args: `query`, `memory_type`, `project`, `include_archived`, `limit` (default: 10) |
| `list_memories` | Browse/filter memories. Args: `memory_type`, `project`, `include_archived`, `limit` (default: 20) |
| `archive_memory` | Archive a memory + cascade links. Args: `id` |

#### Lifecycle (3 tools)

| Tool | Description |
|------|-------------|
| `unarchive_knowledge` | Restore archived knowledge to active. Args: `id` |
| `unarchive_memory` | Restore archived memory to active. Args: `id` |
| `unarchive_project` | Restore archived project to active. Args: `name` |

#### Links (2 tools)

| Tool | Description |
|------|-------------|
| `link_to_project` | Associate an entity with a project. Args: `project`, `knowledge_id` or `memory_id` |
| `unlink_from_project` | Remove association (archives the link). Args: `project`, `knowledge_id` or `memory_id` |

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

# Knowledge entries linked to a project via project_links
GET /pgapi/project_links?select=knowledge_id,knowledge(title,category)&project_id=eq.1&status=eq.active

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

---

## Semantic Search

All search tools (`search_knowledge`, `recall_memory`) use cosine similarity on pre-computed embeddings. Embeddings use OpenAI `text-embedding-3-small` (1536 dimensions).

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

A background Python service polls the database every 30 seconds and generates embeddings for rows where `embedding IS NULL`. It processes two tables:

| Table | Text columns used for embedding |
|-------|-------------------------------|
| `knowledge` | title, content, category, project |
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
