# Custom MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the generic mcp-server-postgres gateway with a purpose-built Python MCP server exposing 12 domain-specific tools for knowledge, shared resources, projects, and agent memory.

**Architecture:** Single-file Python MCP server (`server.py`) using the `mcp` SDK's FastMCP with Streamable HTTP transport. Uses `asyncpg` for Postgres and `httpx` for OpenAI embeddings. Runs in Docker on port 3001, proxied by Nginx at `brain.streckercm.com/mcp/`.

**Tech Stack:** Python 3.12, mcp SDK (FastMCP), asyncpg, httpx, PostgreSQL 17 + pgvector, Docker

**Spec:** `docs/superpowers/specs/2026-04-07-custom-mcp-server-design.md`

---

### Task 1: Database schema — memories table and search functions

**Files:**
- Modify: `init.sql:84` (append after existing `search_knowledge` function)

- [ ] **Step 1: Add memories table, indexes, and search functions to init.sql**

Append the following after the existing `search_knowledge` function (after line 84):

```sql
-- Memories table (persistent agent memory)
CREATE TABLE IF NOT EXISTS memories (
    id SERIAL PRIMARY KEY,
    memory_type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    project TEXT,
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for memories
CREATE INDEX IF NOT EXISTS memories_type_idx ON memories (memory_type);
CREATE INDEX IF NOT EXISTS memories_project_idx ON memories (project);
CREATE INDEX IF NOT EXISTS memories_embedding_idx ON memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Semantic search function for memories
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

-- Semantic search function for shared resources
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

- [ ] **Step 2: Commit**

```bash
git add init.sql
git commit -m "feat: add memories table and search functions for memories + shared_resources"
```

---

### Task 2: Update embedder to process memories

**Files:**
- Modify: `embedder/embed.py:16-25` (add memories to TABLES list)

- [ ] **Step 1: Add memories table config to TABLES**

In `embedder/embed.py`, add a new entry to the `TABLES` list (after line 24):

```python
TABLES = [
    {
        "name": "knowledge",
        "text_columns": ["title", "content", "category", "project"],
    },
    {
        "name": "shared_resources",
        "text_columns": ["name", "description", "resource_type"],
    },
    {
        "name": "memories",
        "text_columns": ["name", "description", "content", "memory_type"],
    },
]
```

- [ ] **Step 2: Commit**

```bash
git add embedder/embed.py
git commit -m "feat: add memories table to embedder polling config"
```

---

### Task 3: MCP server — core setup, DB pool, and embedding helper

**Files:**
- Create: `mcp-gateway/server.py`

This task creates the server skeleton with the database connection pool (via `asyncpg`) and the reusable `get_embedding()` helper that all search tools will use.

- [ ] **Step 1: Create server.py with core setup**

Create `mcp-gateway/server.py` with this content:

```python
import json
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg
import httpx
from mcp.server.fastmcp import FastMCP, Context

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "openbrain")
DB_USER = os.environ.get("DB_USER", "openbrain")
DB_PASS = os.environ.get("DB_PASS", "openbrain-db-2026")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass
class AppContext:
    pool: asyncpg.Pool
    http: httpx.AsyncClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=10,
    )
    async with httpx.AsyncClient() as http:
        try:
            yield AppContext(pool=pool, http=http)
        finally:
            await pool.close()


mcp = FastMCP(
    "openbrain",
    host="0.0.0.0",
    port=3001,
    streamable_http_path="/mcp",
    stateless_http=True,
    lifespan=app_lifespan,
)


def _get_app_ctx(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


async def get_embedding(http: httpx.AsyncClient, text: str) -> list[float] | None:
    """Generate an embedding via the OpenAI API. Returns None if unavailable."""
    if not OPENAI_API_KEY:
        return None
    try:
        resp = await http.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"input": text, "model": EMBEDDING_MODEL},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception:
        return None


def _format_rows(rows: list[asyncpg.Record]) -> str:
    """Convert asyncpg Records to a JSON string for tool responses."""
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# --- Tools are registered below this line ---


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

