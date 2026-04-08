# OpenBrain Custom MCP Server

**Date:** 2026-04-07
**Status:** Draft
**Replaces:** `mcp-gateway` (Node + supergateway + mcp-server-postgres)

## Summary

Replace the generic SQL-passthrough MCP gateway with a purpose-built Python MCP server that exposes domain-specific tools for knowledge management, shared resources, project registry, and persistent agent memory. The server handles Streamable HTTP directly (no supergateway dependency) and supports both semantic and text-based search.

## Motivation

The current `mcp-gateway` wraps `mcp-server-postgres`, exposing a generic `query` tool that accepts raw SQL. This has three problems:

1. **Agents must know the exact schema** to compose correct INSERT/SELECT statements
2. **No validation** — malformed data, wrong types, or destructive queries are all possible
3. **Poor discoverability** — a single `query` tool gives agents no guidance on what operations are available

A custom server with named tools (`add_knowledge`, `search_knowledge`, etc.) is self-documenting, validates inputs, and prevents destructive operations.

## Architecture

### Transport

The Python `mcp` SDK serves Streamable HTTP directly on port 3001 (path `/mcp/`). No supergateway or Node.js dependency. Claude Code connects via the existing Nginx reverse proxy at `https://brain.streckercm.com/mcp/`.

### Container

The `mcp-gateway` Docker service is replaced in-place. Same service name, same port mapping (3007:3001), but built from a Python image instead of Node.

### Dependencies

| Package | Purpose |
|---------|---------|
| `mcp[http]` | MCP SDK with Streamable HTTP transport |
| `asyncpg` | Async PostgreSQL driver |
| `httpx` | HTTP client for OpenAI embeddings API |

## Database Changes

### New table: `memories`

```sql
CREATE TABLE IF NOT EXISTS memories (
    id SERIAL PRIMARY KEY,
    memory_type TEXT NOT NULL,        -- 'user', 'feedback', 'project', 'reference'
    name TEXT NOT NULL,
    description TEXT,                  -- short summary for relevance matching
    content TEXT NOT NULL,
    project TEXT,                      -- optional project scope (NULL = global)
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### New indexes

```sql
CREATE INDEX IF NOT EXISTS memories_type_idx ON memories (memory_type);
CREATE INDEX IF NOT EXISTS memories_project_idx ON memories (project);
CREATE INDEX IF NOT EXISTS memories_embedding_idx ON memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### New function: `search_memories`

```sql
CREATE OR REPLACE FUNCTION search_memories(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    filter_type TEXT DEFAULT NULL,
    filter_project TEXT DEFAULT NULL
)
RETURNS TABLE (
    id INT,
    memory_type TEXT,
    name TEXT,
    description TEXT,
    content TEXT,
    project TEXT,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        m.id,
        m.memory_type,
        m.name,
        m.description,
        m.content,
        m.project,
        1 - (m.embedding <=> query_embedding) AS similarity
    FROM memories m
    WHERE (filter_type IS NULL OR m.memory_type = filter_type)
      AND (filter_project IS NULL OR m.project = filter_project)
      AND m.embedding IS NOT NULL
    ORDER BY m.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;
```

### New function: `search_shared_resources`

```sql
CREATE OR REPLACE FUNCTION search_shared_resources(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    filter_type TEXT DEFAULT NULL,
    filter_project TEXT DEFAULT NULL
)
RETURNS TABLE (
    id INT,
    resource_type TEXT,
    name TEXT,
    description TEXT,
    url TEXT,
    projects TEXT[],
    metadata JSONB,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        r.id,
        r.resource_type,
        r.name,
        r.description,
        r.url,
        r.projects,
        r.metadata,
        1 - (r.embedding <=> query_embedding) AS similarity
    FROM shared_resources r
    WHERE (filter_type IS NULL OR r.resource_type = filter_type)
      AND (filter_project IS NULL OR filter_project = ANY(r.projects))
      AND r.embedding IS NOT NULL
    ORDER BY r.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;
```

### Embedder update

Add `memories` to the embedder's `TABLES` config:

```python
{
    "name": "memories",
    "text_columns": ["name", "description", "content", "memory_type"],
}
```

## Tools

### Knowledge (3 tools)

#### `add_knowledge`

Insert a knowledge entry. Embedding is generated asynchronously by the embedder service.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| project | string | yes | Project name (e.g., "DownholePro") |
| category | string | no | Category (default: "general") |
| title | string | yes | Entry title |
| content | string | yes | Entry content |
| tags | list[string] | no | Tags for filtering |

Returns: the created record's id, project, category, title, and created_at.

#### `search_knowledge`

Semantic search with text fallback. If `OPENAI_API_KEY` is set, generates a query embedding and uses `search_knowledge()` for cosine similarity. Falls back to `ILIKE` across title and content if the API key is missing or the call fails.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | Search query text |
| project | string | no | Filter to a specific project |
| category | string | no | Filter to a specific category |
| limit | int | no | Max results (default: 10) |

Returns: matching records with id, project, category, title, content, tags, and similarity score (when using semantic search).

#### `list_knowledge`

