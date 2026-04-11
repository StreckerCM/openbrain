import json
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg
import httpx
from mcp.server.fastmcp import FastMCP, Context

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


# ---------------------------------------------------------------------------
# Shared DB functions (used by both MCP tools and REST endpoints)
# ---------------------------------------------------------------------------


async def _db_add_knowledge(
    pool: asyncpg.Pool,
    title: str,
    content: str,
    project: str = "general",
    category: str = "general",
    tags: list[str] | None = None,
    url: str | None = None,
) -> dict:
    """Insert a knowledge entry and auto-link to project. Returns the row dict."""
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


async def _db_update_knowledge(pool: asyncpg.Pool, kid: int, **fields) -> dict | None:
    """Partial update of a knowledge entry. Returns updated row or None."""
    allowed = {"title", "content", "category", "url", "tags", "project"}
    sets = []
    params = []
    idx = 1
    for col, val in fields.items():
        if col in allowed and val is not None:
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
    if not sets:
        return None
    sets.append("updated_at = NOW()")
    params.append(kid)
    query = f"""UPDATE knowledge SET {', '.join(sets)}
                WHERE id = ${idx} AND status = 'active'
                RETURNING id, project, category, title, url, tags, status, updated_at"""
    row = await pool.fetchrow(query, *params)
    return dict(row) if row else None


async def _db_hard_delete_knowledge(pool: asyncpg.Pool, kid: int) -> dict | None:
    """Hard-delete a knowledge entry (must be archived). Returns deleted row or None."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, title, status FROM knowledge WHERE id = $1", kid
            )
            if row is None:
                return None
            if row["status"] != "archived":
                raise ValueError("Only archived knowledge can be deleted")
            await conn.execute(
                "DELETE FROM project_links WHERE knowledge_id = $1", kid
            )
            await conn.execute("DELETE FROM knowledge WHERE id = $1", kid)
    return dict(row)


async def _db_save_memory(
    pool: asyncpg.Pool,
    memory_type: str,
    name: str,
    content: str,
    description: str | None = None,
    project: str = "general",
) -> dict:
    """Insert a memory and auto-link to project. Returns the row dict."""
    if memory_type not in VALID_MEMORY_TYPES:
        raise ValueError(f"memory_type must be one of: {', '.join(sorted(VALID_MEMORY_TYPES))}")
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


async def _db_update_memory(pool: asyncpg.Pool, mid: int, **fields) -> dict | None:
    """Partial update of a memory. Returns updated row or None."""
    allowed = {"memory_type", "name", "content", "description", "project"}
    sets = []
    params = []
    idx = 1
    for col, val in fields.items():
        if col in allowed and val is not None:
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
    if not sets:
        return None
    sets.append("updated_at = NOW()")
    params.append(mid)
    query = f"""UPDATE memories SET {', '.join(sets)}
                WHERE id = ${idx} AND status = 'active'
                RETURNING id, memory_type, name, description, content, project, status, updated_at"""
    row = await pool.fetchrow(query, *params)
    return dict(row) if row else None


async def _db_hard_delete_memory(pool: asyncpg.Pool, mid: int) -> dict | None:
    """Hard-delete a memory (must be archived). Returns deleted row or None."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, name, status FROM memories WHERE id = $1", mid
            )
            if row is None:
                return None
            if row["status"] != "archived":
                raise ValueError("Only archived memories can be deleted")
            await conn.execute(
                "DELETE FROM project_links WHERE memory_id = $1", mid
            )
            await conn.execute("DELETE FROM memories WHERE id = $1", mid)
    return dict(row)


async def _db_hard_delete_project(pool: asyncpg.Pool, name: str) -> dict | None:
    """Hard-delete a project (must be archived, cannot be 'general'). Returns deleted row or None."""
    if name == "general":
        raise ValueError("Cannot delete the 'general' project")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, name, status FROM projects WHERE name = $1", name
            )
            if row is None:
                return None
            if row["status"] != "archived":
                raise ValueError("Only archived projects can be deleted")
            await conn.execute(
                "DELETE FROM project_links WHERE project_id = $1", row["id"]
            )
            await conn.execute("DELETE FROM projects WHERE id = $1", row["id"])
    return dict(row)