- [ ] **Step 2: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: MCP server core — FastMCP setup, DB pool, embedding helper"
```

---

### Task 4: MCP server — knowledge tools

**Files:**
- Modify: `mcp-gateway/server.py` (add tools before the `if __name__` block)

- [ ] **Step 1: Add add_knowledge tool**

Insert before the `if __name__ == "__main__":` line in `server.py`:

```python
@mcp.tool()
async def add_knowledge(
    project: str,
    title: str,
    content: str,
    category: str = "general",
    tags: list[str] | None = None,
    ctx: Context = None,
) -> str:
    """Add a knowledge entry to the OpenBrain knowledge base.

    Args:
        project: Project name (e.g. "DownholePro")
        title: Entry title
        content: Entry content
        category: Category (default: "general")
        tags: Optional tags for filtering
    """
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """INSERT INTO knowledge (project, category, title, content, tags)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING id, project, category, title, created_at""",
        project, category, title, content, tags or [],
    )
    return _format_rows([row])


@mcp.tool()
async def search_knowledge(
    query: str,
    project: str | None = None,
    category: str | None = None,
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Search the knowledge base using semantic similarity or text matching.

    Args:
        query: Search query text
        project: Filter to a specific project
        category: Filter to a specific category
        limit: Max results (default: 10)
    """
    app = _get_app_ctx(ctx)
    embedding = await get_embedding(app.http, query)

    if embedding is not None:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        rows = await app.pool.fetch(
            """SELECT id, project, category, title, content, tags,
                      1 - (embedding <=> $1::vector) AS similarity
               FROM knowledge
               WHERE ($2::text IS NULL OR project = $2)
                 AND ($3::text IS NULL OR category = $3)
                 AND embedding IS NOT NULL
               ORDER BY embedding <=> $1::vector
               LIMIT $4""",
            embedding_str, project, category, limit,
        )
    else:
        rows = await app.pool.fetch(
            """SELECT id, project, category, title, content, tags
               FROM knowledge
               WHERE ($1::text IS NULL OR project = $1)
                 AND ($2::text IS NULL OR category = $2)
                 AND (title ILIKE '%' || $3 || '%' OR content ILIKE '%' || $3 || '%')
               ORDER BY updated_at DESC
               LIMIT $4""",
            project, category, query, limit,
        )
    return _format_rows(rows)


@mcp.tool()
async def list_knowledge(
    project: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """Browse and filter knowledge entries.

    Args:
        project: Filter by project
        category: Filter by category
        tags: Filter by any matching tag
        limit: Max results (default: 20)
    """
    app = _get_app_ctx(ctx)
    rows = await app.pool.fetch(
        """SELECT id, project, category, title, content, tags, updated_at
           FROM knowledge
           WHERE ($1::text IS NULL OR project = $1)
             AND ($2::text IS NULL OR category = $2)
             AND ($3::text[] IS NULL OR tags && $3)
           ORDER BY updated_at DESC
           LIMIT $4""",
        project, category, tags, limit,
    )
    return _format_rows(rows)
```

- [ ] **Step 2: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add knowledge tools — add, search, list"
```

---

### Task 5: MCP server — shared resources tools

**Files:**
- Modify: `mcp-gateway/server.py` (add tools before the `if __name__` block)

- [ ] **Step 1: Add shared resources tools**

Insert after the knowledge tools, before `if __name__ == "__main__":`:

```python
@mcp.tool()
async def add_shared_resource(
    resource_type: str,
    name: str,
    description: str | None = None,
    url: str | None = None,
    projects: list[str] | None = None,
    metadata: dict | None = None,
    ctx: Context = None,
) -> str:
    """Add a shared resource to the knowledge base.

    Args:
        resource_type: Type (e.g. "library", "service", "tool")
        name: Resource name
        description: Resource description
        url: Resource URL
        projects: Associated project names
        metadata: Arbitrary JSON metadata
    """
    app = _get_app_ctx(ctx)
    meta_json = json.dumps(metadata) if metadata else "{}"
    row = await app.pool.fetchrow(
        """INSERT INTO shared_resources (resource_type, name, description, url, projects, metadata)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb)
           RETURNING id, resource_type, name, created_at""",
        resource_type, name, description, url, projects or [], meta_json,
    )
    return _format_rows([row])


@mcp.tool()
async def search_shared_resources(
    query: str,
    resource_type: str | None = None,
    project: str | None = None,
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Search shared resources using semantic similarity or text matching.

    Args:
        query: Search query text
        resource_type: Filter by type
        project: Filter to resources associated with a project
        limit: Max results (default: 10)
    """
    app = _get_app_ctx(ctx)
    embedding = await get_embedding(app.http, query)

    if embedding is not None:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        rows = await app.pool.fetch(
            """SELECT id, resource_type, name, description, url, projects, metadata,
                      1 - (embedding <=> $1::vector) AS similarity
               FROM shared_resources
               WHERE ($2::text IS NULL OR resource_type = $2)
                 AND ($3::text IS NULL OR $3 = ANY(projects))
                 AND embedding IS NOT NULL
               ORDER BY embedding <=> $1::vector
               LIMIT $4""",
            embedding_str, resource_type, project, limit,
        )
    else:
        rows = await app.pool.fetch(
            """SELECT id, resource_type, name, description, url, projects, metadata
               FROM shared_resources
               WHERE ($1::text IS NULL OR resource_type = $1)
                 AND ($2::text IS NULL OR $2 = ANY(projects))
                 AND (name ILIKE '%' || $3 || '%' OR description ILIKE '%' || $3 || '%')
               ORDER BY updated_at DESC
               LIMIT $4""",
            resource_type, project, query, limit,
        )
    return _format_rows(rows)