Browse and filter knowledge entries without free-text search. For when you know what you're looking for by structure rather than content.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| project | string | no | Filter by project |
| category | string | no | Filter by category |
| tags | list[string] | no | Filter by any matching tag |
| limit | int | no | Max results (default: 20) |

Returns: matching records ordered by `updated_at` descending.

### Shared Resources (3 tools)

#### `add_shared_resource`

Insert a shared resource entry.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| resource_type | string | yes | Type (e.g., "library", "service", "tool") |
| name | string | yes | Resource name |
| description | string | no | Description |
| url | string | no | URL |
| projects | list[string] | no | Associated projects |
| metadata | dict | no | Arbitrary JSON metadata |

Returns: the created record's id, resource_type, name, and created_at.

#### `search_shared_resources`

Semantic + text fallback search across shared resources. Same embedding/fallback pattern as `search_knowledge`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | Search query text |
| resource_type | string | no | Filter by type |
| project | string | no | Filter to resources associated with a project |
| limit | int | no | Max results (default: 10) |

Returns: matching records with similarity score.

#### `list_shared_resources`

Browse and filter shared resources.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| resource_type | string | no | Filter by type |
| project | string | no | Filter to resources associated with a project |
| limit | int | no | Max results (default: 20) |

Returns: matching records ordered by `updated_at` descending.

### Projects (3 tools)

#### `add_project`

Register a new project.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| name | string | yes | Project name (unique) |
| description | string | no | Project description |
| repo_url | string | no | Repository URL |
| tech_stack | list[string] | no | Technologies used |
| notes | string | no | Additional notes |

Returns: the created record's id, name, and created_at.

#### `list_projects`

List all registered projects.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| tech | string | no | Filter to projects using a specific technology |

Returns: all matching projects with id, name, description, tech_stack.

#### `get_project`

Get full details for a specific project.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| name | string | yes | Project name |

Returns: full project record including all fields.

### Memory (3 tools)

#### `save_memory`

Store a persistent memory. Memories are scoped by type and optionally by project.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_type | string | yes | One of: "user", "feedback", "project", "reference" |
| name | string | yes | Short name for the memory |
| content | string | yes | Memory content |
| description | string | no | One-line description for relevance matching |
| project | string | no | Project scope (NULL = global) |

Returns: the created record's id, memory_type, name, and created_at.

#### `recall_memory`

Semantic search with text fallback across memories. Same pattern as knowledge search but using `search_memories()`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | What to search for |
| memory_type | string | no | Filter by type |
| project | string | no | Filter by project scope |
| limit | int | no | Max results (default: 10) |

Returns: matching memories with similarity score.

#### `list_memories`

Browse and filter memories.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_type | string | no | Filter by type |
| project | string | no | Filter by project |
| limit | int | no | Max results (default: 20) |

Returns: matching memories ordered by `updated_at` descending.

## Search Behavior

All search tools (`search_knowledge`, `search_shared_resources`, `recall_memory`) follow the same pattern:

1. **Semantic path** (preferred): If `OPENAI_API_KEY` is set, call the OpenAI embeddings API to generate a query vector, then use the corresponding `search_*()` Postgres function for cosine similarity ranking.
2. **Text fallback**: If the API key is missing or the embedding call fails, fall back to `ILIKE '%query%'` across relevant text columns.
3. Results are always returned sorted by relevance (similarity score for semantic, most recently updated for text fallback).

## Infrastructure Changes

### docker-compose.yml

- `mcp-gateway` service: change `build: ./mcp-gateway` to build from the new Python-based directory
- Add `env_file: .env` to `mcp-gateway` for `OPENAI_API_KEY` access
- Add `DB_*` environment variables (same pattern as embedder)
- Move Adminer to a non-80 port to reserve port 80 for future web UI

### mcp-gateway/Dockerfile

Replace the Node-based Dockerfile with:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 3001
CMD ["python", "server.py"]
```

### mcp-gateway/requirements.txt

```
mcp[http]
asyncpg
httpx
```

### Claude Code connection

No change needed. Claude Code still connects via:
```json
{
  "mcpServers": {
    "openbrain": {
      "command": "npx",
      "args": ["-y", "supergateway", "--streamableHttp", "https://brain.streckercm.com/mcp/"]
    }
  }
}
```

The local supergateway client bridges stdio (Claude Code) to Streamable HTTP (the server). The server-side supergateway is eliminated, but the client-side bridge remains.

## Phase 2: Web UI (future)

A simple browser-based UI served from the same Python container for searching knowledge, browsing memories, and viewing projects. The MCP Streamable HTTP endpoint lives at `/mcp/`, leaving `/` free for static files and a lightweight API.

Not in scope for this implementation, but the design accounts for it — the server structure makes it straightforward to add routes later.

## Files to create or modify

| File | Action |
|------|--------|
| `mcp-gateway/server.py` | Create — the custom MCP server |
| `mcp-gateway/requirements.txt` | Create — Python dependencies |
| `mcp-gateway/Dockerfile` | Replace — Python-based container |
| `init.sql` | Modify — add memories table, indexes, search function |
| `embedder/embed.py` | Modify — add memories to TABLES config |
| `docker-compose.yml` | Modify — update mcp-gateway env, move Adminer port |
