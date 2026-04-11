# OpenBrain Web UI Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full CRUD management dashboard for the OpenBrain knowledge base — browse, search, create, edit, archive, unarchive, link, and hard-delete knowledge, memories, and projects.

**Architecture:** Preact + HTM SPA (no build step) served from an Nginx container. Reads go through PostgREST, writes go through new REST endpoints in mcp-gateway. Sentry for error tracking across mcp-gateway, frontend, and embedder.

**Tech Stack:** Preact 10, HTM, Preact Signals, marked.js, Python/Starlette (mcp-gateway REST), PostgreSQL views/functions, Nginx reverse proxy, Sentry SDK

**Spec:** `docs/superpowers/specs/2026-04-11-web-ui-dashboard-design.md`

---

## File Map

### New files to create

```
web-ui/
├── Dockerfile
├── nginx.conf
└── static/
    ├── index.html
    ├── css/style.css
    ├── js/
    │   ├── app.js
    │   ├── lib/api.js
    │   ├── lib/state.js
    │   ├── lib/markdown.js
    │   ├── components/sidebar.js
    │   ├── components/toast.js
    │   ├── components/modal.js
    │   ├── components/tag-chips.js
    │   ├── components/entity-list.js
    │   ├── components/entity-form.js
    │   ├── components/markdown-editor.js
    │   ├── components/search-bar.js
    │   ├── pages/dashboard.js
    │   ├── pages/knowledge.js
    │   ├── pages/memories.js
    │   ├── pages/projects.js
    │   ├── pages/search.js
    │   └── pages/archive.js
    └── vendor/
        ├── preact.min.js
        ├── htm.min.js
        ├── preact-hooks.min.js
        ├── preact-signals.min.js
        └── marked.min.js
```

### Existing files to modify

```
mcp-gateway/server.py        — Add REST endpoints via Starlette routes alongside MCP
mcp-gateway/requirements.txt — Add sentry-sdk[starlette]
mcp-gateway/Dockerfile       — Copy init.sql (already done)
embedder/embed.py            — Add Sentry init
embedder/requirements.txt    — Add sentry-sdk
docker-compose.yml           — Add web-ui service, add SENTRY_DSN env var
init.sql                     — Add views + orphaned_items function
```

---

## Task 1: Database Views and Functions

**Files:**
- Modify: `init.sql` (append after existing functions, ~line 193)

- [ ] **Step 1: Add `knowledge_with_projects` view to `init.sql`**

Append this to the end of `init.sql`:

```sql
-- Views for web UI (PostgREST read layer)

CREATE OR REPLACE VIEW knowledge_with_projects AS
SELECT k.*,
       COALESCE(array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL), '{}') AS projects
FROM knowledge k
LEFT JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
LEFT JOIN projects p ON p.id = pl.project_id
GROUP BY k.id;
```

- [ ] **Step 2: Add `memories_with_projects` view to `init.sql`**

Append after the previous view:

```sql
CREATE OR REPLACE VIEW memories_with_projects AS
SELECT m.*,
       COALESCE(array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL), '{}') AS projects
FROM memories m
LEFT JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
LEFT JOIN projects p ON p.id = pl.project_id
GROUP BY m.id;
```

- [ ] **Step 3: Add `recent_activity` view to `init.sql`**

```sql
CREATE OR REPLACE VIEW recent_activity AS
SELECT id, 'knowledge' AS type, title AS name, category AS subtype, updated_at
FROM knowledge WHERE status = 'active'
UNION ALL
SELECT id, 'memory' AS type, name, memory_type AS subtype, updated_at
FROM memories WHERE status = 'active';
```

- [ ] **Step 4: Add `orphaned_items` function to `init.sql`**

```sql
CREATE OR REPLACE FUNCTION orphaned_items()
RETURNS TABLE(id INT, type TEXT, name TEXT, updated_at TIMESTAMPTZ) AS $$
  SELECT k.id, 'knowledge', k.title, k.updated_at
  FROM knowledge k
  LEFT JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
  WHERE k.status = 'active' AND pl.id IS NULL
  UNION ALL
  SELECT m.id, 'memory', m.name, m.updated_at
  FROM memories m
  LEFT JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
  WHERE m.status = 'active' AND pl.id IS NULL
  ORDER BY updated_at DESC;
$$ LANGUAGE sql STABLE;
```

- [ ] **Step 5: Test by applying schema to running database**

Run:
```bash
docker compose exec db psql -U openbrain -d openbrain -f /docker-entrypoint-initdb.d/init.sql
```

Then verify:
```bash
docker compose exec db psql -U openbrain -d openbrain -c "SELECT * FROM knowledge_with_projects LIMIT 2;"
docker compose exec db psql -U openbrain -d openbrain -c "SELECT * FROM memories_with_projects LIMIT 2;"
docker compose exec db psql -U openbrain -d openbrain -c "SELECT * FROM recent_activity LIMIT 5;"
docker compose exec db psql -U openbrain -d openbrain -c "SELECT * FROM orphaned_items() LIMIT 5;"
```

Expected: Each query returns rows (or empty tables with correct columns). No errors.

- [ ] **Step 6: Commit**

```bash
git add init.sql
git commit -m "feat: add PostgREST views and orphaned_items function for web UI"
```

---

## Task 2: mcp-gateway REST Endpoints

The mcp-gateway currently only serves the MCP protocol at `/mcp`. We need to add REST endpoints at `/api/` that reuse the same database logic. The FastMCP server uses Starlette internally — we'll mount a Starlette sub-app for our REST routes.

**Files:**
- Modify: `mcp-gateway/server.py` (add REST routes after existing MCP tool definitions, before `if __name__`)
- Modify: `mcp-gateway/requirements.txt` (add `sentry-sdk[starlette]`)

- [ ] **Step 1: Refactor server.py — extract shared DB functions**

The MCP tool functions are tightly coupled to the MCP `Context` object via `_get_app_ctx(ctx)`. To reuse them from REST handlers, extract the core DB logic into standalone async functions that accept a `pool` parameter.

Add these functions after the `_format_rows` helper (line ~125), before the MCP tool definitions:

```python
# --- Shared DB operations (used by both MCP tools and REST endpoints) ---


async def _db_add_knowledge(pool, title, content, project="general", category="general", tags=None, url=None):
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO knowledge (project, category, title, content, url, tags)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   RETURNING id, project, category, title, url, created_at""",
                project, category, title, content, url, tags or [],
            )
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
    return dict(row)


async def _db_update_knowledge(pool, kid, **fields):
    sets, params, idx = [], [], 1
    for col in ("title", "content", "category", "url", "tags"):
        if col in fields and fields[col] is not None:
            sets.append(f"{col} = ${idx}")
            params.append(fields[col])
            idx += 1
    if not sets:
        return None
    sets.append("updated_at = NOW()")
    params.append(kid)
    query = f"""UPDATE knowledge SET {', '.join(sets)}
                WHERE id = ${idx} AND status != 'archived'
                RETURNING id, project, category, title, url, tags, status, updated_at"""
    return await pool.fetchrow(query, *params)


async def _db_hard_delete_knowledge(pool, kid):
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, status FROM knowledge WHERE id = $1", kid
            )
            if row is None:
                return {"error": "Not found", "status": 404}
            if row["status"] != "archived":
                return {"error": "Item must be archived before deletion", "status": 400}
            await conn.execute(
                "DELETE FROM project_links WHERE knowledge_id = $1", kid
            )
            await conn.execute("DELETE FROM knowledge WHERE id = $1", kid)
    return {"deleted": kid}


async def _db_save_memory(pool, memory_type, name, content, description=None, project="general"):
    if memory_type not in VALID_MEMORY_TYPES:
        return {"error": f"memory_type must be one of: {', '.join(sorted(VALID_MEMORY_TYPES))}"}
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO memories (memory_type, name, content, description, project)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING id, memory_type, name, created_at""",
                memory_type, name, content, description, project,
            )
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
    return dict(row)


async def _db_update_memory(pool, mid, **fields):
    sets, params, idx = [], [], 1
    for col in ("name", "content", "description"):
        if col in fields and fields[col] is not None:
            sets.append(f"{col} = ${idx}")
            params.append(fields[col])
            idx += 1
    if not sets:
        return None
    sets.append("updated_at = NOW()")
    params.append(mid)
    query = f"""UPDATE memories SET {', '.join(sets)}
                WHERE id = ${idx} AND status != 'archived'
                RETURNING id, memory_type, name, description, status, updated_at"""
    return await pool.fetchrow(query, *params)


async def _db_hard_delete_memory(pool, mid):
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, status FROM memories WHERE id = $1", mid
            )
            if row is None:
                return {"error": "Not found", "status": 404}
            if row["status"] != "archived":
                return {"error": "Item must be archived before deletion", "status": 400}
            await conn.execute(
                "DELETE FROM project_links WHERE memory_id = $1", mid
            )
            await conn.execute("DELETE FROM memories WHERE id = $1", mid)
    return {"deleted": mid}


async def _db_hard_delete_project(pool, name):
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, status FROM projects WHERE name = $1", name
            )
            if row is None:
                return {"error": "Not found", "status": 404}
            if row["status"] == "system":
                return {"error": "Cannot delete system project", "status": 400}
            if row["status"] != "archived":
                return {"error": "Project must be archived before deletion", "status": 400}
            await conn.execute(
                "DELETE FROM project_links WHERE project_id = $1", row["id"]
            )
            await conn.execute("DELETE FROM projects WHERE id = $1", row["id"])
    return {"deleted": name}


async def _db_archive(pool, entity_type, entity_id):
    if entity_type == "knowledge":
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE knowledge SET status = 'archived', updated_at = NOW()
                       WHERE id = $1 AND status = 'active'
                       RETURNING id, title, status""",
                    entity_id,
                )
                if row is None:
                    return {"error": "Not found or already archived", "status": 404}
                await conn.execute(
                    """UPDATE project_links SET status = 'archived', archived_at = NOW()
                       WHERE knowledge_id = $1 AND status = 'active'""",
                    entity_id,
                )
        return dict(row)
    elif entity_type == "memory":
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE memories SET status = 'archived', updated_at = NOW()
                       WHERE id = $1 AND status = 'active'
                       RETURNING id, name, status""",
                    entity_id,
                )
                if row is None:
                    return {"error": "Not found or already archived", "status": 404}
                await conn.execute(
                    """UPDATE project_links SET status = 'archived', archived_at = NOW()
                       WHERE memory_id = $1 AND status = 'active'""",
                    entity_id,
                )
        return dict(row)
    elif entity_type == "project":
        # Delegate to existing archive_project logic (complex orphan handling)
        # We'll call the internal function directly
        return {"error": "Use archive_project tool directly", "status": 501}
    return {"error": "Invalid type", "status": 400}


async def _db_unarchive(pool, entity_type, entity_id):
    if entity_type == "knowledge":
        row = await pool.fetchrow(
            """UPDATE knowledge SET status = 'active', updated_at = NOW()
               WHERE id = $1 AND status = 'archived'
               RETURNING id, title, status""",
            entity_id,
        )
    elif entity_type == "memory":
        row = await pool.fetchrow(
            """UPDATE memories SET status = 'active', updated_at = NOW()
               WHERE id = $1 AND status = 'archived'
               RETURNING id, name, status""",
            entity_id,
        )
    elif entity_type == "project":
        row = await pool.fetchrow(
            """UPDATE projects SET status = 'active', updated_at = NOW()
               WHERE name = $1 AND status = 'archived'
               RETURNING id, name, status""",
            entity_id,
        )
    else:
        return {"error": "Invalid type", "status": 400}
    if row is None:
        return {"error": "Not found or not archived", "status": 404}
    return dict(row)


async def _db_link(pool, project_name, knowledge_id=None, memory_id=None):
    if (knowledge_id is None) == (memory_id is None):
        return {"error": "Provide exactly one of knowledge_id or memory_id", "status": 400}
    async with pool.acquire() as conn:
        async with conn.transaction():
            proj = await conn.fetchrow(
                "SELECT id FROM projects WHERE name = $1 AND status IN ('active', 'system')",
                project_name,
            )
            if proj is None:
                return {"error": f"Project '{project_name}' not found or not active", "status": 404}
            if knowledge_id is not None:
                row = await conn.fetchrow(
                    """INSERT INTO project_links (project_id, knowledge_id, status)
                       VALUES ($1, $2, 'active')
                       ON CONFLICT DO NOTHING
                       RETURNING id, project_id, knowledge_id, status""",
                    proj["id"], knowledge_id,
                )
            else:
                row = await conn.fetchrow(
                    """INSERT INTO project_links (project_id, memory_id, status)
                       VALUES ($1, $2, 'active')
                       ON CONFLICT DO NOTHING
                       RETURNING id, project_id, memory_id, status""",
                    proj["id"], memory_id,
                )
    if row is None:
        return {"message": "Link already exists"}
    return dict(row)


async def _db_unlink(pool, project_name, knowledge_id=None, memory_id=None):
    if (knowledge_id is None) == (memory_id is None):
        return {"error": "Provide exactly one of knowledge_id or memory_id", "status": 400}
    proj = await pool.fetchrow(
        "SELECT id FROM projects WHERE name = $1", project_name
    )
    if proj is None:
        return {"error": f"Project '{project_name}' not found", "status": 404}
    if knowledge_id is not None:
        row = await pool.fetchrow(
            """UPDATE project_links SET status = 'archived', archived_at = NOW()
               WHERE project_id = $1 AND knowledge_id = $2 AND status = 'active'
               RETURNING id""",
            proj["id"], knowledge_id,
        )
    else:
        row = await pool.fetchrow(
            """UPDATE project_links SET status = 'archived', archived_at = NOW()
               WHERE project_id = $1 AND memory_id = $2 AND status = 'active'
               RETURNING id""",
            proj["id"], memory_id,
        )
    if row is None:
        return {"error": "Active link not found", "status": 404}
    return {"unlinked": True}


async def _db_search(pool, http, query, mode="semantic", types=None):
    if types is None:
        types = ["knowledge", "memories"]
    results = {}

    if mode == "semantic":
        embedding = await get_embedding(http, query)
        if embedding is None:
            mode = "exact"
            results["_fallback"] = True

    if mode == "semantic":
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        if "knowledge" in types:
            rows = await pool.fetch(
                """SELECT k.id, k.project, k.category, k.title, k.content,
                          k.url, k.tags, k.status,
                          1 - (k.embedding <=> $1::vector) AS similarity
                   FROM knowledge k
                   WHERE k.status = 'active' AND k.embedding IS NOT NULL
                   ORDER BY k.embedding <=> $1::vector
                   LIMIT 20""",
                embedding_str,
            )
            results["knowledge"] = [dict(r) for r in rows]
        if "memories" in types:
            rows = await pool.fetch(
                """SELECT m.id, m.memory_type, m.name, m.description,
                          m.content, m.project, m.status,
                          1 - (m.embedding <=> $1::vector) AS similarity
                   FROM memories m
                   WHERE m.status = 'active' AND m.embedding IS NOT NULL
                   ORDER BY m.embedding <=> $1::vector
                   LIMIT 20""",
                embedding_str,
            )
            results["memories"] = [dict(r) for r in rows]
    else:
        if "knowledge" in types:
            rows = await pool.fetch(
                """SELECT k.id, k.project, k.category, k.title, k.content,
                          k.url, k.tags, k.status
                   FROM knowledge k
                   WHERE k.status = 'active'
                     AND (k.title ILIKE '%' || $1 || '%' OR k.content ILIKE '%' || $1 || '%')
                   ORDER BY k.updated_at DESC
                   LIMIT 20""",
                query,
            )
            results["knowledge"] = [dict(r) for r in rows]
        if "memories" in types:
            rows = await pool.fetch(
                """SELECT m.id, m.memory_type, m.name, m.description,
                          m.content, m.project, m.status
                   FROM memories m
                   WHERE m.status = 'active'
                     AND (m.name ILIKE '%' || $1 || '%' OR m.content ILIKE '%' || $1 || '%')
                   ORDER BY m.updated_at DESC
                   LIMIT 20""",
                query,
            )
            results["memories"] = [dict(r) for r in rows]

    return results


async def _db_bulk_delete(pool, items):
    deleted = []
    errors = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in items:
                t, eid = item["type"], item["id"]
                if t == "knowledge":
                    row = await conn.fetchrow(
                        "SELECT status FROM knowledge WHERE id = $1", eid
                    )
                    if row is None:
                        errors.append({"id": eid, "type": t, "error": "Not found"})
                        continue
                    if row["status"] != "archived":
                        errors.append({"id": eid, "type": t, "error": "Not archived"})
                        continue
                    await conn.execute("DELETE FROM project_links WHERE knowledge_id = $1", eid)
                    await conn.execute("DELETE FROM knowledge WHERE id = $1", eid)
                    deleted.append({"id": eid, "type": t})
                elif t == "memory":
                    row = await conn.fetchrow(
                        "SELECT status FROM memories WHERE id = $1", eid
                    )
                    if row is None:
                        errors.append({"id": eid, "type": t, "error": "Not found"})
                        continue
                    if row["status"] != "archived":
                        errors.append({"id": eid, "type": t, "error": "Not archived"})
                        continue
                    await conn.execute("DELETE FROM project_links WHERE memory_id = $1", eid)
                    await conn.execute("DELETE FROM memories WHERE id = $1", eid)
                    deleted.append({"id": eid, "type": t})
                elif t == "project":
                    row = await conn.fetchrow(
                        "SELECT id, status FROM projects WHERE name = $1", eid
                    )
                    if row is None:
                        errors.append({"id": eid, "type": t, "error": "Not found"})
                        continue
                    if row["status"] == "system":
                        errors.append({"id": eid, "type": t, "error": "Cannot delete system project"})
                        continue
                    if row["status"] != "archived":
                        errors.append({"id": eid, "type": t, "error": "Not archived"})
                        continue
                    await conn.execute("DELETE FROM project_links WHERE project_id = $1", row["id"])
                    await conn.execute("DELETE FROM projects WHERE id = $1", row["id"])
                    deleted.append({"id": eid, "type": t})
    return {"deleted": deleted, "errors": errors}
```

- [ ] **Step 2: Update existing MCP tools to use shared functions**

Replace the body of `add_knowledge` (the `@mcp.tool()` function) to call `_db_add_knowledge`:

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
    result = await _db_add_knowledge(app.pool, title, content, project, category, tags, url)
    return json.dumps(result, default=str, indent=2)
```

Do the same for `save_memory`:

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
    app = _get_app_ctx(ctx)
    result = await _db_save_memory(app.pool, memory_type, name, content, description, project)
    if "error" in result:
        return json.dumps(result)
    return json.dumps(result, default=str, indent=2)
```

Leave the other MCP tools (search, list, archive, unarchive, link, unlink) unchanged for now — their logic is read-heavy and the REST endpoints use the `_db_*` functions directly.

- [ ] **Step 3: Add REST endpoint routes**

Add this block before the `if __name__` section at the bottom of `server.py`:

```python
# --- REST API for web UI ---

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _json(data, status=200):
    return JSONResponse(data, status_code=status, default=str)


def _get_pool():
    """Get the DB pool from the MCP server's lifespan context.
    The pool is stored as an attribute on the module after lifespan starts."""
    return _rest_app_ctx.pool


def _get_http():
    return _rest_app_ctx.http


# Placeholder — set during lifespan
_rest_app_ctx = None


async def rest_knowledge_create(request: Request):
    body = await request.json()
    title = body.get("title")
    content = body.get("content")
    if not title or not content:
        return _json({"error": "title and content are required"}, 400)
    result = await _db_add_knowledge(
        _get_pool(), title, content,
        body.get("project", "general"),
        body.get("category", "general"),
        body.get("tags"),
        body.get("url"),
    )
    return _json(result, 201)


async def rest_knowledge_update(request: Request):
    kid = int(request.path_params["id"])
    body = await request.json()
    row = await _db_update_knowledge(_get_pool(), kid, **body)
    if row is None:
        return _json({"error": "No fields to update or item not found"}, 400)
    return _json(dict(row))


async def rest_knowledge_delete(request: Request):
    kid = int(request.path_params["id"])
    result = await _db_hard_delete_knowledge(_get_pool(), kid)
    if "error" in result:
        return _json(result, result.get("status", 400))
    return _json(result)


async def rest_memories_create(request: Request):
    body = await request.json()
    memory_type = body.get("memory_type")
    name = body.get("name")
    content = body.get("content")
    if not memory_type or not name or not content:
        return _json({"error": "memory_type, name, and content are required"}, 400)
    result = await _db_save_memory(
        _get_pool(), memory_type, name, content,
        body.get("description"),
        body.get("project", "general"),
    )
    if "error" in result:
        return _json(result, 400)
    return _json(result, 201)


async def rest_memories_update(request: Request):
    mid = int(request.path_params["id"])
    body = await request.json()
    row = await _db_update_memory(_get_pool(), mid, **body)
    if row is None:
        return _json({"error": "No fields to update or item not found"}, 400)
    return _json(dict(row))


async def rest_memories_delete(request: Request):
    mid = int(request.path_params["id"])
    result = await _db_hard_delete_memory(_get_pool(), mid)
    if "error" in result:
        return _json(result, result.get("status", 400))
    return _json(result)


async def rest_projects_create(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return _json({"error": "name is required"}, 400)
    app = _get_pool()
    orphan_policy = body.get("orphan_policy")
    if orphan_policy and orphan_policy not in ("archive", "reassign"):
        return _json({"error": "orphan_policy must be 'archive' or 'reassign'"}, 400)
    try:
        row = await app.fetchrow(
            """INSERT INTO projects (name, description, repo_url, tech_stack, notes, orphan_policy)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id, name, status, orphan_policy, created_at""",
            name, body.get("description"), body.get("repo_url"),
            body.get("tech_stack", []), body.get("notes"), orphan_policy,
        )
    except Exception:
        return _json({"error": "Project already exists"}, 409)
    return _json(dict(row), 201)


async def rest_projects_update(request: Request):
    name = request.path_params["name"]
    body = await request.json()
    sets, params, idx = [], [], 1
    for col in ("description", "repo_url", "tech_stack", "notes", "orphan_policy"):
        if col in body and body[col] is not None:
            sets.append(f"{col} = ${idx}")
            params.append(body[col])
            idx += 1
    if not sets:
        return _json({"error": "No fields to update"}, 400)
    sets.append("updated_at = NOW()")
    params.append(name)
    query = f"""UPDATE projects SET {', '.join(sets)}
                WHERE name = ${idx}
                RETURNING id, name, description, repo_url, tech_stack, notes,
                          status, orphan_policy, updated_at"""
    row = await _get_pool().fetchrow(query, *params)
    if row is None:
        return _json({"error": "Project not found"}, 404)
    return _json(dict(row))


async def rest_projects_delete(request: Request):
    name = request.path_params["name"]
    result = await _db_hard_delete_project(_get_pool(), name)
    if "error" in result:
        return _json(result, result.get("status", 400))
    return _json(result)


async def rest_archive(request: Request):
    entity_type = request.path_params["type"]
    entity_id = request.path_params["id"]
    if entity_type == "project":
        # Project archive has complex orphan handling — reuse the existing function
        pool = _get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                proj = await conn.fetchrow(
                    "SELECT id, status, orphan_policy FROM projects WHERE name = $1",
                    entity_id,
                )
                if proj is None:
                    return _json({"error": "Project not found"}, 404)
                if proj["status"] == "system":
                    return _json({"error": "Cannot archive system project"}, 400)
                if proj["status"] == "archived":
                    return _json({"error": "Already archived"}, 400)

                project_id = proj["id"]
                policy = proj["orphan_policy"] or ORPHAN_POLICY

                await conn.execute(
                    "UPDATE projects SET status = 'archived', updated_at = NOW() WHERE id = $1",
                    project_id,
                )
                linked_knowledge = await conn.fetch(
                    "SELECT knowledge_id FROM project_links WHERE project_id = $1 AND knowledge_id IS NOT NULL AND status = 'active'",
                    project_id,
                )
                linked_memories = await conn.fetch(
                    "SELECT memory_id FROM project_links WHERE project_id = $1 AND memory_id IS NOT NULL AND status = 'active'",
                    project_id,
                )
                await conn.execute(
                    "UPDATE project_links SET status = 'archived', archived_at = NOW() WHERE project_id = $1 AND status = 'active'",
                    project_id,
                )
                general_id = await conn.fetchval("SELECT id FROM projects WHERE name = 'general'")
                orphaned_k, orphaned_m = [], []
                for row in linked_knowledge:
                    kid = row["knowledge_id"]
                    remaining = await conn.fetchval(
                        "SELECT COUNT(*) FROM project_links WHERE knowledge_id = $1 AND status = 'active'", kid
                    )
                    if remaining == 0:
                        orphaned_k.append(kid)
                for row in linked_memories:
                    mid = row["memory_id"]
                    remaining = await conn.fetchval(
                        "SELECT COUNT(*) FROM project_links WHERE memory_id = $1 AND status = 'active'", mid
                    )
                    if remaining == 0:
                        orphaned_m.append(mid)
                if policy == "reassign":
                    for kid in orphaned_k:
                        await conn.execute(
                            "INSERT INTO project_links (project_id, knowledge_id, status) VALUES ($1, $2, 'active') ON CONFLICT DO NOTHING",
                            general_id, kid,
                        )
                    for mid in orphaned_m:
                        await conn.execute(
                            "INSERT INTO project_links (project_id, memory_id, status) VALUES ($1, $2, 'active') ON CONFLICT DO NOTHING",
                            general_id, mid,
                        )
                else:
                    for kid in orphaned_k:
                        await conn.execute(
                            "UPDATE knowledge SET status = 'archived', updated_at = NOW() WHERE id = $1", kid
                        )
                    for mid in orphaned_m:
                        await conn.execute(
                            "UPDATE memories SET status = 'archived', updated_at = NOW() WHERE id = $1", mid
                        )
        return _json({"archived": entity_id, "orphan_policy": policy,
                       "orphaned_knowledge": len(orphaned_k), "orphaned_memories": len(orphaned_m)})
    else:
        entity_id = int(entity_id)
        result = await _db_archive(_get_pool(), entity_type, entity_id)
        if "error" in result:
            return _json(result, result.get("status", 400))
        return _json(result)


async def rest_unarchive(request: Request):
    entity_type = request.path_params["type"]
    entity_id = request.path_params["id"]
    if entity_type != "project":
        entity_id = int(entity_id)
    result = await _db_unarchive(_get_pool(), entity_type, entity_id)
    if "error" in result:
        return _json(result, result.get("status", 400))
    return _json(result)


async def rest_link(request: Request):
    body = await request.json()
    result = await _db_link(
        _get_pool(), body.get("project"),
        body.get("knowledge_id"), body.get("memory_id"),
    )
    if "error" in result:
        return _json(result, result.get("status", 400))
    return _json(result, 201)


async def rest_unlink(request: Request):
    body = await request.json()
    result = await _db_unlink(
        _get_pool(), body.get("project"),
        body.get("knowledge_id"), body.get("memory_id"),
    )
    if "error" in result:
        return _json(result, result.get("status", 400))
    return _json(result)


async def rest_search(request: Request):
    body = await request.json()
    query = body.get("query", "")
    if not query:
        return _json({"error": "query is required"}, 400)
    mode = body.get("mode", "semantic")
    types = body.get("types", ["knowledge", "memories"])
    results = await _db_search(_get_pool(), _get_http(), query, mode, types)
    return _json({"results": results})


async def rest_bulk_delete(request: Request):
    body = await request.json()
    items = body.get("items", [])
    if not items:
        return _json({"error": "items list is required"}, 400)
    result = await _db_bulk_delete(_get_pool(), items)
    if result["errors"]:
        return _json(result, 207)
    return _json(result)


rest_routes = [
    Route("/api/knowledge", rest_knowledge_create, methods=["POST"]),
    Route("/api/knowledge/{id:int}", rest_knowledge_update, methods=["PUT"]),
    Route("/api/knowledge/{id:int}", rest_knowledge_delete, methods=["DELETE"]),
    Route("/api/memories", rest_memories_create, methods=["POST"]),
    Route("/api/memories/{id:int}", rest_memories_update, methods=["PUT"]),
    Route("/api/memories/{id:int}", rest_memories_delete, methods=["DELETE"]),
    Route("/api/projects", rest_projects_create, methods=["POST"]),
    Route("/api/projects/{name:str}", rest_projects_update, methods=["PUT"]),
    Route("/api/projects/{name:str}", rest_projects_delete, methods=["DELETE"]),
    Route("/api/archive/{type:str}/{id:str}", rest_archive, methods=["POST"]),
    Route("/api/unarchive/{type:str}/{id:str}", rest_unarchive, methods=["POST"]),
    Route("/api/link", rest_link, methods=["POST"]),
    Route("/api/link", rest_unlink, methods=["DELETE"]),
    Route("/api/search", rest_search, methods=["POST"]),
    Route("/api/bulk-delete", rest_bulk_delete, methods=["DELETE"]),
]
```

- [ ] **Step 4: Mount REST routes on the MCP server's ASGI app**

Replace the `if __name__` block at the end of `server.py`:

```python
if __name__ == "__main__":
    import asyncio
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Mount

    print("[startup] Applying database schema...", flush=True)
    asyncio.run(_apply_schema())
    print("[startup] Starting MCP server with REST API...", flush=True)

    # Get the underlying ASGI app from FastMCP
    asgi_app = mcp.streamable_http_app()

    # Wrap with REST routes: mount REST at /api, MCP stays at /mcp
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount

    combined_app = Starlette(
        routes=[
            *rest_routes,
            Mount("/mcp", app=asgi_app),
        ],
        on_startup=[_on_startup],
        on_shutdown=[_on_shutdown],
    )

    combined_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    import uvicorn
    uvicorn.run(combined_app, host="0.0.0.0", port=3001)
```

And add startup/shutdown handlers to manage the shared pool (replace the `app_lifespan` usage):

```python
_rest_app_ctx = None


async def _on_startup():
    global _rest_app_ctx
    pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=10,
    )
    http = httpx.AsyncClient()
    _rest_app_ctx = AppContext(pool=pool, http=http)


async def _on_shutdown():
    global _rest_app_ctx
    if _rest_app_ctx:
        await _rest_app_ctx.http.aclose()
        await _rest_app_ctx.pool.close()
```

Note: The MCP lifespan still creates its own pool for MCP tool calls. The REST endpoints use `_rest_app_ctx`. This is fine — two small connection pools to the same DB.

- [ ] **Step 5: Update `mcp-gateway/requirements.txt`**

```
mcp[http]
asyncpg
httpx
sentry-sdk[starlette]
uvicorn
```

- [ ] **Step 6: Test REST endpoints locally**

Rebuild and start:
```bash
docker compose build mcp-gateway && docker compose up -d mcp-gateway
```

Test:
```bash
# Create knowledge
curl -s -X POST http://localhost:3007/api/knowledge \
  -H "Content-Type: application/json" \
  -d '{"title":"Test entry","content":"Test content","project":"general"}' | python -m json.tool

# Search
curl -s -X POST http://localhost:3007/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"test","mode":"exact","types":["knowledge"]}' | python -m json.tool

# MCP endpoint still works
curl -s http://localhost:3007/mcp/ -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test"}},"id":1}'
```