@mcp.tool()
async def list_shared_resources(
    resource_type: str | None = None,
    project: str | None = None,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """Browse and filter shared resources.

    Args:
        resource_type: Filter by type
        project: Filter to resources associated with a project
        limit: Max results (default: 20)
    """
    app = _get_app_ctx(ctx)
    rows = await app.pool.fetch(
        """SELECT id, resource_type, name, description, url, projects, metadata, updated_at
           FROM shared_resources
           WHERE ($1::text IS NULL OR resource_type = $1)
             AND ($2::text IS NULL OR $2 = ANY(projects))
           ORDER BY updated_at DESC
           LIMIT $3""",
        resource_type, project, limit,
    )
    return _format_rows(rows)
```

- [ ] **Step 2: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add shared resources tools — add, search, list"
```

---

### Task 6: MCP server — project tools

**Files:**
- Modify: `mcp-gateway/server.py` (add tools before the `if __name__` block)

- [ ] **Step 1: Add project tools**

Insert after the shared resources tools, before `if __name__ == "__main__":`:

```python
@mcp.tool()
async def add_project(
    name: str,
    description: str | None = None,
    repo_url: str | None = None,
    tech_stack: list[str] | None = None,
    notes: str | None = None,
    ctx: Context = None,
) -> str:
    """Register a new project in the knowledge base.

    Args:
        name: Project name (must be unique)
        description: Project description
        repo_url: Repository URL
        tech_stack: Technologies used
        notes: Additional notes
    """
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """INSERT INTO projects (name, description, repo_url, tech_stack, notes)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING id, name, created_at""",
        name, description, repo_url, tech_stack or [], notes,
    )
    return _format_rows([row])


@mcp.tool()
async def list_projects(
    tech: str | None = None,
    ctx: Context = None,
) -> str:
    """List all registered projects.

    Args:
        tech: Filter to projects using a specific technology
    """
    app = _get_app_ctx(ctx)
    rows = await app.pool.fetch(
        """SELECT id, name, description, tech_stack
           FROM projects
           WHERE ($1::text IS NULL OR $1 = ANY(tech_stack))
           ORDER BY name""",
        tech,
    )
    return _format_rows(rows)


@mcp.tool()
async def get_project(
    name: str,
    ctx: Context = None,
) -> str:
    """Get full details for a specific project.

    Args:
        name: Project name
    """
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """SELECT id, name, description, repo_url, tech_stack, notes, created_at, updated_at
           FROM projects
           WHERE name = $1""",
        name,
    )
    if row is None:
        return json.dumps({"error": f"Project '{name}' not found"})
    return _format_rows([row])
```

- [ ] **Step 2: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add project tools — add, list, get"
```

---

### Task 7: MCP server — memory tools

**Files:**
- Modify: `mcp-gateway/server.py` (add tools before the `if __name__` block)

- [ ] **Step 1: Add memory tools**

Insert after the project tools, before `if __name__ == "__main__":`:

```python
VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}


