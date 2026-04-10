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
SCHEMA_FILE = os.environ.get("SCHEMA_FILE", "/app/init.sql")
ORPHAN_POLICY = os.environ.get("ORPHAN_POLICY", "archive")


@dataclass
class AppContext:
    pool: asyncpg.Pool
    http: httpx.AsyncClient


def _split_sql(sql: str) -> list[str]:
    """Split SQL text into individual statements, respecting $$ blocks."""
    statements: list[str] = []
    current: list[str] = []
    in_dollar = False
    for line in sql.splitlines():
        stripped = line.strip()
        # Toggle $$ quoting (used in PL/pgSQL function bodies)
        if "$$" in stripped:
            count = stripped.count("$$")
            if count % 2 == 1:
                in_dollar = not in_dollar
        current.append(line)
        # Statement ends with ; outside a $$ block
        if stripped.endswith(";") and not in_dollar:
            stmt = "\n".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
    # Leftover (shouldn't happen with well-formed SQL)
    remaining = "\n".join(current).strip().rstrip(";").strip()
    if remaining:
        statements.append(remaining)
    return statements


async def _apply_schema() -> None:
    """Apply init.sql schema eagerly at process start (before MCP server)."""
    if not os.path.exists(SCHEMA_FILE):
        return
    with open(SCHEMA_FILE) as f:
        schema_sql = f.read()
    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )
    try:
        await conn.execute(schema_sql)
        print("[schema] init.sql applied successfully", flush=True)
    except Exception as e:
        print(f"[schema] WARNING: batch execute failed: {e}", flush=True)
        print("[schema] Attempting statements individually...", flush=True)
        for stmt in _split_sql(schema_sql):
            try:
                await conn.execute(stmt)
            except Exception as stmt_err:
                print(f"[schema] Skipped statement: {stmt_err}", flush=True)
    finally:
        await conn.close()


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


# --- Knowledge tools ---


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


# --- Project tools ---


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


# --- Memory tools ---


VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}


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

            await conn.execute(
                """UPDATE projects SET status = 'archived', updated_at = NOW()
                   WHERE id = $1""",
                project_id,
            )

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

            await conn.execute(
                """UPDATE project_links SET status = 'archived', archived_at = NOW()
                   WHERE project_id = $1 AND status = 'active'""",
                project_id,
            )

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


if __name__ == "__main__":
    import asyncio
    print("[startup] Applying database schema...", flush=True)
    asyncio.run(_apply_schema())
    print("[startup] Starting MCP server...", flush=True)
    mcp.run(transport="streamable-http")
