# OpenBrain — Agent Reference

OpenBrain is a shared knowledge base for the Strecker development projects. It stores structured knowledge entries, project metadata, and shared resources, with vector embeddings for semantic search. Agents can read and write to it via two interfaces: the MCP gateway or the PostgREST API.

---

## Base URL

```
https://brain.streckercm.com
```

Access is restricted to LAN (`192.168.1.0/24`), Tailscale (`100.72.222.0/24`, `100.87.233.84`), and WireGuard (`10.0.0.0/24`).

---

## MCP Gateway

The MCP gateway exposes the full PostgreSQL database as a Streamable HTTP MCP endpoint using `mcp-server-postgres`.

**Endpoint:** `https://brain.streckercm.com/mcp/`
> Trailing slash is required.

**Transport:** Streamable HTTP (not SSE)

### Connecting (Claude Code `mcp.json`)

```json
{
  "mcpServers": {
    "openbrain": {
      "command": "node",
      "args": [
        "/path/to/supergateway/dist/index.js",
        "--streamableHttp",
        "https://brain.streckercm.com/mcp/"
      ]
    }
  }
}
```

### Available MCP Tools

The MCP server wraps PostgreSQL and exposes these tools:

| Tool | Description |
|------|-------------|
| `query` | Execute any SQL query (SELECT, INSERT, UPDATE, DELETE) |
| `list_tables` | List all tables in the database |
| `describe_table` | Get the schema for a specific table |

The `query` tool is the primary interface. Use it for all reads and writes.

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

**Known project names:** `DownholePro`, `GeoMagSharp`, `XactSpot-MSA`

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
| `resource_type` | text | Category (e.g. `constants`, `algorithms`, `error-models`, `cross-references`, `library-status`, `geomagnetic-models`) |
| `name` | text | Short name |
| `description` | text | Full content/description |
| `url` | text | Optional reference URL |
| `projects` | text[] | Which projects this applies to (empty = all) |
| `metadata` | jsonb | Flexible extra data |
| `embedding` | vector(1536) | Auto-generated semantic embedding |
| `created_at` | timestamptz | Auto-set on insert |
| `updated_at` | timestamptz | Auto-set on insert |

---

## Semantic Search

The `search_knowledge` function performs cosine similarity search using pre-computed embeddings. Embeddings use OpenAI `text-embedding-ada-002` (1536 dimensions).

**Via MCP `query` tool:**

```sql
SELECT * FROM search_knowledge(
    query_embedding := '<your_1536_dim_vector>',
    match_count := 10,
    filter_project := 'DownholePro'   -- optional, NULL searches all projects
);
```

**Returns:** `id`, `project`, `category`, `title`, `content`, `tags`, `similarity` (0–1, higher = more similar)

> In practice, most agents should use the MCP `query` tool with plain SQL `SELECT` statements filtered by `project`, `category`, or `tags` rather than vector search, unless semantic similarity across the full corpus is specifically needed.

---

## Common SQL Patterns (via MCP `query` tool)

```sql
-- List all entries for a project
SELECT id, title, category, tags FROM knowledge WHERE project = 'DownholePro' ORDER BY category, title;

-- Full text search in content
SELECT id, title, content FROM knowledge WHERE content ILIKE '%ISCWSA%';

-- Get all shared resources for a project
SELECT name, resource_type, description FROM shared_resources WHERE 'DownholePro' = ANY(projects) OR projects = '{}';

-- Insert a new knowledge entry
INSERT INTO knowledge (project, category, title, content, tags)
VALUES ('DownholePro', 'algorithms', 'Title Here', 'Content here...', ARRAY['tag1','tag2']);

-- Update content and trigger re-embedding
UPDATE knowledge SET content = 'New content...', updated_at = NOW() WHERE id = 42;
```

---

## Embedder Service

A background Python service polls the database every 30 seconds and generates embeddings for any `knowledge` or `shared_resources` rows where `embedding IS NULL`. Uses OpenAI `text-embedding-ada-002`. After inserting records, embeddings will be available within ~30 seconds.

---

## Adminer (Database UI)

A web-based database browser is available at:

```
https://brain.streckercm.com
```

Login:
- **System:** PostgreSQL
- **Server:** `db`
- **Username:** `openbrain`
- **Password:** `openbrain-db-2026`
- **Database:** `openbrain`