@mcp.tool()
async def save_memory(
    memory_type: str,
    name: str,
    content: str,
    description: str | None = None,
    project: str | None = None,
    ctx: Context = None,
) -> str:
    """Store a persistent memory for future recall.

    Args:
        memory_type: One of: "user", "feedback", "project", "reference"
        name: Short name for the memory
        content: Memory content
        description: One-line description for relevance matching
        project: Project scope (omit for global)
    """
    if memory_type not in VALID_MEMORY_TYPES:
        return json.dumps({"error": f"memory_type must be one of: {', '.join(sorted(VALID_MEMORY_TYPES))}"})
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """INSERT INTO memories (memory_type, name, content, description, project)
           VALUES ($1, $2, $3, $4, $5)
           RETURNING id, memory_type, name, created_at""",
        memory_type, name, content, description, project,
    )
    return _format_rows([row])


@mcp.tool()
async def recall_memory(
    query: str,
    memory_type: str | None = None,
    project: str | None = None,
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Search memories using semantic similarity or text matching.

    Args:
        query: What to search for
        memory_type: Filter by type (user, feedback, project, reference)
        project: Filter by project scope
        limit: Max results (default: 10)
    """
    app = _get_app_ctx(ctx)
    embedding = await get_embedding(app.http, query)

    if embedding is not None:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        rows = await app.pool.fetch(
            """SELECT id, memory_type, name, description, content, project,
                      1 - (embedding <=> $1::vector) AS similarity
               FROM memories
               WHERE ($2::text IS NULL OR memory_type = $2)
                 AND ($3::text IS NULL OR project = $3)
                 AND embedding IS NOT NULL
               ORDER BY embedding <=> $1::vector
               LIMIT $4""",
            embedding_str, memory_type, project, limit,
        )
    else:
        rows = await app.pool.fetch(
            """SELECT id, memory_type, name, description, content, project
               FROM memories
               WHERE ($1::text IS NULL OR memory_type = $1)
                 AND ($2::text IS NULL OR project = $2)
                 AND (name ILIKE '%' || $3 || '%' OR content ILIKE '%' || $3 || '%')
               ORDER BY updated_at DESC
               LIMIT $4""",
            memory_type, project, query, limit,
        )
    return _format_rows(rows)


@mcp.tool()
async def list_memories(
    memory_type: str | None = None,
    project: str | None = None,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """Browse and filter stored memories.

    Args:
        memory_type: Filter by type (user, feedback, project, reference)
        project: Filter by project scope
        limit: Max results (default: 20)
    """
    app = _get_app_ctx(ctx)
    rows = await app.pool.fetch(
        """SELECT id, memory_type, name, description, content, project, updated_at
           FROM memories
           WHERE ($1::text IS NULL OR memory_type = $1)
             AND ($2::text IS NULL OR project = $2)
           ORDER BY updated_at DESC
           LIMIT $3""",
        memory_type, project, limit,
    )
    return _format_rows(rows)
```

- [ ] **Step 2: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add memory tools — save, recall, list"
```

---

### Task 8: Container setup — Dockerfile and requirements

**Files:**
- Replace: `mcp-gateway/Dockerfile`
- Create: `mcp-gateway/requirements.txt`

- [ ] **Step 1: Replace Dockerfile**

Replace the entire contents of `mcp-gateway/Dockerfile` with:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 3001
CMD ["python", "server.py"]
```

- [ ] **Step 2: Create requirements.txt**

Create `mcp-gateway/requirements.txt`:

```
mcp[http]
asyncpg
httpx
```

- [ ] **Step 3: Commit**

```bash
git add mcp-gateway/Dockerfile mcp-gateway/requirements.txt
git commit -m "feat: replace Node mcp-gateway with Python container"
```

---

### Task 9: Update docker-compose.yml

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Update mcp-gateway service**

In `docker-compose.yml`, replace the `mcp-gateway` service block (lines 48-57) with:

```yaml
  mcp-gateway:
    build: ./mcp-gateway
    restart: unless-stopped
    env_file:
      - .env
    environment:
      DB_HOST: db
      DB_PORT: "5432"
      DB_NAME: openbrain
      DB_USER: openbrain
      DB_PASS: openbrain-db-2026
    ports:
      - "3007:3001"
    networks:
      - default
      - nginxproxymanager_default
    depends_on:
      db:
        condition: service_healthy
```

- [ ] **Step 2: Move Adminer off default port**

In `docker-compose.yml`, update the `adminer` service block (lines 38-45) to add an explicit port mapping:

```yaml
  adminer:
    image: adminer:latest
    restart: unless-stopped
    ports:
      - "3008:8080"
    networks:
      - default
      - nginxproxymanager_default
    depends_on:
      db:
        condition: service_healthy
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: update docker-compose for Python mcp-gateway, move Adminer to port 3008"
```

---

### Task 10: Build and smoke test

- [ ] **Step 1: Rebuild containers**

```bash
cd /path/to/openbrain
docker compose build mcp-gateway
```

Expected: successful build with Python 3.12 image, pip installs mcp, asyncpg, httpx.

- [ ] **Step 2: Bring up the stack**

```bash
docker compose up -d
```

Expected: all services start. Check `mcp-gateway` logs:

```bash
docker compose logs mcp-gateway --tail 20
```

Expected: server starts on port 3001 with no errors.

- [ ] **Step 3: Verify MCP endpoint responds**

```bash
curl -X POST http://localhost:3007/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

Expected: JSON response with server info and capabilities.

- [ ] **Step 4: Verify tools are listed**

```bash
curl -X POST http://localhost:3007/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Expected: JSON response listing all 12 tools: `add_knowledge`, `search_knowledge`, `list_knowledge`, `add_shared_resource`, `search_shared_resources`, `list_shared_resources`, `add_project`, `list_projects`, `get_project`, `save_memory`, `recall_memory`, `list_memories`.

- [ ] **Step 5: Smoke test — add and retrieve a knowledge entry**

```bash
curl -X POST http://localhost:3007/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add_knowledge","arguments":{"project":"test","title":"Smoke test","content":"This is a smoke test entry"}}}'
```

Expected: JSON response with the new record's id, project, category, title, and created_at.

Then list it:

```bash
curl -X POST http://localhost:3007/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_knowledge","arguments":{"project":"test"}}}'
```

Expected: JSON response containing the smoke test entry.

- [ ] **Step 6: Verify from Claude Code**

In Claude Code, ensure the `openbrain` MCP server connects and all 12 tools are available. Test a simple `list_projects` call.

- [ ] **Step 7: Clean up smoke test data**

If the smoke test entry should be removed, use Adminer at `http://localhost:3008` or connect to the DB directly:

```bash
docker compose exec db psql -U openbrain -c "DELETE FROM knowledge WHERE project = 'test' AND title = 'Smoke test';"
```

- [ ] **Step 8: Commit any fixes**

If any fixes were needed during smoke testing, commit them:

```bash
git add -A
git commit -m "fix: smoke test adjustments for MCP server"
```