Expected: All three return valid JSON responses.

- [ ] **Step 7: Commit**

```bash
git add mcp-gateway/server.py mcp-gateway/requirements.txt
git commit -m "feat: add REST API endpoints to mcp-gateway for web UI"
```

---

## Task 3: Sentry Integration

**Files:**
- Modify: `mcp-gateway/server.py` (add Sentry init near top)
- Modify: `embedder/embed.py` (add Sentry init near top)
- Modify: `embedder/requirements.txt` (add sentry-sdk)
- Modify: `docker-compose.yml` (add SENTRY_DSN env var)

- [ ] **Step 1: Add Sentry init to mcp-gateway/server.py**

Add after the imports at the top of `server.py`, before the config variables:

```python
import sentry_sdk

SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=0.1,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE", "openbrain@0.1.0"),
    )
    sentry_sdk.set_tag("service", "mcp-gateway")
```

- [ ] **Step 2: Add Sentry init to embedder/embed.py**

Add after the existing imports:

```python
try:
    import sentry_sdk
    SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
    if SENTRY_DSN:
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=0.1,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=os.environ.get("SENTRY_RELEASE", "openbrain@0.1.0"),
        )
        sentry_sdk.set_tag("service", "embedder")
except ImportError:
    pass
```

- [ ] **Step 3: Update embedder/requirements.txt**

```
openai>=1.0.0
psycopg2-binary>=2.9.0
sentry-sdk
```

- [ ] **Step 4: Add SENTRY_DSN to docker-compose.yml**

Add `SENTRY_DSN: ${SENTRY_DSN:-}` to the `environment` block of both `mcp-gateway` and `embedder` services.

For mcp-gateway, add after the `ORPHAN_POLICY` line:
```yaml
      SENTRY_DSN: ${SENTRY_DSN:-}
```

For embedder, add after the `POLL_INTERVAL` line:
```yaml
      SENTRY_DSN: ${SENTRY_DSN:-}
```

- [ ] **Step 5: Add SENTRY_DSN to .env.example**

Append:
```
# Sentry error tracking (optional)
SENTRY_DSN=
```

- [ ] **Step 6: Commit**

```bash
git add mcp-gateway/server.py embedder/embed.py embedder/requirements.txt docker-compose.yml .env.example
git commit -m "feat: add Sentry error tracking to mcp-gateway and embedder"
```

---

## Task 4: Web UI Container Setup

**Files:**
- Create: `web-ui/Dockerfile`
- Create: `web-ui/nginx.conf`
- Create: `web-ui/static/index.html`
- Create: `web-ui/static/css/style.css`
- Modify: `docker-compose.yml` (add web-ui service)

- [ ] **Step 1: Create `web-ui/Dockerfile`**

```dockerfile
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY static /usr/share/nginx/html
EXPOSE 80
```

- [ ] **Step 2: Create `web-ui/nginx.conf`**

```nginx
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    # SPA fallback — all non-file requests serve index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Read API — proxy to PostgREST (read-only)
    location /api/read/ {
        rewrite ^/api/read/(.*)$ /$1 break;
        proxy_pass http://postgrest:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Write API — proxy to mcp-gateway REST endpoints
    location /api/write/ {
        rewrite ^/api/write/(.*)$ /api/$1 break;
        proxy_pass http://mcp-gateway:3001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Content-Type $http_content_type;
    }

    # Search API — proxy to mcp-gateway
    location /api/search {
        proxy_pass http://mcp-gateway:3001/api/search;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Content-Type $http_content_type;
    }
}
```

- [ ] **Step 3: Create `web-ui/static/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenBrain</title>
    <link rel="stylesheet" href="/css/style.css">
</head>
<body>
    <div id="app"></div>
    <script type="module" src="/js/app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Create `web-ui/static/css/style.css`**

```css
/* === Reset & Base === */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface-2: #12121a;
    --border: #2a2a3a;
    --text-1: #e2e8f0;
    --text-2: #94a3b8;
    --text-3: #7c8491;
    --accent: #4f46e5;
    --accent-hover: #6366f1;
    --success: #22c55e;
    --warning: #f59e0b;
    --danger: #ef4444;
    --link-chip-bg: #1e3a5f;
    --link-chip-text: #58a6ff;
    --tag-bg: #1a2332;
    --tag-text: #7ee787;
    --sidebar-width: 180px;
    --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-mono: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}

body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text-1);
    line-height: 1.5;
    min-height: 100vh;
}

a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hover); }

/* === Layout === */
.app-layout {
    display: flex;
    min-height: 100vh;
}

/* === Sidebar === */
.sidebar {
    width: var(--sidebar-width);
    background: var(--surface-2);
    border-right: 1px solid var(--border);
    padding: 16px;
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    z-index: 100;
    display: flex;
    flex-direction: column;
}

.sidebar-logo {
    font-weight: 700;
    font-size: 15px;
    color: var(--text-1);
    margin-bottom: 20px;
}

.sidebar-nav {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
}

.sidebar-link {
    display: block;
    padding: 8px 10px;
    border-radius: 6px;
    font-size: 13px;
    color: var(--text-2);
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
}

.sidebar-link:hover { background: rgba(255,255,255,0.05); color: var(--text-1); }
.sidebar-link.active { background: var(--accent); color: #fff; }
.sidebar-divider { border-top: 1px solid var(--border); margin: 8px 0; padding-top: 8px; }

/* === Main content === */
.main-content {
    flex: 1;
    margin-left: var(--sidebar-width);
    padding: 24px;
    min-height: 100vh;
}

/* === Hamburger (mobile) === */
.hamburger {
    display: none;
    position: fixed;
    top: 12px;
    left: 12px;
    z-index: 200;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 10px;
    color: var(--text-1);
    cursor: pointer;
    font-size: 18px;
}

.sidebar-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.5);
    z-index: 99;
}

@media (max-width: 768px) {
    .hamburger { display: block; }
    .sidebar { transform: translateX(-100%); transition: transform 0.2s; }
    .sidebar.open { transform: translateX(0); }
    .sidebar-backdrop.open { display: block; }
    .main-content { margin-left: 0; padding: 60px 16px 16px; }
}

/* === Page header === */
.page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
}

.page-title { font-size: 18px; font-weight: 600; }

/* === Breadcrumb === */
.breadcrumb { font-size: 12px; color: var(--text-3); margin-bottom: 16px; }
.breadcrumb a { color: var(--accent); }

/* === Buttons === */
.btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 13px;
    cursor: pointer;
    border: none;
    transition: background 0.15s;
}

.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary { background: var(--surface); border: 1px solid var(--border); color: var(--text-1); }
.btn-secondary:hover { background: rgba(255,255,255,0.05); }
.btn-warning { background: var(--surface); border: 1px solid var(--warning); color: var(--warning); }
.btn-danger { background: #7f1d1d; border: 1px solid var(--danger); color: #fff; }
.btn-success { background: var(--surface); border: 1px solid var(--success); color: var(--success); }
.btn-sm { padding: 4px 10px; font-size: 11px; }

/* === Cards === */
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
}

/* === Stats grid === */
.stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 24px;
}

.stat-card { cursor: pointer; transition: border-color 0.15s; }
.stat-card:hover { border-color: var(--accent); }
.stat-label { font-size: 11px; text-transform: uppercase; color: var(--text-3); margin-bottom: 4px; }
.stat-value { font-size: 28px; font-weight: 700; }
.stat-sub { font-size: 11px; margin-top: 4px; }

@media (max-width: 768px) {
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
}

/* === Table === */
.data-table {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
}

.table-header {
    display: grid;
    padding: 10px 16px;
    font-size: 11px;
    text-transform: uppercase;
    color: var(--text-3);
    border-bottom: 1px solid var(--border);
}

.table-row {
    display: grid;
    padding: 12px 16px;
    font-size: 13px;
    border-bottom: 1px solid rgba(255,255,255,0.03);
    cursor: pointer;
    transition: background 0.1s;
}

.table-row:hover { background: #1c2128; }
.table-row:last-child { border-bottom: none; }

/* === Filter bar === */
.filter-bar {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
    align-items: center;
}

.filter-input, .filter-select {
    padding: 8px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-1);
    font-size: 13px;
}

.filter-input { flex: 1; min-width: 200px; }
.filter-input:focus, .filter-select:focus { outline: none; border-color: var(--accent); }

/* === Chips === */
.chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
}

.chip-project { background: var(--link-chip-bg); color: var(--link-chip-text); }
.chip-tag { background: var(--tag-bg); color: var(--tag-text); }
.chip-orphan { color: var(--warning); font-size: 11px; }
.chip-add {
    background: var(--surface);
    border: 1px dashed var(--border);
    color: var(--text-3);
    cursor: pointer;
    padding: 3px 10px;
}

.chip-remove {
    cursor: pointer;
    opacity: 0.6;
    margin-left: 2px;
}

.chip-remove:hover { opacity: 1; }

/* === Type badges === */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
}