async def _db_archive(pool: asyncpg.Pool, entity_type: str, entity_id: int) -> dict | None:
    """Archive a knowledge entry or memory. Returns the updated row or None."""
    if entity_type == "knowledge":
        table, id_col, ret_cols = "knowledge", "id", "id, title, status"
        link_col = "knowledge_id"
    elif entity_type == "memory":
        table, id_col, ret_cols = "memories", "id", "id, name, status"
        link_col = "memory_id"
    else:
        raise ValueError("entity_type must be 'knowledge' or 'memory'")

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""UPDATE {table} SET status = 'archived', updated_at = NOW()
                    WHERE {id_col} = $1 AND status = 'active'
                    RETURNING {ret_cols}""",
                entity_id,
            )
            if row is None:
                return None
            await conn.execute(
                f"""UPDATE project_links SET status = 'archived', archived_at = NOW()
                    WHERE {link_col} = $1 AND status = 'active'""",
                entity_id,
            )
    return dict(row)


async def _db_unarchive(pool: asyncpg.Pool, entity_type: str, entity_id: int) -> dict | None:
    """Unarchive a knowledge entry or memory. Returns the updated row or None."""
    if entity_type == "knowledge":
        table, ret_cols = "knowledge", "id, title, status"
    elif entity_type == "memory":
        table, ret_cols = "memories", "id, name, status"
    else:
        raise ValueError("entity_type must be 'knowledge' or 'memory'")

    row = await pool.fetchrow(
        f"""UPDATE {table} SET status = 'active', updated_at = NOW()
            WHERE id = $1 AND status = 'archived'
            RETURNING {ret_cols}""",
        entity_id,
    )
    return dict(row) if row else None


async def _db_link(
    pool: asyncpg.Pool,
    project_name: str,
    knowledge_id: int | None = None,
    memory_id: int | None = None,
) -> dict | None:
    """Link a knowledge entry or memory to a project. Returns the link row or None if already exists."""
    if (knowledge_id is None) == (memory_id is None):
        raise ValueError("Provide exactly one of knowledge_id or memory_id")

    async with pool.acquire() as conn:
        async with conn.transaction():
            proj = await conn.fetchrow(
                "SELECT id FROM projects WHERE name = $1 AND status IN ('active', 'system')",
                project_name,
            )
            if proj is None:
                raise LookupError(f"Project '{project_name}' not found or not active")

            if knowledge_id is not None:
                exists = await conn.fetchval(
                    "SELECT id FROM knowledge WHERE id = $1 AND status = 'active'",
                    knowledge_id,
                )
                if exists is None:
                    raise LookupError(f"Knowledge entry {knowledge_id} not found or not active")
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
                    raise LookupError(f"Memory {memory_id} not found or not active")
                row = await conn.fetchrow(
                    """INSERT INTO project_links (project_id, memory_id, status)
                       VALUES ($1, $2, 'active')
                       ON CONFLICT DO NOTHING
                       RETURNING id, project_id, memory_id, status""",
                    proj["id"], memory_id,
                )
    return dict(row) if row else None


async def _db_unlink(
    pool: asyncpg.Pool,
    project_name: str,
    knowledge_id: int | None = None,
    memory_id: int | None = None,
) -> dict | None:
    """Unlink a knowledge entry or memory from a project. Returns the link row or None."""
    if (knowledge_id is None) == (memory_id is None):
        raise ValueError("Provide exactly one of knowledge_id or memory_id")

    proj = await pool.fetchrow(
        "SELECT id FROM projects WHERE name = $1", project_name
    )
    if proj is None:
        raise LookupError(f"Project '{project_name}' not found")

    if knowledge_id is not None:
        row = await pool.fetchrow(
            """UPDATE project_links SET status = 'archived', archived_at = NOW()
               WHERE project_id = $1 AND knowledge_id = $2 AND status = 'active'
               RETURNING id, project_id, knowledge_id, status""",
            proj["id"], knowledge_id,
        )
    else:
        row = await pool.fetchrow(
            """UPDATE project_links SET status = 'archived', archived_at = NOW()
               WHERE project_id = $1 AND memory_id = $2 AND status = 'active'
               RETURNING id, project_id, memory_id, status""",
            proj["id"], memory_id,
        )
    return dict(row) if row else None


async def _db_search(
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    query: str,
    mode: str = "all",
    types: list[str] | None = None,
) -> list[dict]:
    """Search knowledge and/or memories. Returns list of dicts."""
    search_types = types or ["knowledge", "memories"]
    embedding = await get_embedding(http, query)
    results = []

    if "knowledge" in search_types:
        if embedding is not None:
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            rows = await pool.fetch(
                """SELECT 'knowledge' AS type, k.id, k.title AS name, k.content,
                          k.status, 1 - (k.embedding <=> $1::vector) AS similarity
                   FROM knowledge k
                   WHERE k.status = 'active' AND k.embedding IS NOT NULL
                   ORDER BY k.embedding <=> $1::vector
                   LIMIT 20""",
                embedding_str,
            )
        else:
            rows = await pool.fetch(
                """SELECT 'knowledge' AS type, k.id, k.title AS name, k.content,
                          k.status, 0.0 AS similarity
                   FROM knowledge k
                   WHERE k.status = 'active'
                     AND (k.title ILIKE '%' || $1 || '%' OR k.content ILIKE '%' || $1 || '%')
                   ORDER BY k.updated_at DESC
                   LIMIT 20""",
                query,
            )
        results.extend(dict(r) for r in rows)

    if "memories" in search_types:
        if embedding is not None:
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            rows = await pool.fetch(
                """SELECT 'memory' AS type, m.id, m.name, m.content,
                          m.status, 1 - (m.embedding <=> $1::vector) AS similarity
                   FROM memories m
                   WHERE m.status = 'active' AND m.embedding IS NOT NULL
                   ORDER BY m.embedding <=> $1::vector
                   LIMIT 20""",
                embedding_str,
            )
        else:
            rows = await pool.fetch(
                """SELECT 'memory' AS type, m.id, m.name, m.content,
                          m.status, 0.0 AS similarity
                   FROM memories m
                   WHERE m.status = 'active'
                     AND (m.name ILIKE '%' || $1 || '%' OR m.content ILIKE '%' || $1 || '%')
                   ORDER BY m.updated_at DESC
                   LIMIT 20""",
                query,
            )
        results.extend(dict(r) for r in rows)

    return results


async def _db_bulk_delete(pool: asyncpg.Pool, items: list[dict]) -> dict:
    """Transactional bulk delete of archived items.
    Each item: {"type": "knowledge"|"memory"|"project", "id": <int>|<str>}
    Returns summary of deleted counts.
    """
    deleted = {"knowledge": 0, "memories": 0, "projects": 0}
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in items:
                item_type = item["type"]
                item_id = item["id"]
                if item_type == "knowledge":
                    row = await conn.fetchrow(
                        "SELECT status FROM knowledge WHERE id = $1", int(item_id)
                    )
                    if row is None or row["status"] != "archived":
                        raise ValueError(f"Knowledge {item_id} not found or not archived")
                    await conn.execute(
                        "DELETE FROM project_links WHERE knowledge_id = $1", int(item_id)
                    )
                    await conn.execute("DELETE FROM knowledge WHERE id = $1", int(item_id))
                    deleted["knowledge"] += 1
                elif item_type == "memory":
                    row = await conn.fetchrow(
                        "SELECT status FROM memories WHERE id = $1", int(item_id)
                    )
                    if row is None or row["status"] != "archived":
                        raise ValueError(f"Memory {item_id} not found or not archived")
                    await conn.execute(
                        "DELETE FROM project_links WHERE memory_id = $1", int(item_id)
                    )
                    await conn.execute("DELETE FROM memories WHERE id = $1", int(item_id))
                    deleted["memories"] += 1
                elif item_type == "project":
                    name = str(item_id)
                    if name == "general":
                        raise ValueError("Cannot delete the 'general' project")
                    row = await conn.fetchrow(
                        "SELECT id, status FROM projects WHERE name = $1", name
                    )
                    if row is None or row["status"] != "archived":
                        raise ValueError(f"Project '{name}' not found or not archived")
                    await conn.execute(
                        "DELETE FROM project_links WHERE project_id = $1", row["id"]
                    )
                    await conn.execute("DELETE FROM projects WHERE id = $1", row["id"])
                    deleted["projects"] += 1
                else:
                    raise ValueError(f"Unknown type: {item_type}")
    return deleted


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
    result = await _db_add_knowledge(app.pool, title, content, project, category, tags, url)
    return json.dumps([result], default=str, indent=2)


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
                   ORDER BY similarity DESC
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
                          k.url, k.tags, k.status, k.updated_at
                   FROM knowledge k
                   JOIN project_links pl ON pl.knowledge_id = k.id AND pl.status = 'active'
                   JOIN projects p ON p.id = pl.project_id AND p.name = $1
                   WHERE ($2::text IS NULL OR k.status = $2)
                     AND ($3::text IS NULL OR k.category = $3)
                     AND (k.title ILIKE '%' || $4 || '%' OR k.content ILIKE '%' || $4 || '%')
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
                     AND (k.title ILIKE '%' || $3 || '%' OR k.content ILIKE '%' || $3 || '%')
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
    try:
        result = await _db_save_memory(app.pool, memory_type, name, content, description, project)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps([result], default=str, indent=2)


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
                   ORDER BY similarity DESC
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
                          m.content, m.project, m.status, m.updated_at
                   FROM memories m
                   JOIN project_links pl ON pl.memory_id = m.id AND pl.status = 'active'
                   JOIN projects p ON p.id = pl.project_id AND p.name = $1
                   WHERE ($2::text IS NULL OR m.memory_type = $2)
                     AND ($3::text IS NULL OR m.status = $3)
                     AND (m.name ILIKE '%' || $4 || '%' OR m.content ILIKE '%' || $4 || '%')
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
                     AND (m.name ILIKE '%' || $3 || '%' OR m.content ILIKE '%' || $3 || '%')
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


# ---------------------------------------------------------------------------
# REST API endpoints (Starlette)
# ---------------------------------------------------------------------------

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_rest_app_ctx: AppContext | None = None


def _get_pool() -> asyncpg.Pool:
    assert _rest_app_ctx is not None, "REST app not initialized"
    return _rest_app_ctx.pool


def _get_http() -> httpx.AsyncClient:
    assert _rest_app_ctx is not None, "REST app not initialized"
    return _rest_app_ctx.http


def _json(data, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code)


def _err(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


# --- Knowledge REST ---


async def rest_knowledge_create(request: Request) -> JSONResponse:
    body = await request.json()
    title = body.get("title")
    content = body.get("content")
    if not title or not content:
        return _err("title and content are required", 400)
    try:
        result = await _db_add_knowledge(
            _get_pool(),
            title=title,
            content=content,
            project=body.get("project", "general"),
            category=body.get("category", "general"),
            tags=body.get("tags"),
            url=body.get("url"),
        )
        return _json(result, 201)
    except asyncpg.UniqueViolationError:
        return _err("Duplicate knowledge entry", 409)


async def rest_knowledge_update(request: Request) -> JSONResponse:
    kid = int(request.path_params["id"])
    body = await request.json()
    fields = {k: v for k, v in body.items() if k in {"title", "content", "category", "url", "tags", "project"}}
    if not fields:
        return _err("No valid fields to update", 400)
    result = await _db_update_knowledge(_get_pool(), kid, **fields)
    if result is None:
        return _err(f"Knowledge entry {kid} not found or not active", 404)
    return _json(result)


async def rest_knowledge_delete(request: Request) -> JSONResponse:
    kid = int(request.path_params["id"])
    try:
        result = await _db_hard_delete_knowledge(_get_pool(), kid)
    except ValueError as e:
        return _err(str(e), 400)
    if result is None:
        return _err(f"Knowledge entry {kid} not found", 404)
    return _json({"deleted": result})


# --- Memories REST ---


async def rest_memories_create(request: Request) -> JSONResponse:
    body = await request.json()
    memory_type = body.get("memory_type")
    name = body.get("name")
    content = body.get("content")
    if not memory_type or not name or not content:
        return _err("memory_type, name, and content are required", 400)
    try:
        result = await _db_save_memory(
            _get_pool(),
            memory_type=memory_type,
            name=name,
            content=content,
            description=body.get("description"),
            project=body.get("project", "general"),
        )
        return _json(result, 201)
    except ValueError as e:
        return _err(str(e), 400)
    except asyncpg.UniqueViolationError:
        return _err("Duplicate memory entry", 409)


async def rest_memories_update(request: Request) -> JSONResponse:
    mid = int(request.path_params["id"])
    body = await request.json()
    fields = {k: v for k, v in body.items() if k in {"memory_type", "name", "content", "description", "project"}}
    if not fields:
        return _err("No valid fields to update", 400)
    result = await _db_update_memory(_get_pool(), mid, **fields)
    if result is None:
        return _err(f"Memory {mid} not found or not active", 404)
    return _json(result)


async def rest_memories_delete(request: Request) -> JSONResponse:
    mid = int(request.path_params["id"])
    try:
        result = await _db_hard_delete_memory(_get_pool(), mid)
    except ValueError as e:
        return _err(str(e), 400)
    if result is None:
        return _err(f"Memory {mid} not found", 404)
    return _json({"deleted": result})


# --- Projects REST ---


async def rest_projects_create(request: Request) -> JSONResponse:
    body = await request.json()
    name = body.get("name")
    if not name:
        return _err("name is required", 400)
    orphan_policy = body.get("orphan_policy")
    if orphan_policy is not None and orphan_policy not in ("archive", "reassign"):
        return _err("orphan_policy must be 'archive' or 'reassign'", 400)
    try:
        row = await _get_pool().fetchrow(
            """INSERT INTO projects (name, description, repo_url, tech_stack, notes, orphan_policy)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id, name, status, orphan_policy, created_at""",
            name,
            body.get("description"),
            body.get("repo_url"),
            body.get("tech_stack", []),
            body.get("notes"),
            orphan_policy,
        )
        return _json(dict(row), 201)
    except asyncpg.UniqueViolationError:
        return _err(f"Project '{name}' already exists", 409)


async def rest_projects_update(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    body = await request.json()
    orphan_policy = body.get("orphan_policy")
    if orphan_policy is not None and orphan_policy not in ("archive", "reassign"):
        return _err("orphan_policy must be 'archive' or 'reassign'", 400)
    sets = []
    params = []
    idx = 1
    for col, val in [
        ("description", body.get("description")),
        ("repo_url", body.get("repo_url")),
        ("tech_stack", body.get("tech_stack")),
        ("notes", body.get("notes")),
        ("orphan_policy", orphan_policy),
    ]:
        if val is not None:
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
    if not sets:
        return _err("No fields to update", 400)
    sets.append("updated_at = NOW()")
    params.append(name)
    query = f"""UPDATE projects SET {', '.join(sets)}
                WHERE name = ${idx}
                RETURNING id, name, description, repo_url, tech_stack, notes,
                          status, orphan_policy, updated_at"""
    row = await _get_pool().fetchrow(query, *params)
    if row is None:
        return _err(f"Project '{name}' not found", 404)
    return _json(dict(row))


async def rest_projects_delete(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    try:
        result = await _db_hard_delete_project(_get_pool(), name)
    except ValueError as e:
        return _err(str(e), 400)
    if result is None:
        return _err(f"Project '{name}' not found", 404)
    return _json({"deleted": result})


# --- Archive / Unarchive REST ---


async def rest_archive(request: Request) -> JSONResponse:
    entity_type = request.path_params["type"]
    entity_id = request.path_params["id"]

    if entity_type == "project":
        # Full project archive with orphan handling (mirrors archive_project MCP tool)
        pool = _get_pool()
        name = entity_id
        async with pool.acquire() as conn:
            async with conn.transaction():
                proj = await conn.fetchrow(
                    "SELECT id, status, orphan_policy FROM projects WHERE name = $1",
                    name,
                )
                if proj is None:
                    return _err(f"Project '{name}' not found", 404)
                if proj["status"] == "system":
                    return _err(f"Cannot archive system project '{name}'", 400)
                if proj["status"] == "archived":
                    return _err(f"Project '{name}' is already archived", 400)

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

        return _json({
            "archived_project": name,
            "orphan_policy": policy,
            "orphaned_knowledge": len(orphaned_knowledge),
            "orphaned_memories": len(orphaned_memories),
        })

    # knowledge or memory
    if entity_type not in ("knowledge", "memory"):
        return _err("type must be 'knowledge', 'memory', or 'project'", 400)
    try:
        result = await _db_archive(_get_pool(), entity_type, int(entity_id))
    except ValueError as e:
        return _err(str(e), 400)
    if result is None:
        return _err(f"{entity_type} {entity_id} not found or already archived", 404)
    return _json(result)


async def rest_unarchive(request: Request) -> JSONResponse:
    entity_type = request.path_params["type"]
    entity_id = request.path_params["id"]

    if entity_type == "project":
        row = await _get_pool().fetchrow(
            """UPDATE projects SET status = 'active', updated_at = NOW()
               WHERE name = $1 AND status = 'archived'
               RETURNING id, name, status""",
            entity_id,
        )
        if row is None:
            return _err(f"Project '{entity_id}' not found or not archived", 404)
        return _json(dict(row))

    if entity_type not in ("knowledge", "memory"):
        return _err("type must be 'knowledge', 'memory', or 'project'", 400)
    try:
        result = await _db_unarchive(_get_pool(), entity_type, int(entity_id))
    except ValueError as e:
        return _err(str(e), 400)
    if result is None:
        return _err(f"{entity_type} {entity_id} not found or not archived", 404)
    return _json(result)


# --- Link REST ---


async def rest_link(request: Request) -> JSONResponse:
    body = await request.json()
    project_name = body.get("project")
    knowledge_id = body.get("knowledge_id")
    memory_id = body.get("memory_id")
    if not project_name:
        return _err("project is required", 400)
    try:
        result = await _db_link(
            _get_pool(),
            project_name=project_name,
            knowledge_id=int(knowledge_id) if knowledge_id is not None else None,
            memory_id=int(memory_id) if memory_id is not None else None,
        )
    except ValueError as e:
        return _err(str(e), 400)
    except LookupError as e:
        return _err(str(e), 404)
    if result is None:
        return _json({"message": "Link already exists"}, 200)
    return _json(result, 201)


async def rest_unlink(request: Request) -> JSONResponse:
    body = await request.json()
    project_name = body.get("project")
    knowledge_id = body.get("knowledge_id")
    memory_id = body.get("memory_id")
    if not project_name:
        return _err("project is required", 400)
    try:
        result = await _db_unlink(
            _get_pool(),
            project_name=project_name,
            knowledge_id=int(knowledge_id) if knowledge_id is not None else None,
            memory_id=int(memory_id) if memory_id is not None else None,
        )
    except ValueError as e:
        return _err(str(e), 400)
    except LookupError as e:
        return _err(str(e), 404)
    if result is None:
        return _err("Active link not found", 404)
    return _json(result)


# --- Search REST ---


async def rest_search(request: Request) -> JSONResponse:
    body = await request.json()
    query = body.get("query")
    if not query:
        return _err("query is required", 400)
    results = await _db_search(
        _get_pool(),
        _get_http(),
        query=query,
        mode=body.get("mode", "all"),
        types=body.get("types"),
    )
    return _json(results)


# --- Bulk delete REST ---


async def rest_bulk_delete(request: Request) -> JSONResponse:
    body = await request.json()
    items = body.get("items")
    if not items or not isinstance(items, list):
        return _err("items array is required", 400)
    try:
        result = await _db_bulk_delete(_get_pool(), items)
    except ValueError as e:
        return _err(str(e), 400)
    return _json(result)


# ---------------------------------------------------------------------------
# Combined ASGI app: REST + MCP
# ---------------------------------------------------------------------------

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


@asynccontextmanager
async def _rest_lifespan(app):
    global _rest_app_ctx
    pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=10,
    )
    http = httpx.AsyncClient()
    _rest_app_ctx = AppContext(pool=pool, http=http)
    try:
        yield
    finally:
        await http.aclose()
        await pool.close()


# Build the MCP ASGI app (handles /mcp path)
mcp_asgi = mcp.streamable_http_app()


async def _combined_app(scope, receive, send):
    """Route requests: /api/* -> REST app, /mcp* -> MCP app."""
    path = scope.get("path", "")
    if scope["type"] == "lifespan":
        await rest_app(scope, receive, send)
        return
    if path.startswith("/api"):
        await rest_app(scope, receive, send)
    else:
        await mcp_asgi(scope, receive, send)


rest_app = Starlette(
    routes=rest_routes,
    lifespan=_rest_lifespan,
)
rest_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app = _combined_app


if __name__ == "__main__":
    import asyncio
    import uvicorn

    print("[startup] Applying database schema...", flush=True)
    asyncio.run(_apply_schema())
    print("[startup] Starting combined MCP + REST server...", flush=True)
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=3001,
        log_level="info",
    )
