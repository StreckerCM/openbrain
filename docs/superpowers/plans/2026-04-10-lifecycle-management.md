# Lifecycle Management & Schema Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add archive/lifecycle management to OpenBrain with a proper junction table for project↔entity relationships, replacing the denormalized shared_resources table.

**Architecture:** Update the PostgreSQL schema (`init.sql`) to add status columns, a `project_links` junction table, and a `general` system project. Write a one-time migration SQL script to backfill data. Rewrite `mcp-gateway/server.py` tools to use the junction table and support archive/unarchive operations. Update the embedder to stop watching the dropped table.

**Tech Stack:** PostgreSQL 17 + pgvector, Python (FastMCP + asyncpg), Docker Compose

**Spec:** `docs/superpowers/specs/2026-04-10-lifecycle-management-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `init.sql` | Modify | Updated schema: new columns, project_links table, updated search functions, seed data |
| `migrate.sql` | Create | One-time migration: backfill project_links, migrate shared_resources, drop old table |
| `mcp-gateway/server.py` | Modify | All 18 MCP tools (9 modified, 8 new, 1 new, 3 removed) |
| `embedder/embed.py` | Modify | Remove shared_resources from TABLES list |
| `docker-compose.yml` | Modify | Add ORPHAN_POLICY env var to mcp-gateway service |

---

### Task 1: Update init.sql — Add columns and project_links table

This task modifies the schema DDL that runs on startup. Since `init.sql` uses `IF NOT EXISTS` and `CREATE OR REPLACE`, it is safe to re-run.

**Files:**
- Modify: `init.sql`

- [ ] **Step 1: Add status and url columns to existing tables**

Replace the knowledge table definition in `init.sql` (lines 5-15) with:

```sql
-- Project knowledge base table
CREATE TABLE IF NOT EXISTS knowledge (
    id SERIAL PRIMARY KEY,
    project TEXT NOT NULL DEFAULT 'general',
    category TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    url TEXT,
    tags TEXT[] DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

Replace the projects table definition (lines 32-41) with:

```sql
-- Project registry
CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    repo_url TEXT,
    tech_stack TEXT[] DEFAULT '{}',
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    orphan_policy TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

Replace the memories table definition (lines 87-97) with:

```sql
-- Memories table (persistent agent memory)
CREATE TABLE IF NOT EXISTS memories (
    id SERIAL PRIMARY KEY,
    memory_type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    project TEXT DEFAULT 'general',
    status TEXT NOT NULL DEFAULT 'active',
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

- [ ] **Step 2: Add project_links table and indexes**

Add after the memories indexes section (after line 103):

```sql
-- Project links junction table
CREATE TABLE IF NOT EXISTS project_links (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id),
    knowledge_id INT REFERENCES knowledge(id),
    memory_id INT REFERENCES memories(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    CONSTRAINT exactly_one_entity CHECK (
        (knowledge_id IS NOT NULL AND memory_id IS NULL) OR
        (knowledge_id IS NULL AND memory_id IS NOT NULL)
    )
);

-- Partial unique indexes to prevent duplicate active links
CREATE UNIQUE INDEX IF NOT EXISTS project_links_knowledge_unique
    ON project_links (project_id, knowledge_id)
    WHERE knowledge_id IS NOT NULL AND status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS project_links_memory_unique
    ON project_links (project_id, memory_id)
    WHERE memory_id IS NOT NULL AND status = 'active';

-- Query indexes
CREATE INDEX IF NOT EXISTS project_links_project_idx ON project_links (project_id);
CREATE INDEX IF NOT EXISTS project_links_knowledge_idx ON project_links (knowledge_id) WHERE knowledge_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_links_memory_idx ON project_links (memory_id) WHERE memory_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_links_status_idx ON project_links (status);
```

- [ ] **Step 3: Add general project seed data**

Add after the project_links section:

```sql
-- Seed the system 'general' project
INSERT INTO projects (name, description, status)
VALUES ('general', 'Default project for non-project-specific knowledge and memories', 'system')
ON CONFLICT (name) DO NOTHING;
```

- [ ] **Step 4: Add status index on knowledge and memories**

Add after the existing knowledge indexes (after line 49):

```sql
CREATE INDEX IF NOT EXISTS knowledge_status_idx ON knowledge (status);
CREATE INDEX IF NOT EXISTS knowledge_url_idx ON knowledge (url) WHERE url IS NOT NULL;
```

Add after the existing memories indexes (after line 103):

```sql
CREATE INDEX IF NOT EXISTS memories_status_idx ON memories (status);
```

- [ ] **Step 5: Update search_knowledge function**

Replace the `search_knowledge` function with:

```sql
CREATE OR REPLACE FUNCTION search_knowledge(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    filter_project TEXT DEFAULT NULL,
    filter_status TEXT DEFAULT 'active'
)
RETURNS TABLE (
    id INT,
    project TEXT,
    category TEXT,
    title TEXT,
    content TEXT,
    url TEXT,
    tags TEXT[],
    status TEXT,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF filter_project IS NOT NULL THEN
        RETURN QUERY
        SELECT
            k.id, k.project, k.category, k.title, k.content, k.url, k.tags, k.status,
            1 - (k.embedding <=> query_embedding) AS similarity
        FROM knowledge k
        JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
        JOIN projects p ON p.id = pl.project_id AND p.name = filter_project
        WHERE k.status = filter_status
          AND k.embedding IS NOT NULL
        ORDER BY k.embedding <=> query_embedding
        LIMIT match_count;
    ELSE
        RETURN QUERY
        SELECT
            k.id, k.project, k.category, k.title, k.content, k.url, k.tags, k.status,
            1 - (k.embedding <=> query_embedding) AS similarity
        FROM knowledge k
        WHERE k.status = filter_status
          AND k.embedding IS NOT NULL
        ORDER BY k.embedding <=> query_embedding
        LIMIT match_count;
    END IF;
END;
$$;
```

- [ ] **Step 6: Update search_memories function**

Replace the `search_memories` function with:

```sql
CREATE OR REPLACE FUNCTION search_memories(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    filter_type TEXT DEFAULT NULL,
    filter_project TEXT DEFAULT NULL,
    filter_status TEXT DEFAULT 'active'
)
RETURNS TABLE (
    id INT,
    memory_type TEXT,
    name TEXT,
    description TEXT,
    content TEXT,
    project TEXT,
    status TEXT,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF filter_project IS NOT NULL THEN
        RETURN QUERY
        SELECT
            m.id, m.memory_type, m.name, m.description, m.content, m.project, m.status,
            1 - (m.embedding <=> query_embedding) AS similarity
        FROM memories m
        JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
        JOIN projects p ON p.id = pl.project_id AND p.name = filter_project
        WHERE (filter_type IS NULL OR m.memory_type = filter_type)
          AND m.status = filter_status
          AND m.embedding IS NOT NULL
        ORDER BY m.embedding <=> query_embedding
        LIMIT match_count;
    ELSE
        RETURN QUERY
        SELECT
            m.id, m.memory_type, m.name, m.description, m.content, m.project, m.status,
            1 - (m.embedding <=> query_embedding) AS similarity
        FROM memories m
        WHERE (filter_type IS NULL OR m.memory_type = filter_type)
          AND m.status = filter_status
          AND m.embedding IS NOT NULL
        ORDER BY m.embedding <=> query_embedding
        LIMIT match_count;
    END IF;
END;
$$;
```

- [ ] **Step 7: Remove shared_resources table and search function from init.sql**

Remove the `shared_resources` table definition (lines 17-29), its indexes (line 45), and the `search_shared_resources` function (lines 142-179) from `init.sql`. These will only exist in existing databases and will be cleaned up by the migration script.

- [ ] **Step 8: Commit**

```bash
git add init.sql
git commit -m "feat: update schema with status columns, project_links table, remove shared_resources DDL"
```

---

### Task 2: Create migration script

This script handles backfilling existing data into the new structure. It is designed to run once against a live database that was created with the old schema. It must be idempotent.

**Files:**
- Create: `migrate.sql`

- [ ] **Step 1: Write the migration script**

Create `migrate.sql`:

```sql
-- ============================================================
-- OpenBrain Migration: Lifecycle Management & Schema Refactor
-- Run once against an existing database after deploying the
-- updated init.sql. Safe to re-run (idempotent).
-- ============================================================

BEGIN;

-- ----------------------------------------------------------
-- Step 1: Add new columns to existing tables (idempotent)
-- ----------------------------------------------------------

-- knowledge: add url and status
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

-- memories: add status
ALTER TABLE memories ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

-- projects: add status and orphan_policy
ALTER TABLE projects ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS orphan_policy TEXT;

-- Add indexes for new columns
CREATE INDEX IF NOT EXISTS knowledge_status_idx ON knowledge (status);
CREATE INDEX IF NOT EXISTS knowledge_url_idx ON knowledge (url) WHERE url IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_status_idx ON memories (status);

-- ----------------------------------------------------------
-- Step 2: Create project_links table
-- ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS project_links (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id),
    knowledge_id INT REFERENCES knowledge(id),
    memory_id INT REFERENCES memories(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    CONSTRAINT exactly_one_entity CHECK (
        (knowledge_id IS NOT NULL AND memory_id IS NULL) OR
        (knowledge_id IS NULL AND memory_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS project_links_knowledge_unique
    ON project_links (project_id, knowledge_id)
    WHERE knowledge_id IS NOT NULL AND status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS project_links_memory_unique
    ON project_links (project_id, memory_id)
    WHERE memory_id IS NOT NULL AND status = 'active';

CREATE INDEX IF NOT EXISTS project_links_project_idx ON project_links (project_id);
CREATE INDEX IF NOT EXISTS project_links_knowledge_idx ON project_links (knowledge_id) WHERE knowledge_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_links_memory_idx ON project_links (memory_id) WHERE memory_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS project_links_status_idx ON project_links (status);

-- ----------------------------------------------------------
-- Step 3: Seed the 'general' system project
-- ----------------------------------------------------------

INSERT INTO projects (name, description, status)
VALUES ('general', 'Default project for non-project-specific knowledge and memories', 'system')
ON CONFLICT (name) DO NOTHING;

-- ----------------------------------------------------------
-- Step 4: Backfill project_links from knowledge.project
-- ----------------------------------------------------------

-- Ensure all referenced projects exist
INSERT INTO projects (name, status)
SELECT DISTINCT k.project, 'active'
FROM knowledge k
WHERE k.project IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.name = k.project)
ON CONFLICT (name) DO NOTHING;

-- Create links for knowledge entries with a project
INSERT INTO project_links (project_id, knowledge_id, status)
SELECT p.id, k.id, 'active'
FROM knowledge k
JOIN projects p ON p.name = k.project
WHERE k.project IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.knowledge_id = k.id
  );

-- Link knowledge entries without a project to 'general'
INSERT INTO project_links (project_id, knowledge_id, status)
SELECT p.id, k.id, 'active'
FROM knowledge k
CROSS JOIN projects p
WHERE p.name = 'general'
  AND (k.project IS NULL OR k.project = '')
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.knowledge_id = k.id
  );

-- Set provenance default for entries without a project
UPDATE knowledge SET project = 'general' WHERE project IS NULL OR project = '';

-- ----------------------------------------------------------
-- Step 5: Backfill project_links from memories.project
-- ----------------------------------------------------------

-- Ensure all referenced projects exist
INSERT INTO projects (name, status)
SELECT DISTINCT m.project, 'active'
FROM memories m
WHERE m.project IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.name = m.project)
ON CONFLICT (name) DO NOTHING;

-- Create links for memories with a project
INSERT INTO project_links (project_id, memory_id, status)
SELECT p.id, m.id, 'active'
FROM memories m
JOIN projects p ON p.name = m.project
WHERE m.project IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.memory_id = m.id
  );

-- Link memories without a project to 'general'
INSERT INTO project_links (project_id, memory_id, status)
SELECT p.id, m.id, 'active'
FROM memories m
CROSS JOIN projects p
WHERE p.name = 'general'
  AND (m.project IS NULL OR m.project = '')
  AND NOT EXISTS (
      SELECT 1 FROM project_links pl
      WHERE pl.project_id = p.id AND pl.memory_id = m.id
  );

-- Set provenance default for entries without a project
UPDATE memories SET project = 'general' WHERE project IS NULL OR project = '';

-- ----------------------------------------------------------
-- Step 6: Migrate shared_resources → knowledge + project_links
-- ----------------------------------------------------------

-- Only run if shared_resources table exists
DO $$
DECLARE
    sr RECORD;
    new_knowledge_id INT;
    proj_id INT;
    content_text TEXT;
    proj_name TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'shared_resources') THEN
        RAISE NOTICE 'shared_resources table does not exist, skipping migration';
        RETURN;
    END IF;

    FOR sr IN SELECT * FROM shared_resources LOOP
        -- Build content from description + metadata
        content_text := COALESCE(sr.description, sr.name);
        IF sr.metadata IS NOT NULL AND sr.metadata::text != '{}' THEN
            content_text := content_text || E'\n\nMetadata:\n' || jsonb_pretty(sr.metadata);
        END IF;

        -- Check if this resource was already migrated (by matching name + category)
        SELECT k.id INTO new_knowledge_id
        FROM knowledge k
        WHERE k.title = sr.name AND k.category = sr.resource_type
        LIMIT 1;

        IF new_knowledge_id IS NULL THEN
            -- Insert as knowledge entry
            INSERT INTO knowledge (project, category, title, content, url, status)
            VALUES ('general', sr.resource_type, sr.name, content_text, sr.url, 'active')
            RETURNING id INTO new_knowledge_id;
        END IF;

        -- Create project_links for each project in the array
        IF sr.projects IS NOT NULL THEN
            FOREACH proj_name IN ARRAY sr.projects LOOP
                -- Ensure the project exists
                INSERT INTO projects (name, status)
                VALUES (proj_name, 'active')
                ON CONFLICT (name) DO NOTHING;

                SELECT p.id INTO proj_id FROM projects p WHERE p.name = proj_name;

                -- Create the link if it doesn't exist
                INSERT INTO project_links (project_id, knowledge_id, status)
                VALUES (proj_id, new_knowledge_id, 'active')
                ON CONFLICT DO NOTHING;
            END LOOP;
        ELSE
            -- No projects specified, link to general
            SELECT p.id INTO proj_id FROM projects p WHERE p.name = 'general';
            INSERT INTO project_links (project_id, knowledge_id, status)
            VALUES (proj_id, new_knowledge_id, 'active')
            ON CONFLICT DO NOTHING;
        END IF;
    END LOOP;
END $$;

-- ----------------------------------------------------------
-- Step 7: Drop shared_resources table and related objects
-- ----------------------------------------------------------

DROP FUNCTION IF EXISTS search_shared_resources(vector(1536), INT, TEXT, TEXT);
DROP INDEX IF EXISTS shared_resources_embedding_idx;
DROP TABLE IF EXISTS shared_resources;

COMMIT;
```

- [ ] **Step 2: Commit**

```bash
git add migrate.sql
git commit -m "feat: add one-time migration script for lifecycle management refactor"
```

---

### Task 3: Update docker-compose.yml — Add ORPHAN_POLICY env var

**Files:**
- Modify: `docker-compose.yml:50-70`

- [ ] **Step 1: Add ORPHAN_POLICY to mcp-gateway environment**

In `docker-compose.yml`, add `ORPHAN_POLICY` to the mcp-gateway service environment block (after line 60):

```yaml
  mcp-gateway:
    build: ./mcp-gateway
    restart: unless-stopped
    env_file:
      - .env
    environment:
      DB_HOST: db
      DB_PORT: "5432"
      DB_NAME: ${POSTGRES_DB:-openbrain}
      DB_USER: ${POSTGRES_USER:-openbrain}
      DB_PASS: ${POSTGRES_PASSWORD:?err}
      ORPHAN_POLICY: ${ORPHAN_POLICY:-archive}
    volumes:
      - ./init.sql:/app/init.sql:ro
    ports:
      - "3007:3001"
    networks:
      - default
      - nginxproxymanager_default
    depends_on:
      db:
        condition: service_healthy
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add ORPHAN_POLICY env var to mcp-gateway service"
```

---

### Task 4: Rewrite server.py — Modified existing tools

This task updates all 9 existing tools that need changes. The 3 shared_resource tools are removed and the remaining tools are updated to use the junction table and status filtering.

**Files:**
- Modify: `mcp-gateway/server.py`

- [ ] **Step 1: Add ORPHAN_POLICY env var**

Add after the `SCHEMA_FILE` line (line 18):

```python
ORPHAN_POLICY = os.environ.get("ORPHAN_POLICY", "archive")
```

- [ ] **Step 2: Update add_knowledge tool**

Replace the existing `add_knowledge` function (lines 130-155) with:

```python
@mcp.tool()
async def add_knowledge(
    title: str,
    content: str,
    project: str = "general",
    category: str = "general",
    tags: list[str] | None = None,
    url: str | None = None,
    ctx: Context = None,
) -> str:
    """Add a knowledge entry to the OpenBrain knowledge base.

    Args:
        title: Entry title
        content: Entry content
        project: Project name for provenance and initial link (default: "general")
        category: Category (default: "general")
        tags: Optional tags for filtering
        url: Optional URL (e.g. repo link, docs page)
    """
    app = _get_app_ctx(ctx)
    async with app.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO knowledge (project, category, title, content, url, tags)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   RETURNING id, project, category, title, url, created_at""",
                project, category, title, content, url, tags or [],
            )
            # Auto-create project_link
            proj = await conn.fetchrow(
                "SELECT id FROM projects WHERE name = $1", project
            )
            if proj is None:
                proj = await conn.fetchrow(
                    """INSERT INTO projects (name, status)
                       VALUES ($1, 'active') RETURNING id""",
                    project,
                )
            await conn.execute(
                """INSERT INTO project_links (project_id, knowledge_id, status)
                   VALUES ($1, $2, 'active')
                   ON CONFLICT DO NOTHING""",
                proj["id"], row["id"],
            )
    return _format_rows([row])
```

- [ ] **Step 3: Update search_knowledge tool**

Replace the existing `search_knowledge` function (lines 158-201) with:

```python
@mcp.tool()
async def search_knowledge(
    query: str,
    project: str | None = None,
    category: str | None = None,
    include_archived: bool = False,
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Search the knowledge base using semantic similarity or text matching.

    Args:
        query: Search query text
        project: Filter to knowledge linked to a specific project (via project_links)
        category: Filter to a specific category
        include_archived: Include archived entries (default: false)
        limit: Max results (default: 10)
    """
    app = _get_app_ctx(ctx)
    status_filter = None if include_archived else "active"
    embedding = await get_embedding(app.http, query)

    if embedding is not None:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        if project is not None:
            rows = await app.pool.fetch(
                """SELECT DISTINCT k.id, k.project, k.category, k.title, k.content,
                          k.url, k.tags, k.status,
                          1 - (k.embedding <=> $1::vector) AS similarity
                   FROM knowledge k
                   JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
                   JOIN projects p ON p.id = pl.project_id AND p.name = $2
                   WHERE ($3::text IS NULL OR k.status = $3)
                     AND ($4::text IS NULL OR k.category = $4)
                     AND k.embedding IS NOT NULL
                   ORDER BY k.embedding <=> $1::vector
                   LIMIT $5""",
                embedding_str, project, status_filter, category, limit,
            )
        else:
            rows = await app.pool.fetch(
                """SELECT k.id, k.project, k.category, k.title, k.content,
                          k.url, k.tags, k.status,
                          1 - (k.embedding <=> $1::vector) AS similarity
                   FROM knowledge k
                   WHERE ($2::text IS NULL OR k.status = $2)
                     AND ($3::text IS NULL OR k.category = $3)
                     AND k.embedding IS NOT NULL
                   ORDER BY k.embedding <=> $1::vector
                   LIMIT $4""",
                embedding_str, status_filter, category, limit,
            )
    else:
        if project is not None:
            rows = await app.pool.fetch(
                """SELECT DISTINCT k.id, k.project, k.category, k.title, k.content,
                          k.url, k.tags, k.status
                   FROM knowledge k
                   JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
                   JOIN projects p ON p.id = pl.project_id AND p.name = $1
                   WHERE ($2::text IS NULL OR k.status = $2)
                     AND ($3::text IS NULL OR k.category = $3)
                     AND (k.title ILIKE '%%' || $4 || '%%' OR k.content ILIKE '%%' || $4 || '%%')
                   ORDER BY k.updated_at DESC
                   LIMIT $5""",
                project, status_filter, category, query, limit,
            )
        else:
            rows = await app.pool.fetch(
                """SELECT k.id, k.project, k.category, k.title, k.content,
                          k.url, k.tags, k.status
                   FROM knowledge k
                   WHERE ($1::text IS NULL OR k.status = $1)
                     AND ($2::text IS NULL OR k.category = $2)
                     AND (k.title ILIKE '%%' || $3 || '%%' OR k.content ILIKE '%%' || $3 || '%%')
                   ORDER BY k.updated_at DESC
                   LIMIT $4""",
                status_filter, category, query, limit,
            )
    return _format_rows(rows)
```

- [ ] **Step 4: Update list_knowledge tool**

Replace the existing `list_knowledge` function (lines 204-231) with:

```python
@mcp.tool()
async def list_knowledge(
    project: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    include_archived: bool = False,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """Browse and filter knowledge entries.

    Args:
        project: Filter to knowledge linked to a specific project
        category: Filter by category
        tags: Filter by any matching tag
        include_archived: Include archived entries (default: false)
        limit: Max results (default: 20)
    """
    app = _get_app_ctx(ctx)
    status_filter = None if include_archived else "active"

    if project is not None:
        rows = await app.pool.fetch(
            """SELECT DISTINCT k.id, k.project, k.category, k.title, k.content,
                      k.url, k.tags, k.status, k.updated_at
               FROM knowledge k
               JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
               JOIN projects p ON p.id = pl.project_id AND p.name = $1
               WHERE ($2::text IS NULL OR k.status = $2)
                 AND ($3::text IS NULL OR k.category = $3)
                 AND ($4::text[] IS NULL OR k.tags && $4)
               ORDER BY k.updated_at DESC
               LIMIT $5""",
            project, status_filter, category, tags, limit,
        )
    else:
        rows = await app.pool.fetch(
            """SELECT k.id, k.project, k.category, k.title, k.content,
                      k.url, k.tags, k.status, k.updated_at
               FROM knowledge k
               WHERE ($1::text IS NULL OR k.status = $1)
                 AND ($2::text IS NULL OR k.category = $2)
                 AND ($3::text[] IS NULL OR k.tags && $3)
               ORDER BY k.updated_at DESC
               LIMIT $4""",
            status_filter, category, tags, limit,
        )
    return _format_rows(rows)
```

- [ ] **Step 5: Remove shared_resource tools**

Delete the entire `# --- Shared resources tools ---` section (lines 234-338): `add_shared_resource`, `search_shared_resources`, and `list_shared_resources`.

- [ ] **Step 6: Update add_project tool**

Replace the existing `add_project` function (lines 344-369) with:

```python
@mcp.tool()
async def add_project(
    name: str,
    description: str | None = None,
    repo_url: str | None = None,
    tech_stack: list[str] | None = None,
    notes: str | None = None,
    orphan_policy: str | None = None,
    ctx: Context = None,
) -> str:
    """Register a new project in the knowledge base.

    Args:
        name: Project name (must be unique)
        description: Project description
        repo_url: Repository URL
        tech_stack: Technologies used
        notes: Additional notes
        orphan_policy: Orphan handling when project is archived: "archive" or "reassign" (default: uses global ORPHAN_POLICY env var)
    """
    if orphan_policy is not None and orphan_policy not in ("archive", "reassign"):
        return json.dumps({"error": "orphan_policy must be 'archive' or 'reassign'"})
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """INSERT INTO projects (name, description, repo_url, tech_stack, notes, orphan_policy)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id, name, status, orphan_policy, created_at""",
        name, description, repo_url, tech_stack or [], notes, orphan_policy,
    )
    return _format_rows([row])
```

- [ ] **Step 7: Update list_projects tool**

Replace the existing `list_projects` function (lines 372-390) with:

```python
@mcp.tool()
async def list_projects(
    tech: str | None = None,
    include_archived: bool = False,
    ctx: Context = None,
) -> str:
    """List all registered projects.

    Args:
        tech: Filter to projects using a specific technology
        include_archived: Include archived projects (default: false)
    """
    app = _get_app_ctx(ctx)
    if include_archived:
        rows = await app.pool.fetch(
            """SELECT id, name, description, tech_stack, status, orphan_policy
               FROM projects
               WHERE ($1::text IS NULL OR $1 = ANY(tech_stack))
               ORDER BY name""",
            tech,
        )
    else:
        rows = await app.pool.fetch(
            """SELECT id, name, description, tech_stack, status, orphan_policy
               FROM projects
               WHERE status IN ('active', 'system')
                 AND ($1::text IS NULL OR $1 = ANY(tech_stack))
               ORDER BY name""",
            tech,
        )
    return _format_rows(rows)
```

- [ ] **Step 8: Update get_project tool**

Replace the existing `get_project` function (lines 393-412) with:

```python
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
        """SELECT id, name, description, repo_url, tech_stack, notes,
                  status, orphan_policy, created_at, updated_at
           FROM projects
           WHERE name = $1""",
        name,
    )
    if row is None:
        return json.dumps({"error": f"Project '{name}' not found"})
    return _format_rows([row])
```

- [ ] **Step 9: Update save_memory tool**

Replace the existing `save_memory` function (lines 421-448) with:

```python
@mcp.tool()
async def save_memory(
    memory_type: str,
    name: str,
    content: str,
    description: str | None = None,
    project: str = "general",
    ctx: Context = None,
) -> str:
    """Store a persistent memory for future recall.

    Args:
        memory_type: One of: "user", "feedback", "project", "reference"
        name: Short name for the memory
        content: Memory content
        description: One-line description for relevance matching
        project: Project name for provenance and initial link (default: "general")
    """
    if memory_type not in VALID_MEMORY_TYPES:
        return json.dumps({"error": f"memory_type must be one of: {', '.join(sorted(VALID_MEMORY_TYPES))}"})
    app = _get_app_ctx(ctx)
    async with app.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO memories (memory_type, name, content, description, project)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING id, memory_type, name, created_at""",
                memory_type, name, content, description, project,
            )
            # Auto-create project_link
            proj = await conn.fetchrow(
                "SELECT id FROM projects WHERE name = $1", project
            )
            if proj is None:
                proj = await conn.fetchrow(
                    """INSERT INTO projects (name, status)
                       VALUES ($1, 'active') RETURNING id""",
                    project,
                )
            await conn.execute(
                """INSERT INTO project_links (project_id, memory_id, status)
                   VALUES ($1, $2, 'active')
                   ON CONFLICT DO NOTHING""",
                proj["id"], row["id"],
            )
    return _format_rows([row])
```

- [ ] **Step 10: Update recall_memory tool**

Replace the existing `recall_memory` function (lines 451-494) with:

```python
@mcp.tool()
async def recall_memory(
    query: str,
    memory_type: str | None = None,
    project: str | None = None,
    include_archived: bool = False,
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Search memories using semantic similarity or text matching.

    Args:
        query: What to search for
        memory_type: Filter by type (user, feedback, project, reference)
        project: Filter to memories linked to a specific project
        include_archived: Include archived memories (default: false)
        limit: Max results (default: 10)
    """
    app = _get_app_ctx(ctx)
    status_filter = None if include_archived else "active"
    embedding = await get_embedding(app.http, query)

    if embedding is not None:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        if project is not None:
            rows = await app.pool.fetch(
                """SELECT DISTINCT m.id, m.memory_type, m.name, m.description,
                          m.content, m.project, m.status,
                          1 - (m.embedding <=> $1::vector) AS similarity
                   FROM memories m
                   JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
                   JOIN projects p ON p.id = pl.project_id AND p.name = $2
                   WHERE ($3::text IS NULL OR m.memory_type = $3)
                     AND ($4::text IS NULL OR m.status = $4)
                     AND m.embedding IS NOT NULL
                   ORDER BY m.embedding <=> $1::vector
                   LIMIT $5""",
                embedding_str, project, memory_type, status_filter, limit,
            )
        else:
            rows = await app.pool.fetch(
                """SELECT m.id, m.memory_type, m.name, m.description,
                          m.content, m.project, m.status,
                          1 - (m.embedding <=> $1::vector) AS similarity
                   FROM memories m
                   WHERE ($2::text IS NULL OR m.memory_type = $2)
                     AND ($3::text IS NULL OR m.status = $3)
                     AND m.embedding IS NOT NULL
                   ORDER BY m.embedding <=> $1::vector
                   LIMIT $4""",
                embedding_str, memory_type, status_filter, limit,
            )
    else:
        if project is not None:
            rows = await app.pool.fetch(
                """SELECT DISTINCT m.id, m.memory_type, m.name, m.description,
                          m.content, m.project, m.status
                   FROM memories m
                   JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
                   JOIN projects p ON p.id = pl.project_id AND p.name = $1
                   WHERE ($2::text IS NULL OR m.memory_type = $2)
                     AND ($3::text IS NULL OR m.status = $3)
                     AND (m.name ILIKE '%%' || $4 || '%%' OR m.content ILIKE '%%' || $4 || '%%')
                   ORDER BY m.updated_at DESC
                   LIMIT $5""",
                project, memory_type, status_filter, query, limit,
            )
        else:
            rows = await app.pool.fetch(
                """SELECT m.id, m.memory_type, m.name, m.description,
                          m.content, m.project, m.status
                   FROM memories m
                   WHERE ($1::text IS NULL OR m.memory_type = $1)
                     AND ($2::text IS NULL OR m.status = $2)
                     AND (m.name ILIKE '%%' || $3 || '%%' OR m.content ILIKE '%%' || $3 || '%%')
                   ORDER BY m.updated_at DESC
                   LIMIT $4""",
                memory_type, status_filter, query, limit,
            )
    return _format_rows(rows)
```

- [ ] **Step 11: Update list_memories tool**

Replace the existing `list_memories` function (lines 497-521) with:

```python
@mcp.tool()
async def list_memories(
    memory_type: str | None = None,
    project: str | None = None,
    include_archived: bool = False,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """Browse and filter stored memories.

    Args:
        memory_type: Filter by type (user, feedback, project, reference)
        project: Filter to memories linked to a specific project
        include_archived: Include archived memories (default: false)
        limit: Max results (default: 20)
    """
    app = _get_app_ctx(ctx)
    status_filter = None if include_archived else "active"

    if project is not None:
        rows = await app.pool.fetch(
            """SELECT DISTINCT m.id, m.memory_type, m.name, m.description,
                      m.content, m.project, m.status, m.updated_at
               FROM memories m
               JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
               JOIN projects p ON p.id = pl.project_id AND p.name = $1
               WHERE ($2::text IS NULL OR m.memory_type = $2)
                 AND ($3::text IS NULL OR m.status = $3)
               ORDER BY m.updated_at DESC
               LIMIT $4""",
            project, memory_type, status_filter, limit,
        )
    else:
        rows = await app.pool.fetch(
            """SELECT m.id, m.memory_type, m.name, m.description,
                      m.content, m.project, m.status, m.updated_at
               FROM memories m
               WHERE ($1::text IS NULL OR m.memory_type = $1)
                 AND ($2::text IS NULL OR m.status = $2)
               ORDER BY m.updated_at DESC
               LIMIT $3""",
            memory_type, status_filter, limit,
        )
    return _format_rows(rows)
```

- [ ] **Step 12: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: update existing MCP tools for lifecycle management and junction table"
```

---

### Task 5: Add new tools to server.py — update_project

**Files:**
- Modify: `mcp-gateway/server.py`

- [ ] **Step 1: Add update_project tool**

Add after the `get_project` function:

```python
@mcp.tool()
async def update_project(
    name: str,
    description: str | None = None,
    repo_url: str | None = None,
    tech_stack: list[str] | None = None,
    notes: str | None = None,
    orphan_policy: str | None = None,
    ctx: Context = None,
) -> str:
    """Update an existing project's details. Only provided fields are changed.

    Args:
        name: Project name (lookup key, cannot be changed)
        description: New description
        repo_url: New repository URL
        tech_stack: New tech stack list
        notes: New notes
        orphan_policy: Orphan handling: "archive" or "reassign"
    """
    if orphan_policy is not None and orphan_policy not in ("archive", "reassign"):
        return json.dumps({"error": "orphan_policy must be 'archive' or 'reassign'"})
    app = _get_app_ctx(ctx)
    # Build dynamic UPDATE
    sets = []
    params = []
    idx = 1
    for col, val in [
        ("description", description),
        ("repo_url", repo_url),
        ("tech_stack", tech_stack),
        ("notes", notes),
        ("orphan_policy", orphan_policy),
    ]:
        if val is not None:
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
    if not sets:
        return json.dumps({"error": "No fields to update"})
    sets.append(f"updated_at = NOW()")
    params.append(name)
    query = f"""UPDATE projects SET {', '.join(sets)}
                WHERE name = ${idx}
                RETURNING id, name, description, repo_url, tech_stack, notes,
                          status, orphan_policy, updated_at"""
    row = await app.pool.fetchrow(query, *params)
    if row is None:
        return json.dumps({"error": f"Project '{name}' not found"})
    return _format_rows([row])
```

- [ ] **Step 2: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add update_project MCP tool"
```

---

### Task 6: Add new tools to server.py — archive and unarchive tools

**Files:**
- Modify: `mcp-gateway/server.py`

- [ ] **Step 1: Add archive_knowledge tool**

Add a new section after the list_knowledge tool:

```python
# --- Archive tools ---


@mcp.tool()
async def archive_knowledge(
    id: int,
    ctx: Context = None,
) -> str:
    """Archive a knowledge entry. Sets it and all its project links to archived.

    Args:
        id: Knowledge entry ID
    """
    app = _get_app_ctx(ctx)
    async with app.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE knowledge SET status = 'archived', updated_at = NOW()
                   WHERE id = $1 AND status = 'active'
                   RETURNING id, title, status""",
                id,
            )
            if row is None:
                return json.dumps({"error": f"Knowledge entry {id} not found or already archived"})
            await conn.execute(
                """UPDATE project_links SET status = 'archived', archived_at = NOW()
                   WHERE knowledge_id = $1 AND status = 'active'""",
                id,
            )
    return _format_rows([row])
```

- [ ] **Step 2: Add archive_memory tool**

```python
@mcp.tool()
async def archive_memory(
    id: int,
    ctx: Context = None,
) -> str:
    """Archive a memory. Sets it and all its project links to archived.

    Args:
        id: Memory ID
    """
    app = _get_app_ctx(ctx)
    async with app.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE memories SET status = 'archived', updated_at = NOW()
                   WHERE id = $1 AND status = 'active'
                   RETURNING id, name, status""",
                id,
            )
            if row is None:
                return json.dumps({"error": f"Memory {id} not found or already archived"})
            await conn.execute(
                """UPDATE project_links SET status = 'archived', archived_at = NOW()
                   WHERE memory_id = $1 AND status = 'active'""",
                id,
            )
    return _format_rows([row])
```

- [ ] **Step 3: Add archive_project tool with orphan handling**

```python
@mcp.tool()
async def archive_project(
    name: str,
    ctx: Context = None,
) -> str:
    """Archive a project. Cascades to all its project links and handles orphans.

    The system 'general' project cannot be archived.

    Orphan policy (per-project setting overrides ORPHAN_POLICY env var):
    - "archive": orphaned entities are archived
    - "reassign": orphaned entities are linked to the 'general' project

    Args:
        name: Project name
    """
    app = _get_app_ctx(ctx)
    async with app.pool.acquire() as conn:
        async with conn.transaction():
            # Check project exists and is not system
            proj = await conn.fetchrow(
                "SELECT id, status, orphan_policy FROM projects WHERE name = $1",
                name,
            )
            if proj is None:
                return json.dumps({"error": f"Project '{name}' not found"})
            if proj["status"] == "system":
                return json.dumps({"error": f"Cannot archive system project '{name}'"})
            if proj["status"] == "archived":
                return json.dumps({"error": f"Project '{name}' is already archived"})

            project_id = proj["id"]
            policy = proj["orphan_policy"] or ORPHAN_POLICY

            # Archive the project
            await conn.execute(
                """UPDATE projects SET status = 'archived', updated_at = NOW()
                   WHERE id = $1""",
                project_id,
            )

            # Get entities linked to this project before archiving links
            linked_knowledge = await conn.fetch(
                """SELECT knowledge_id FROM project_links
                   WHERE project_id = $1 AND knowledge_id IS NOT NULL AND status = 'active'""",
                project_id,
            )
            linked_memories = await conn.fetch(
                """SELECT memory_id FROM project_links
                   WHERE project_id = $1 AND memory_id IS NOT NULL AND status = 'active'""",
                project_id,
            )

            # Archive all links for this project
            await conn.execute(
                """UPDATE project_links SET status = 'archived', archived_at = NOW()
                   WHERE project_id = $1 AND status = 'active'""",
                project_id,
            )

            # Handle orphans
            general_id = await conn.fetchval(
                "SELECT id FROM projects WHERE name = 'general'"
            )
            orphaned_knowledge = []
            orphaned_memories = []

            for row in linked_knowledge:
                kid = row["knowledge_id"]
                remaining = await conn.fetchval(
                    """SELECT COUNT(*) FROM project_links
                       WHERE knowledge_id = $1 AND status = 'active'""",
                    kid,
                )
                if remaining == 0:
                    orphaned_knowledge.append(kid)

            for row in linked_memories:
                mid = row["memory_id"]
                remaining = await conn.fetchval(
                    """SELECT COUNT(*) FROM project_links
                       WHERE memory_id = $1 AND status = 'active'""",
                    mid,
                )
                if remaining == 0:
                    orphaned_memories.append(mid)

            if policy == "reassign":
                for kid in orphaned_knowledge:
                    await conn.execute(
                        """INSERT INTO project_links (project_id, knowledge_id, status)
                           VALUES ($1, $2, 'active')
                           ON CONFLICT DO NOTHING""",
                        general_id, kid,
                    )
                for mid in orphaned_memories:
                    await conn.execute(
                        """INSERT INTO project_links (project_id, memory_id, status)
                           VALUES ($1, $2, 'active')
                           ON CONFLICT DO NOTHING""",
                        general_id, mid,
                    )
            else:  # archive
                for kid in orphaned_knowledge:
                    await conn.execute(
                        """UPDATE knowledge SET status = 'archived', updated_at = NOW()
                           WHERE id = $1""",
                        kid,
                    )
                for mid in orphaned_memories:
                    await conn.execute(
                        """UPDATE memories SET status = 'archived', updated_at = NOW()
                           WHERE id = $1""",
                        mid,
                    )

    result = {
        "archived_project": name,
        "orphan_policy": policy,
        "orphaned_knowledge": len(orphaned_knowledge),
        "orphaned_memories": len(orphaned_memories),
    }
    return json.dumps(result, indent=2)
```

- [ ] **Step 4: Add unarchive_knowledge tool**

```python
@mcp.tool()
async def unarchive_knowledge(
    id: int,
    ctx: Context = None,
) -> str:
    """Restore an archived knowledge entry to active. Project links are NOT
    automatically restored — use link_to_project to re-associate.

    Args:
        id: Knowledge entry ID
    """
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """UPDATE knowledge SET status = 'active', updated_at = NOW()
           WHERE id = $1 AND status = 'archived'
           RETURNING id, title, status""",
        id,
    )
    if row is None:
        return json.dumps({"error": f"Knowledge entry {id} not found or not archived"})
    return _format_rows([row])
```

- [ ] **Step 5: Add unarchive_memory tool**

```python
@mcp.tool()
async def unarchive_memory(
    id: int,
    ctx: Context = None,
) -> str:
    """Restore an archived memory to active. Project links are NOT
    automatically restored — use link_to_project to re-associate.

    Args:
        id: Memory ID
    """
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """UPDATE memories SET status = 'active', updated_at = NOW()
           WHERE id = $1 AND status = 'archived'
           RETURNING id, name, status""",
        id,
    )
    if row is None:
        return json.dumps({"error": f"Memory {id} not found or not archived"})
    return _format_rows([row])
```

- [ ] **Step 6: Add unarchive_project tool**

```python
@mcp.tool()
async def unarchive_project(
    name: str,
    ctx: Context = None,
) -> str:
    """Restore an archived project to active. Project links are NOT
    automatically restored — re-link entities manually.

    Args:
        name: Project name
    """
    app = _get_app_ctx(ctx)
    row = await app.pool.fetchrow(
        """UPDATE projects SET status = 'active', updated_at = NOW()
           WHERE name = $1 AND status = 'archived'
           RETURNING id, name, status""",
        name,
    )
    if row is None:
        return json.dumps({"error": f"Project '{name}' not found or not archived"})
    return _format_rows([row])
```

- [ ] **Step 7: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add archive and unarchive MCP tools with orphan handling"
```

---

### Task 7: Add new tools to server.py — link management tools

**Files:**
- Modify: `mcp-gateway/server.py`

- [ ] **Step 1: Add link_to_project tool**

Add a new section after the archive tools:

```python
# --- Link management tools ---


@mcp.tool()
async def link_to_project(
    project: str,
    knowledge_id: int | None = None,
    memory_id: int | None = None,
    ctx: Context = None,
) -> str:
    """Associate a knowledge entry or memory with a project.

    Args:
        project: Project name to link to
        knowledge_id: Knowledge entry ID (provide exactly one of knowledge_id or memory_id)
        memory_id: Memory ID (provide exactly one of knowledge_id or memory_id)
    """
    if (knowledge_id is None) == (memory_id is None):
        return json.dumps({"error": "Provide exactly one of knowledge_id or memory_id"})

    app = _get_app_ctx(ctx)
    async with app.pool.acquire() as conn:
        async with conn.transaction():
            proj = await conn.fetchrow(
                "SELECT id FROM projects WHERE name = $1 AND status IN ('active', 'system')",
                project,
            )
            if proj is None:
                return json.dumps({"error": f"Project '{project}' not found or not active"})

            if knowledge_id is not None:
                # Verify the knowledge entry exists and is active
                exists = await conn.fetchval(
                    "SELECT id FROM knowledge WHERE id = $1 AND status = 'active'",
                    knowledge_id,
                )
                if exists is None:
                    return json.dumps({"error": f"Knowledge entry {knowledge_id} not found or not active"})
                row = await conn.fetchrow(
                    """INSERT INTO project_links (project_id, knowledge_id, status)
                       VALUES ($1, $2, 'active')
                       ON CONFLICT DO NOTHING
                       RETURNING id, project_id, knowledge_id, status""",
                    proj["id"], knowledge_id,
                )
            else:
                exists = await conn.fetchval(
                    "SELECT id FROM memories WHERE id = $1 AND status = 'active'",
                    memory_id,
                )
                if exists is None:
                    return json.dumps({"error": f"Memory {memory_id} not found or not active"})
                row = await conn.fetchrow(
                    """INSERT INTO project_links (project_id, memory_id, status)
                       VALUES ($1, $2, 'active')
                       ON CONFLICT DO NOTHING
                       RETURNING id, project_id, memory_id, status""",
                    proj["id"], memory_id,
                )

    if row is None:
        return json.dumps({"message": "Link already exists"})
    return _format_rows([row])
```

- [ ] **Step 2: Add unlink_from_project tool**

```python
@mcp.tool()
async def unlink_from_project(
    project: str,
    knowledge_id: int | None = None,
    memory_id: int | None = None,
    ctx: Context = None,
) -> str:
    """Remove the association between a knowledge entry or memory and a project.
    Archives the link — does not delete the entity itself.

    Args:
        project: Project name to unlink from
        knowledge_id: Knowledge entry ID (provide exactly one of knowledge_id or memory_id)
        memory_id: Memory ID (provide exactly one of knowledge_id or memory_id)
    """
    if (knowledge_id is None) == (memory_id is None):
        return json.dumps({"error": "Provide exactly one of knowledge_id or memory_id"})

    app = _get_app_ctx(ctx)
    proj = await app.pool.fetchrow(
        "SELECT id FROM projects WHERE name = $1", project
    )
    if proj is None:
        return json.dumps({"error": f"Project '{project}' not found"})

    if knowledge_id is not None:
        row = await app.pool.fetchrow(
            """UPDATE project_links SET status = 'archived', archived_at = NOW()
               WHERE project_id = $1 AND knowledge_id = $2 AND status = 'active'
               RETURNING id, project_id, knowledge_id, status""",
            proj["id"], knowledge_id,
        )
    else:
        row = await app.pool.fetchrow(
            """UPDATE project_links SET status = 'archived', archived_at = NOW()
               WHERE project_id = $1 AND memory_id = $2 AND status = 'active'
               RETURNING id, project_id, memory_id, status""",
            proj["id"], memory_id,
        )

    if row is None:
        return json.dumps({"error": "Active link not found"})
    return _format_rows([row])
```

- [ ] **Step 3: Commit**

```bash
git add mcp-gateway/server.py
git commit -m "feat: add link_to_project and unlink_from_project MCP tools"
```

---

### Task 8: Update embedder

**Files:**
- Modify: `embedder/embed.py:16-29`

- [ ] **Step 1: Remove shared_resources from TABLES**

Replace the `TABLES` list (lines 16-29) with:

```python
TABLES = [
    {
        "name": "knowledge",
        "text_columns": ["title", "content", "category", "project"],
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
git commit -m "feat: remove shared_resources from embedder table list"
```

---

### Task 9: Update documentation (README.md and docs/readme.md)

Both documentation files reference the old 12-tool / 4-domain structure with shared_resources. They need to reflect the new 18-tool layout, updated schema, lifecycle concepts, and the general project.

**Files:**
- Modify: `README.md`
- Modify: `docs/readme.md`

- [ ] **Step 1: Update README.md architecture section**

Replace the architecture diagram (lines 7-17) with:

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

Replace the description paragraph (lines 19-21) with:

```markdown
The **mcp-gateway** is a custom Python server built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk). It exposes 18 domain-specific tools for managing knowledge, projects, memories, lifecycle (archive/unarchive), and cross-project linking — with built-in semantic search via OpenAI embeddings.

A separate **embedder** service runs in the background, polling every 30 seconds to generate vector embeddings for any new or updated rows.
```

- [ ] **Step 2: Update README.md tool verification message**

Replace line 169:

```markdown
You should see 18 tools across five domains: knowledge, projects, memories, lifecycle, and links.
```

- [ ] **Step 3: Update README.md Available MCP Tools section**

Replace the entire "Available MCP Tools" section (lines 172-203) with:

```markdown
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
```

- [ ] **Step 4: Update README.md Database Schema section**

Replace the entire "Database Schema" section (lines 205-257) with:

```markdown
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
```

- [ ] **Step 5: Update docs/readme.md — MCP gateway description and tool count**

Replace line 19 of `docs/readme.md`:

```markdown
The MCP gateway is a custom Python server built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk). It exposes 18 domain-specific tools for managing knowledge, projects, memories, lifecycle (archive/unarchive), and cross-project linking — with built-in semantic search via OpenAI embeddings.
```

- [ ] **Step 6: Update docs/readme.md — Available MCP Tools section**

Replace the entire "Available MCP Tools" subsection (lines 48-81) with:

```markdown
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
```

- [ ] **Step 7: Update docs/readme.md — PostgREST API section**

Replace the shared_resources PostgREST example (lines 110-111):

```http
# Knowledge entries linked via project_links (query the junction table)
GET /pgapi/project_links?select=knowledge_id,knowledge(title,category)&project_id=eq.1&status=eq.active
```

- [ ] **Step 8: Update docs/readme.md — Database Schema section**

Replace the entire "Database Schema" section (lines 165-225) with the same updated schema tables as in Step 4 above (knowledge, projects, memories, project_links). Remove the `shared_resources` schema section entirely.

- [ ] **Step 9: Update docs/readme.md — Semantic Search section**

Replace line 231:

```markdown
All search tools (`search_knowledge`, `recall_memory`) use cosine similarity on pre-computed embeddings. Embeddings use OpenAI `text-embedding-3-small` (1536 dimensions).
```

- [ ] **Step 10: Update docs/readme.md — Embedder Service section**

Replace the embedder table (lines 260-266) with:

```markdown
| Table | Text columns used for embedding |
|-------|-------------------------------|
| `knowledge` | title, content, category, project |
| `memories` | name, description, content, memory_type |
```

- [ ] **Step 11: Commit**

```bash
git add README.md docs/readme.md
git commit -m "docs: update README and agent reference for lifecycle management refactor"
```

---

### Task 10: Verify deployment

- [ ] **Step 1: Rebuild containers**

```bash
docker compose build mcp-gateway embedder
```

Expected: Both images build successfully.

- [ ] **Step 2: Restart services**

```bash
docker compose up -d
```

Expected: All services start. Check logs for schema application:

```bash
docker compose logs mcp-gateway --tail=20
```

Expected output includes: `[schema] init.sql applied successfully`

- [ ] **Step 3: Run migration script**

```bash
docker compose exec -T db psql -U openbrain -d openbrain < migrate.sql
```

Expected: Migration completes without errors. Shared resources are migrated to knowledge entries with project_links.

- [ ] **Step 4: Verify general project exists**

```bash
docker compose exec db psql -U openbrain -d openbrain -c "SELECT id, name, status FROM projects WHERE name = 'general';"
```

Expected: One row with `status = 'system'`.

- [ ] **Step 5: Verify project_links were created**

```bash
docker compose exec db psql -U openbrain -d openbrain -c "SELECT COUNT(*) FROM project_links;"
```

Expected: Count matches total knowledge + memory rows.

- [ ] **Step 6: Verify shared_resources table is dropped**

```bash
docker compose exec db psql -U openbrain -d openbrain -c "SELECT COUNT(*) FROM shared_resources;" 2>&1
```

Expected: Error — `relation "shared_resources" does not exist`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore: verify lifecycle management deployment"
```