.badge-knowledge { background: #1e3a5f; color: #58a6ff; }
.badge-memory { background: #1a2e1a; color: #22c55e; }
.badge-user { background: #1a2e1a; color: #22c55e; }
.badge-feedback { background: #2e2a1a; color: #f59e0b; }
.badge-project { background: #1a1a2e; color: #a78bfa; }
.badge-reference { background: #1e3a5f; color: #58a6ff; }
.badge-system { background: #2a2a3a; color: #94a3b8; }
.badge-active { background: #1a2e1a; color: #22c55e; }
.badge-archived { background: #2e2a1a; color: #f59e0b; }

/* === Similarity score === */
.similarity-high { background: #1a2e1a; color: #22c55e; }
.similarity-medium { background: #2a2e1a; color: #a3e635; }
.similarity-low { background: #2e2a1a; color: #f59e0b; }

/* === Pagination === */
.pagination {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 12px;
    font-size: 12px;
    color: var(--text-3);
}

.pagination-buttons { display: flex; gap: 4px; }

.page-btn {
    padding: 4px 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text-2);
    cursor: pointer;
    font-size: 12px;
}

.page-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

/* === Forms === */
.form-group { margin-bottom: 16px; }

.form-label {
    display: block;
    font-size: 12px;
    color: var(--text-3);
    margin-bottom: 4px;
}

.form-input, .form-select, .form-textarea {
    width: 100%;
    padding: 10px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-1);
    font-size: 14px;
    font-family: var(--font);
}

.form-textarea {
    min-height: 160px;
    resize: vertical;
    font-family: var(--font-mono);
    font-size: 13px;
}

.form-input:focus, .form-select:focus, .form-textarea:focus {
    outline: none;
    border-color: var(--accent);
}

.form-row {
    display: grid;
    grid-template-columns: 1fr 2fr;
    gap: 12px;
}

.form-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 16px;
}

/* === Markdown preview === */
.md-toggle {
    display: flex;
    gap: 4px;
    font-size: 11px;
}

.md-toggle-btn {
    padding: 3px 8px;
    border-radius: 4px;
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text-2);
}

.md-toggle-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

.md-content {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    font-size: 13px;
    line-height: 1.7;
    color: var(--text-1);
}

.md-content h1, .md-content h2, .md-content h3 { margin: 16px 0 8px; }
.md-content p { margin-bottom: 8px; }
.md-content ul, .md-content ol { padding-left: 24px; margin-bottom: 8px; }
.md-content code { background: #1e242c; padding: 1px 6px; border-radius: 3px; font-family: var(--font-mono); font-size: 12px; }
.md-content pre { background: #1e242c; padding: 12px; border-radius: 6px; overflow-x: auto; margin-bottom: 8px; }
.md-content pre code { padding: 0; background: none; }

/* === Modal === */
.modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 300;
    display: flex;
    align-items: center;
    justify-content: center;
}

.modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    max-width: 480px;
    width: 90%;
}

.modal-title { font-size: 16px; font-weight: 600; margin-bottom: 12px; }
.modal-body { font-size: 13px; color: var(--text-2); margin-bottom: 20px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

/* === Toast === */
.toast-container {
    position: fixed;
    top: 16px;
    right: 16px;
    z-index: 400;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.toast {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 13px;
    color: var(--text-1);
    cursor: pointer;
    max-width: 400px;
    animation: toast-in 0.2s ease-out;
}

.toast-error { border-color: var(--danger); }
.toast-success { border-color: var(--success); }
.toast-warning { border-color: var(--warning); }

@keyframes toast-in {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
}

/* === Tabs === */
.tabs {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
}

.tab {
    padding: 10px 20px;
    font-size: 13px;
    color: var(--text-3);
    cursor: pointer;
    border-bottom: 2px solid transparent;
}

.tab.active { color: var(--text-1); border-bottom-color: var(--accent); }
.tab-count { background: var(--border); padding: 1px 6px; border-radius: 10px; font-size: 11px; margin-left: 4px; }

/* === Checkbox === */
.checkbox {
    width: 16px;
    height: 16px;
    border: 1px solid var(--border);
    border-radius: 3px;
    background: var(--surface);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
}

.checkbox.checked { background: var(--accent); border-color: var(--accent); }

/* === Bulk action bar === */
.bulk-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    background: #1a1015;
    border: 1px solid #4a1525;
    border-radius: 8px;
    margin-top: 16px;
}

/* === Loading === */
.spinner {
    display: inline-block;
    width: 24px;
    height: 24px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.loading-center {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 200px;
}

/* === Empty state === */
.empty-state {
    text-align: center;
    padding: 48px 16px;
    color: var(--text-3);
    font-size: 14px;
}

.empty-state a { color: var(--accent); }

/* === Project cards grid === */
.project-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
}

.project-card { transition: border-color 0.15s; cursor: pointer; }
.project-card:hover { border-color: var(--accent); }

/* === Metadata row === */
.meta-row {
    display: flex;
    gap: 16px;
    font-size: 12px;
    color: var(--text-3);
    flex-wrap: wrap;
    align-items: center;
    margin-bottom: 16px;
}

.meta-row span { color: var(--text-1); }

/* === Two-column layout === */
.two-col {
    display: grid;
    grid-template-columns: 2fr 1fr;
    gap: 16px;
}

@media (max-width: 768px) {
    .two-col { grid-template-columns: 1fr; }
    .form-row { grid-template-columns: 1fr; }
}

/* === Search result cards === */
.search-result {
    padding: 14px 16px;
    transition: border-color 0.15s;
    cursor: pointer;
}

.search-result:hover { border-color: var(--accent); }

/* === Utility === */
.flex { display: flex; }
.flex-col { flex-direction: column; }
.gap-4 { gap: 4px; }
.gap-6 { gap: 6px; }
.gap-8 { gap: 8px; }
.gap-10 { gap: 10px; }
.gap-12 { gap: 12px; }
.items-center { align-items: center; }
.justify-between { justify-content: space-between; }
.flex-wrap { flex-wrap: wrap; }
.flex-1 { flex: 1; }
.text-right { text-align: right; }
.mt-8 { margin-top: 8px; }
.mb-12 { margin-bottom: 12px; }
.mb-16 { margin-bottom: 16px; }
.mb-20 { margin-bottom: 20px; }
```

- [ ] **Step 5: Add web-ui service to docker-compose.yml**

Add before the `docs:` service in `docker-compose.yml`:

```yaml
  web-ui:
    build: ./web-ui
    restart: unless-stopped
    ports:
      - "3010:80"
    networks:
      - default
      - nginxproxymanager_default
    depends_on:
      - postgrest
      - mcp-gateway
```

- [ ] **Step 6: Test container builds**

```bash
docker compose build web-ui
docker compose up -d web-ui
curl -s -o /dev/null -w "%{http_code}" http://localhost:3010/
```

Expected: HTTP 200, returns the index.html shell.

- [ ] **Step 7: Commit**

```bash
git add web-ui/ docker-compose.yml
git commit -m "feat: add web-ui container with Nginx proxy, SPA shell, and CSS"
```

---

## Task 5: Vendor Libraries

**Files:**
- Create: `web-ui/static/vendor/preact.min.js`
- Create: `web-ui/static/vendor/htm.min.js`
- Create: `web-ui/static/vendor/preact-hooks.min.js`
- Create: `web-ui/static/vendor/preact-signals.min.js`
- Create: `web-ui/static/vendor/marked.min.js`

- [ ] **Step 1: Download vendor libraries**

```bash
mkdir -p web-ui/static/vendor
curl -o web-ui/static/vendor/preact.min.js "https://unpkg.com/preact@10/dist/preact.module.js"
curl -o web-ui/static/vendor/preact-hooks.min.js "https://unpkg.com/preact@10/hooks/dist/hooks.module.js"
curl -o web-ui/static/vendor/htm.min.js "https://unpkg.com/htm@3/dist/htm.module.js"
curl -o web-ui/static/vendor/preact-signals.min.js "https://unpkg.com/@preact/signals@1/dist/signals.module.js"
curl -o web-ui/static/vendor/marked.min.js "https://unpkg.com/marked@12/marked.min.js"
```

- [ ] **Step 2: Verify files downloaded correctly**

```bash
ls -la web-ui/static/vendor/
```

Expected: All 5 files exist with non-zero sizes.

- [ ] **Step 3: Commit**

```bash
git add web-ui/static/vendor/
git commit -m "feat: add vendor libraries (preact, htm, signals, marked)"
```

---

## Task 6: Core Frontend Libraries

**Files:**
- Create: `web-ui/static/js/lib/state.js`
- Create: `web-ui/static/js/lib/api.js`
- Create: `web-ui/static/js/lib/markdown.js`

- [ ] **Step 1: Create `web-ui/static/js/lib/state.js`**

```javascript
import { signal } from '/vendor/preact-signals.min.js';

export const currentRoute = signal(window.location.hash || '#/');
export const sidebarOpen = signal(false);
export const toasts = signal([]);

window.addEventListener('hashchange', () => {
    currentRoute.value = window.location.hash || '#/';
});

export function navigate(hash) {
    window.location.hash = hash;
}

let toastId = 0;
export function addToast(message, type = 'error') {
    const id = ++toastId;
    toasts.value = [...toasts.value, { id, message, type }];
    setTimeout(() => removeToast(id), 5000);
}

export function removeToast(id) {
    toasts.value = toasts.value.filter(t => t.id !== id);
}
```

- [ ] **Step 2: Create `web-ui/static/js/lib/api.js`**

```javascript
import { addToast } from './state.js';

const READ_BASE = '/api/read';
const WRITE_BASE = '/api/write';
const SEARCH_URL = '/api/search';

async function request(url, options = {}) {
    try {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            const msg = body.error || body.message || `Error ${resp.status}`;
            throw new Error(msg);
        }
        if (resp.status === 204) return null;
        return resp.json();
    } catch (err) {
        if (err.message === 'Failed to fetch') {
            addToast('Cannot reach server. Check that containers are running.', 'error');
        } else {
            addToast(err.message, 'error');
        }
        throw err;
    }
}

// --- Read API (PostgREST) ---

export function readKnowledge(params = '') {
    return request(`${READ_BASE}/knowledge_with_projects?${params}`);
}

export function readKnowledgeById(id) {
    return request(`${READ_BASE}/knowledge?id=eq.${id}&select=*`);
}

export function readMemories(params = '') {
    return request(`${READ_BASE}/memories_with_projects?${params}`);
}

export function readMemoryById(id) {
    return request(`${READ_BASE}/memories?id=eq.${id}&select=*`);
}

export function readProjects(params = '') {
    return request(`${READ_BASE}/projects?${params}`);
}

export function readProjectByName(name) {
    return request(`${READ_BASE}/projects?name=eq.${name}`);
}

export function readRecentActivity(limit = 10) {
    return request(`${READ_BASE}/recent_activity?order=updated_at.desc&limit=${limit}`);
}

export function readOrphanedItems() {
    return request(`${READ_BASE}/rpc/orphaned_items`);
}

export function readCount(table, filter = 'status=eq.active') {
    return request(`${READ_BASE}/${table}?${filter}&select=count`, {
        headers: { 'Prefer': 'count=exact', 'Range-Unit': 'items', 'Range': '0-0' },
    }).then(() => null).catch(() => null);
}

export async function fetchCounts() {
    const headers = { 'Prefer': 'count=exact' };
    const opts = { headers };
    const [kResp, mResp, pResp] = await Promise.all([
        fetch(`${READ_BASE}/knowledge?status=eq.active&select=id&limit=0`, opts),
        fetch(`${READ_BASE}/memories?status=eq.active&select=id&limit=0`, opts),
        fetch(`${READ_BASE}/projects?status=in.(active,system)&select=id&limit=0`, opts),
    ]);
    const parseCount = (resp) => {
        const range = resp.headers.get('Content-Range');
        if (range) {
            const match = range.match(/\/(\d+)/);
            return match ? parseInt(match[1]) : 0;
        }
        return 0;
    };
    return {
        knowledge: parseCount(kResp),
        memories: parseCount(mResp),
        projects: parseCount(pResp),
    };
}

export async function fetchArchivedCounts() {
    const headers = { 'Prefer': 'count=exact' };
    const opts = { headers };
    const [kResp, mResp, pResp] = await Promise.all([
        fetch(`${READ_BASE}/knowledge?status=eq.archived&select=id&limit=0`, opts),
        fetch(`${READ_BASE}/memories?status=eq.archived&select=id&limit=0`, opts),
        fetch(`${READ_BASE}/projects?status=eq.archived&select=id&limit=0`, opts),
    ]);
    const parseCount = (resp) => {
        const range = resp.headers.get('Content-Range');
        if (range) {
            const match = range.match(/\/(\d+)/);
            return match ? parseInt(match[1]) : 0;
        }
        return 0;
    };
    return {
        knowledge: parseCount(kResp),
        memories: parseCount(mResp),
        projects: parseCount(pResp),
    };
}

// --- Write API (mcp-gateway REST) ---

export function createKnowledge(data) {
    return request(`${WRITE_BASE}/knowledge`, { method: 'POST', body: JSON.stringify(data) });
}

export function updateKnowledge(id, data) {
    return request(`${WRITE_BASE}/knowledge/${id}`, { method: 'PUT', body: JSON.stringify(data) });
}

export function deleteKnowledge(id) {
    return request(`${WRITE_BASE}/knowledge/${id}`, { method: 'DELETE' });
}

export function createMemory(data) {
    return request(`${WRITE_BASE}/memories`, { method: 'POST', body: JSON.stringify(data) });
}

export function updateMemory(id, data) {
    return request(`${WRITE_BASE}/memories/${id}`, { method: 'PUT', body: JSON.stringify(data) });
}

export function deleteMemory(id) {
    return request(`${WRITE_BASE}/memories/${id}`, { method: 'DELETE' });
}

export function createProject(data) {
    return request(`${WRITE_BASE}/projects`, { method: 'POST', body: JSON.stringify(data) });
}

export function updateProject(name, data) {
    return request(`${WRITE_BASE}/projects/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(data) });
}

export function deleteProject(name) {
    return request(`${WRITE_BASE}/projects/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

export function archiveItem(type, id) {
    return request(`${WRITE_BASE}/archive/${type}/${id}`, { method: 'POST' });
}

export function unarchiveItem(type, id) {
    return request(`${WRITE_BASE}/unarchive/${type}/${id}`, { method: 'POST' });
}

export function linkToProject(project, knowledgeId, memoryId) {
    const body = { project };
    if (knowledgeId) body.knowledge_id = knowledgeId;
    if (memoryId) body.memory_id = memoryId;
    return request(`${WRITE_BASE}/link`, { method: 'POST', body: JSON.stringify(body) });
}

export function unlinkFromProject(project, knowledgeId, memoryId) {
    const body = { project };
    if (knowledgeId) body.knowledge_id = knowledgeId;
    if (memoryId) body.memory_id = memoryId;
    return request(`${WRITE_BASE}/link`, { method: 'DELETE', body: JSON.stringify(body) });
}

export function searchItems(query, mode = 'semantic', types = ['knowledge', 'memories']) {
    return request(SEARCH_URL, {
        method: 'POST',
        body: JSON.stringify({ query, mode, types }),
    });
}

export function bulkDelete(items) {
    return request(`${WRITE_BASE}/bulk-delete`, {
        method: 'DELETE',
        body: JSON.stringify({ items }),
    });
}
```

- [ ] **Step 3: Create `web-ui/static/js/lib/markdown.js`**

```javascript
import '/vendor/marked.min.js';

const { marked } = window;

marked.setOptions({
    breaks: true,
    gfm: true,
});

export function renderMarkdown(text) {
    if (!text) return '';
    return marked.parse(text);
}
```

- [ ] **Step 4: Commit**

```bash
git add web-ui/static/js/lib/
git commit -m "feat: add core frontend libraries (state, API client, markdown)"
```

---

## Task 7: Shared Components

**Files:**
- Create: `web-ui/static/js/components/sidebar.js`
- Create: `web-ui/static/js/components/toast.js`
- Create: `web-ui/static/js/components/modal.js`
- Create: `web-ui/static/js/components/tag-chips.js`
- Create: `web-ui/static/js/components/markdown-editor.js`
- Create: `web-ui/static/js/components/entity-list.js`
- Create: `web-ui/static/js/components/entity-form.js`
- Create: `web-ui/static/js/components/search-bar.js`

This task creates the reusable Preact components. Each is a self-contained module exporting one or more components.

- [ ] **Step 1: Create sidebar component**

Create `web-ui/static/js/components/sidebar.js`:

```javascript
import { h } from '/vendor/preact.min.js';
import { html } from '/vendor/htm.min.js';
import { sidebarOpen, currentRoute, navigate } from '../lib/state.js';

const NAV_ITEMS = [
    { hash: '#/', label: 'Dashboard', icon: '\u{1F4CA}' },
    { hash: '#/knowledge', label: 'Knowledge', icon: '\u{1F4DA}' },
    { hash: '#/memories', label: 'Memories', icon: '\u{1F9E0}' },
    { hash: '#/projects', label: 'Projects', icon: '\u{1F4C1}' },
    { hash: '#/search', label: 'Search', icon: '\u{1F50D}' },
    { hash: '#/archive', label: 'Archive', icon: '\u{1F5C4}\uFE0F', divider: true },
];

export function Sidebar() {
    const route = currentRoute.value;
    const isOpen = sidebarOpen.value;

    function handleNav(hash) {
        navigate(hash);
        sidebarOpen.value = false;
    }

    function isActive(hash) {
        if (hash === '#/') return route === '#/' || route === '';
        return route.startsWith(hash);
    }

    return html`
        <button class="hamburger" onClick=${() => sidebarOpen.value = !isOpen}>☰</button>
        <div class="sidebar-backdrop ${isOpen ? 'open' : ''}" onClick=${() => sidebarOpen.value = false}></div>
        <nav class="sidebar ${isOpen ? 'open' : ''}">
            <div class="sidebar-logo">OpenBrain</div>
            <div class="sidebar-nav">
                ${NAV_ITEMS.map(item => html`
                    ${item.divider && html`<div class="sidebar-divider"></div>`}
                    <a class="sidebar-link ${isActive(item.hash) ? 'active' : ''}"
                       onClick=${() => handleNav(item.hash)}>
                        ${item.icon} ${item.label}
                    </a>
                `)}
            </div>
        </nav>
    `;
}
```

- [ ] **Step 2: Create toast component**

Create `web-ui/static/js/components/toast.js`:

```javascript
import { html } from '/vendor/htm.min.js';
import { toasts, removeToast } from '../lib/state.js';

export function ToastContainer() {
    const items = toasts.value;
    if (!items.length) return null;

    return html`
        <div class="toast-container">
            ${items.map(t => html`
                <div class="toast toast-${t.type}" key=${t.id} onClick=${() => removeToast(t.id)}>
                    ${t.message}
                </div>
            `)}
        </div>
    `;
}
```

- [ ] **Step 3: Create modal component**

Create `web-ui/static/js/components/modal.js`:

```javascript
import { html } from '/vendor/htm.min.js';

export function Modal({ title, children, onClose, actions }) {
    return html`
        <div class="modal-backdrop" onClick=${onClose}>
            <div class="modal" onClick=${e => e.stopPropagation()}>
                <div class="modal-title">${title}</div>
                <div class="modal-body">${children}</div>
                <div class="modal-actions">${actions}</div>
            </div>
        </div>
    `;
}

export function ConfirmModal({ title, message, confirmLabel, confirmClass, onConfirm, onCancel }) {
    return html`
        <${Modal} title=${title} onClose=${onCancel} actions=${html`
            <button class="btn btn-secondary" onClick=${onCancel}>Cancel</button>
            <button class="btn ${confirmClass || 'btn-danger'}" onClick=${onConfirm}>${confirmLabel || 'Confirm'}</button>
        `}>
            <p>${message}</p>
        <//>
    `;
}
```

- [ ] **Step 4: Create tag-chips component**

Create `web-ui/static/js/components/tag-chips.js`:

```javascript
import { html } from '/vendor/htm.min.js';
import { useState } from '/vendor/preact-hooks.min.js';
import { readProjects } from '../lib/api.js';

export function ProjectChips({ projects, onRemove, onAdd, readOnly }) {
    const [showDropdown, setShowDropdown] = useState(false);
    const [allProjects, setAllProjects] = useState([]);

    async function openDropdown() {
        const data = await readProjects('status=in.(active,system)&order=name&select=name');
        setAllProjects(data.map(p => p.name).filter(n => !projects.includes(n)));
        setShowDropdown(true);
    }

    function selectProject(name) {
        onAdd(name);
        setShowDropdown(false);
    }

    return html`
        <div class="flex gap-6 flex-wrap items-center">
            ${projects.map(p => html`
                <span class="chip chip-project">
                    ${p}
                    ${!readOnly && onRemove && html`
                        <span class="chip-remove" onClick=${() => onRemove(p)}>✕</span>
                    `}
                </span>
            `)}
            ${!readOnly && onAdd && html`
                <span class="chip chip-add" onClick=${openDropdown}>+ Add project</span>
            `}
            ${showDropdown && html`
                <div style="position:relative;">
                    <div style="position:absolute;top:0;left:0;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:4px;z-index:50;min-width:150px;">
                        ${allProjects.length === 0 && html`<div style="padding:8px;color:var(--text-3);font-size:12px;">No more projects</div>`}
                        ${allProjects.map(p => html`
                            <div style="padding:6px 10px;cursor:pointer;font-size:13px;border-radius:4px;"
                                 onMouseOver=${e => e.target.style.background = 'rgba(255,255,255,0.05)'}
                                 onMouseOut=${e => e.target.style.background = 'transparent'}
                                 onClick=${() => selectProject(p)}>${p}</div>
                        `)}
                        <div style="padding:6px 10px;cursor:pointer;font-size:11px;color:var(--text-3);border-top:1px solid var(--border);margin-top:4px;"
                             onClick=${() => setShowDropdown(false)}>Cancel</div>
                    </div>
                </div>
            `}
        </div>
    `;
}

export function TagChips({ tags }) {
    if (!tags || !tags.length) return null;
    return html`
        <div class="flex gap-4 flex-wrap">
            ${tags.map(t => html`<span class="chip chip-tag">${t}</span>`)}
        </div>
    `;
}
```

- [ ] **Step 5: Create markdown-editor component**

Create `web-ui/static/js/components/markdown-editor.js`:

```javascript
import { html } from '/vendor/htm.min.js';
import { useState } from '/vendor/preact-hooks.min.js';
import { renderMarkdown } from '../lib/markdown.js';

export function MarkdownEditor({ value, onChange, label }) {
    const [mode, setMode] = useState('write');

    return html`
        <div class="form-group">
            <div class="flex justify-between items-center" style="margin-bottom:4px;">
                <label class="form-label" style="margin-bottom:0;">${label || 'Content'} (Markdown)</label>
                <div class="md-toggle">
                    <span class="md-toggle-btn ${mode === 'write' ? 'active' : ''}"
                          onClick=${() => setMode('write')}>Write</span>
                    <span class="md-toggle-btn ${mode === 'preview' ? 'active' : ''}"
                          onClick=${() => setMode('preview')}>Preview</span>
                </div>
            </div>
            ${mode === 'write'
                ? html`<textarea class="form-textarea" value=${value}
                         onInput=${e => onChange(e.target.value)}></textarea>`
                : html`<div class="md-content" dangerouslySetInnerHTML=${{ __html: renderMarkdown(value) }}></div>`
            }
        </div>
    `;
}
```

- [ ] **Step 6: Create entity-list component**

Create `web-ui/static/js/components/entity-list.js`:

```javascript
import { html } from '/vendor/htm.min.js';
import { useState, useEffect } from '/vendor/preact-hooks.min.js';
import { navigate } from '../lib/state.js';

export function EntityList({ fetchFn, columns, gridTemplate, detailRoute, filters, emptyMessage }) {
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [page, setPage] = useState(0);
    const [filterValues, setFilterValues] = useState({});
    const pageSize = 20;

    async function load() {
        setLoading(true);
        try {
            const data = await fetchFn(page, pageSize, filterValues);
            setItems(data);
        } catch (e) { /* toast handled by api.js */ }
        setLoading(false);
    }

    useEffect(() => { load(); }, [page, JSON.stringify(filterValues)]);

    function updateFilter(key, value) {
        setFilterValues(prev => ({ ...prev, [key]: value }));
        setPage(0);
    }

    return html`
        ${filters && html`
            <div class="filter-bar">
                ${filters(filterValues, updateFilter)}
            </div>
        `}
        ${loading
            ? html`<div class="loading-center"><div class="spinner"></div></div>`
            : items.length === 0
                ? html`<div class="empty-state">${emptyMessage || 'No items found.'}</div>`
                : html`
                    <div class="data-table">
                        <div class="table-header" style="grid-template-columns:${gridTemplate}">
                            ${columns.map(c => html`<div>${c.label}</div>`)}
                        </div>
                        ${items.map(item => html`
                            <div class="table-row" style="grid-template-columns:${gridTemplate}"
                                 onClick=${() => navigate(detailRoute(item))}>
                                ${columns.map(c => html`<div>${c.render(item)}</div>`)}
                            </div>
                        `)}
                    </div>
                    <div class="pagination">
                        <div>Page ${page + 1}</div>
                        <div class="pagination-buttons">
                            <button class="page-btn" disabled=${page === 0}
                                    onClick=${() => setPage(p => p - 1)}>← Prev</button>
                            <button class="page-btn" disabled=${items.length < pageSize}
                                    onClick=${() => setPage(p => p + 1)}>Next →</button>
                        </div>
                    </div>
                `
        }
    `;
}
```

- [ ] **Step 7: Create entity-form and search-bar components**

Create `web-ui/static/js/components/entity-form.js`:

```javascript
import { html } from '/vendor/htm.min.js';
import { MarkdownEditor } from './markdown-editor.js';
import { ProjectChips } from './tag-chips.js';

export function EntityForm({ fields, values, onChange, onSubmit, onCancel, submitLabel }) {
    function updateField(name, value) {
        onChange({ ...values, [name]: value });
    }

    return html`
        <form onSubmit=${e => { e.preventDefault(); onSubmit(); }}>
            ${fields.map(f => {
                if (f.type === 'markdown') {
                    return html`<${MarkdownEditor} label=${f.label} value=${values[f.name] || ''}
                                  onChange=${v => updateField(f.name, v)} />`;
                }
                if (f.type === 'projects') {
                    return html`
                        <div class="form-group">
                            <label class="form-label">${f.label}</label>
                            <${ProjectChips} projects=${values[f.name] || []}
                                onAdd=${p => updateField(f.name, [...(values[f.name] || []), p])}
                                onRemove=${p => updateField(f.name, (values[f.name] || []).filter(x => x !== p))} />
                        </div>
                    `;
                }
                if (f.type === 'select') {
                    return html`
                        <div class="form-group">
                            <label class="form-label">${f.label}</label>
                            <select class="form-select" value=${values[f.name] || ''}
                                    onChange=${e => updateField(f.name, e.target.value)}>
                                ${f.options.map(o => html`<option value=${o}>${o}</option>`)}
                            </select>
                        </div>
                    `;
                }
                return html`
                    <div class="form-group">
                        <label class="form-label">${f.label}${f.required ? ' *' : ''}</label>
                        <input class="form-input" type="text" value=${values[f.name] || ''}
                               placeholder=${f.placeholder || ''}
                               required=${f.required}
                               readOnly=${f.readOnly}
                               onInput=${e => updateField(f.name, e.target.value)} />
                    </div>
                `;
            })}
            <div class="form-actions">
                <button type="button" class="btn btn-secondary" onClick=${onCancel}>Cancel</button>
                <button type="submit" class="btn btn-primary">${submitLabel || 'Save'}</button>
            </div>
        </form>
    `;
}
```

Create `web-ui/static/js/components/search-bar.js`:

```javascript
import { html } from '/vendor/htm.min.js';
import { useState } from '/vendor/preact-hooks.min.js';

export function SearchBar({ onSearch, initialQuery }) {
    const [query, setQuery] = useState(initialQuery || '');
    const [mode, setMode] = useState('semantic');
    const [types, setTypes] = useState(['knowledge', 'memories']);

    function toggleType(t) {
        if (types.length === 2) {
            setTypes([t]);
        } else if (types.includes(t)) {
            setTypes(['knowledge', 'memories']);
        } else {
            setTypes([t]);
        }
    }

    function handleSubmit(e) {
        e.preventDefault();
        if (query.trim()) onSearch(query, mode, types);
    }

    return html`
        <form onSubmit=${handleSubmit}>
            <div class="flex gap-8 mb-12">
                <input class="form-input flex-1" value=${query} onInput=${e => setQuery(e.target.value)}
                       placeholder="Search knowledge and memories..." style="font-size:14px;padding:12px 16px;" />
                <button type="submit" class="btn btn-primary" style="padding:12px 20px;">Search</button>
            </div>
            <div class="flex gap-12 items-center" style="font-size:12px;">
                <div class="flex gap-4">
                    <span class="md-toggle-btn ${mode === 'semantic' ? 'active' : ''}"
                          onClick=${() => setMode('semantic')}>Semantic</span>
                    <span class="md-toggle-btn ${mode === 'exact' ? 'active' : ''}"
                          onClick=${() => setMode('exact')}>Exact</span>
                </div>
                <span style="color:var(--text-3);">|</span>
                <div class="flex gap-4">
                    <span class="md-toggle-btn ${types.length === 2 ? 'active' : ''}"
                          onClick=${() => setTypes(['knowledge', 'memories'])}>All</span>
                    <span class="md-toggle-btn ${types.length === 1 && types[0] === 'knowledge' ? 'active' : ''}"
                          onClick=${() => toggleType('knowledge')}>Knowledge</span>
                    <span class="md-toggle-btn ${types.length === 1 && types[0] === 'memories' ? 'active' : ''}"
                          onClick=${() => toggleType('memories')}>Memories</span>
                </div>
            </div>
        </form>
    `;
}
```

- [ ] **Step 8: Commit**

```bash
git add web-ui/static/js/components/
git commit -m "feat: add shared Preact components (sidebar, modal, chips, list, form, editor)"
```

---

## Task 8: App Router and Page Shells

**Files:**
- Create: `web-ui/static/js/app.js`
- Create: `web-ui/static/js/pages/dashboard.js`
- Create: `web-ui/static/js/pages/knowledge.js`
- Create: `web-ui/static/js/pages/memories.js`
- Create: `web-ui/static/js/pages/projects.js`
- Create: `web-ui/static/js/pages/search.js`
- Create: `web-ui/static/js/pages/archive.js`

This is the largest task. Each page module exports components for list, detail, create, and edit views. The router in `app.js` matches the hash route and renders the correct page.

Due to the size of this task, each page should be implemented one at a time with a commit after each is working. The step sequence below covers the router + all 6 pages.

- [ ] **Step 1: Create `web-ui/static/js/app.js` (router + app shell)**

```javascript
import { h, render } from '/vendor/preact.min.js';
import { html } from '/vendor/htm.min.js';
import { currentRoute } from './lib/state.js';
import { Sidebar } from './components/sidebar.js';
import { ToastContainer } from './components/toast.js';
import { DashboardPage } from './pages/dashboard.js';
import { KnowledgePage } from './pages/knowledge.js';
import { MemoriesPage } from './pages/memories.js';
import { ProjectsPage } from './pages/projects.js';
import { SearchPage } from './pages/search.js';
import { ArchivePage } from './pages/archive.js';

function Router() {
    const route = currentRoute.value;
    const [base, ...rest] = route.replace('#/', '').split('/');
    const param = rest.join('/');

    switch (base) {
        case '':
            return html`<${DashboardPage} />`;
        case 'knowledge':
            return html`<${KnowledgePage} param=${param} />`;
        case 'memories':
            return html`<${MemoriesPage} param=${param} />`;
        case 'projects':
            return html`<${ProjectsPage} param=${param} />`;
        case 'search':
            return html`<${SearchPage} />`;
        case 'archive':
            return html`<${ArchivePage} />`;
        default:
            return html`<${DashboardPage} />`;
    }
}

function App() {
    return html`
        <div class="app-layout">
            <${Sidebar} />
            <main class="main-content">
                <${Router} />
            </main>
            <${ToastContainer} />
        </div>
    `;
}

render(html`<${App} />`, document.getElementById('app'));
```

- [ ] **Step 2: Create dashboard page**

Create `web-ui/static/js/pages/dashboard.js`. This page fetches stats, recent activity, and orphaned items. Full implementation:

```javascript
import { html } from '/vendor/htm.min.js';
import { useState, useEffect } from '/vendor/preact-hooks.min.js';
import { navigate } from '../lib/state.js';
import { fetchCounts, fetchArchivedCounts, readRecentActivity, readOrphanedItems } from '../lib/api.js';

function timeAgo(dateStr) {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
}

export function DashboardPage() {
    const [counts, setCounts] = useState(null);
    const [archived, setArchived] = useState(null);
    const [recent, setRecent] = useState([]);
    const [orphans, setOrphans] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        Promise.all([
            fetchCounts(),
            fetchArchivedCounts(),
            readRecentActivity(10),
            readOrphanedItems(),
        ]).then(([c, a, r, o]) => {
            setCounts(c);
            setArchived(a);
            setRecent(r);
            setOrphans(o);
            setLoading(false);
        }).catch(() => setLoading(false));
    }, []);

    if (loading) return html`<div class="loading-center"><div class="spinner"></div></div>`;

    const typeBadge = (type) => type === 'knowledge'
        ? html`<span class="badge badge-knowledge">knowledge</span>`
        : html`<span class="badge badge-memory">memory</span>`;

    return html`
        <div class="page-header"><h1 class="page-title">Dashboard</h1></div>

        <div class="stats-grid">
            <div class="card stat-card" onClick=${() => navigate('#/knowledge')}>
                <div class="stat-label">Knowledge</div>
                <div class="stat-value">${counts?.knowledge || 0}</div>
                <div class="stat-sub" style="color:var(--accent);">${archived?.knowledge || 0} archived</div>
            </div>
            <div class="card stat-card" onClick=${() => navigate('#/memories')}>
                <div class="stat-label">Memories</div>
                <div class="stat-value">${counts?.memories || 0}</div>
                <div class="stat-sub" style="color:var(--accent);">${archived?.memories || 0} archived</div>
            </div>
            <div class="card stat-card" onClick=${() => navigate('#/projects')}>
                <div class="stat-label">Projects</div>
                <div class="stat-value">${counts?.projects || 0}</div>
                <div class="stat-sub" style="color:var(--accent);">1 system</div>
            </div>
            <div class="card stat-card" style=${orphans.length > 0 ? 'border-color:var(--warning)' : ''}
                 onClick=${() => navigate('#/knowledge')}>
                <div class="stat-label">Orphans</div>
                <div class="stat-value" style=${orphans.length > 0 ? 'color:var(--warning)' : ''}>${orphans.length}</div>
                <div class="stat-sub" style="color:var(--warning);">${orphans.length > 0 ? 'need attention' : 'all linked'}</div>
            </div>
        </div>

        <div class="two-col">
            <div class="card">
                <div style="font-size:13px;font-weight:600;margin-bottom:12px;">Recent Activity</div>
                <div class="flex flex-col gap-10">
                    ${recent.map(item => html`
                        <div class="flex justify-between items-center" style="padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.03);cursor:pointer;"
                             onClick=${() => navigate(`#/${item.type === 'knowledge' ? 'knowledge' : 'memories'}/${item.id}`)}>
                            <div>${typeBadge(item.type)} <span style="margin-left:8px;">${item.name}</span></div>
                            <div style="color:var(--text-3);font-size:11px;">${timeAgo(item.updated_at)}</div>
                        </div>
                    `)}
                    ${recent.length === 0 && html`<div style="color:var(--text-3);font-size:13px;">No recent activity</div>`}
                </div>
            </div>

            ${orphans.length > 0 && html`
                <div class="card" style="border-color:var(--warning);">
                    <div style="font-size:13px;font-weight:600;color:var(--warning);margin-bottom:12px;">⚠ Orphaned Items</div>
                    <div style="font-size:12px;color:var(--text-2);margin-bottom:12px;">Not linked to any project</div>
                    <div class="flex flex-col gap-8">
                        ${orphans.slice(0, 5).map(item => html`
                            <div style="padding:6px 8px;background:var(--bg);border-radius:4px;cursor:pointer;font-size:12px;"
                                 onClick=${() => navigate(`#/${item.type === 'knowledge' ? 'knowledge' : 'memories'}/${item.id}`)}>
                                ${item.name}
                            </div>
                        `)}
                    </div>
                    ${orphans.length > 5 && html`
                        <div style="margin-top:10px;font-size:11px;color:var(--accent);cursor:pointer;"
                             onClick=${() => navigate('#/knowledge')}>View all orphans →</div>
                    `}
                </div>
            `}
        </div>
    `;
}
```

- [ ] **Step 3: Create knowledge page**

Create `web-ui/static/js/pages/knowledge.js`. Implements list, detail, create, and edit views based on the `param` prop. This is the most complex page — memories follows the same pattern.

Due to length, this file implements all four knowledge sub-views (list, detail, new, edit) in ~250 lines. The key patterns:
- List view uses `EntityList` with PostgREST queries
- Detail view fetches by ID, renders markdown, shows project chips with link/unlink
- Create/Edit use `EntityForm` with the knowledge field schema

Implementation should follow the patterns established in the dashboard page and the shared components. The list fetches from `readKnowledge()`, detail from `readKnowledgeById()`, create calls `createKnowledge()`, edit calls `updateKnowledge()`.

- [ ] **Step 4: Create memories page**

Create `web-ui/static/js/pages/memories.js`. Same pattern as knowledge but with `memory_type` instead of `category`, a description field, and no URL/tags. Fetches from `readMemories()`, `readMemoryById()`, etc.

- [ ] **Step 5: Create projects page**

Create `web-ui/static/js/pages/projects.js`. Uses card grid instead of table for list view. Detail shows linked entities in tabs. Form includes orphan_policy dropdown.

- [ ] **Step 6: Create search page**

Create `web-ui/static/js/pages/search.js`. Uses `SearchBar` component, calls `searchItems()`, renders results as cards with similarity scores.

- [ ] **Step 7: Create archive page**

Create `web-ui/static/js/pages/archive.js`. Fetches all archived items from all three tables. Implements checkbox selection, bulk action bar, restore (unarchive) and delete (with confirmation modal).

- [ ] **Step 8: Commit all pages**

```bash
git add web-ui/static/js/
git commit -m "feat: add all page components (dashboard, knowledge, memories, projects, search, archive)"
```

---

## Task 9: Sentry Frontend Integration

**Files:**
- Download: `web-ui/static/vendor/sentry.min.js`
- Modify: `web-ui/static/js/app.js` (add Sentry init)
- Modify: `web-ui/static/index.html` (add Sentry script)

- [ ] **Step 1: Download Sentry browser bundle**

```bash
curl -o web-ui/static/vendor/sentry.min.js "https://browser.sentry-cdn.com/8.0.0/bundle.min.js"
```

- [ ] **Step 2: Add Sentry script to index.html**

Add before the app.js script tag:

```html
<script src="/vendor/sentry.min.js"></script>
<script>
    if (window.Sentry && window.SENTRY_DSN) {
        Sentry.init({ dsn: window.SENTRY_DSN, environment: 'production' });
        Sentry.setTag('service', 'web-ui');
    }
</script>
```

- [ ] **Step 3: Inject SENTRY_DSN via nginx config**

Add a location block to `web-ui/nginx.conf` that serves a tiny config JS file dynamically. Alternatively, since we're using Nginx, add a `sub_filter` directive to inject the DSN into index.html. The simplest approach: create a `config.js` that reads from a known endpoint.

Create `web-ui/static/js/config.js`:
```javascript
window.SENTRY_DSN = '';
```

This file will be overridden at container startup via an entrypoint script, or kept empty if Sentry isn't configured.

Update `web-ui/Dockerfile`:
```dockerfile
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY static /usr/share/nginx/html
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
```

Create `web-ui/entrypoint.sh`:
```bash
#!/bin/sh
if [ -n "$SENTRY_DSN" ]; then
    echo "window.SENTRY_DSN = '${SENTRY_DSN}';" > /usr/share/nginx/html/js/config.js
fi
exec "$@"
```

Add config.js to index.html before sentry.min.js:
```html
<script src="/js/config.js"></script>
```

- [ ] **Step 4: Add SENTRY_DSN env var to web-ui in docker-compose.yml**

```yaml
  web-ui:
    build: ./web-ui
    restart: unless-stopped
    environment:
      SENTRY_DSN: ${SENTRY_DSN:-}
    ports:
      - "3010:80"
```

- [ ] **Step 5: Commit**

```bash
git add web-ui/static/vendor/sentry.min.js web-ui/static/js/config.js web-ui/entrypoint.sh web-ui/Dockerfile web-ui/static/index.html web-ui/nginx.conf docker-compose.yml
git commit -m "feat: add Sentry frontend error tracking"
```

---

## Task 10: Integration Testing

- [ ] **Step 1: Build and start all containers**

```bash
docker compose build
docker compose up -d
```

- [ ] **Step 2: Verify PostgREST views work through Nginx**

```bash
curl -s http://localhost:3010/api/read/knowledge_with_projects?limit=2 | python -m json.tool
curl -s http://localhost:3010/api/read/recent_activity?order=updated_at.desc&limit=5 | python -m json.tool
curl -s http://localhost:3010/api/read/rpc/orphaned_items | python -m json.tool
```

Expected: JSON arrays returned from each.

- [ ] **Step 3: Verify write endpoints work through Nginx**

```bash
# Create knowledge
curl -s -X POST http://localhost:3010/api/write/knowledge \
  -H "Content-Type: application/json" \
  -d '{"title":"Integration test","content":"Test content"}' | python -m json.tool

# Search
curl -s -X POST http://localhost:3010/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"integration","mode":"exact","types":["knowledge"]}' | python -m json.tool
```

Expected: 201 with created entry, then search returns the entry.

- [ ] **Step 4: Open browser and test UI**

Open `http://localhost:3010` in a browser. Verify:
1. Dashboard loads with stats cards
2. Knowledge list shows entries
3. Create new knowledge entry via the form
4. Edit an existing entry
5. Archive an entry (click Archive on detail page)
6. View Archive page, see the archived entry
7. Restore the archived entry
8. Search works (try both semantic and exact)
9. Project detail shows linked entities
10. Mobile responsive (resize browser to <768px, hamburger menu works)

- [ ] **Step 5: Clean up test data and commit any fixes**

```bash
git add -A
git commit -m "fix: integration test adjustments"
```

---

## Task 11: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add web UI section to README**

Add a section documenting the web UI: how to access it (port 3010), what it does, and the container architecture. Mention the optional Sentry DSN configuration.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add web UI documentation to README"
```
