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


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=10,
    )
    # Run schema migrations on startup (all DDL is idempotent)
    if os.path.exists(SCHEMA_FILE):
        with open(SCHEMA_FILE) as f:
            schema_sql = f.read()
        try:
            async with pool.acquire() as conn:
                await conn.execute(schema_sql)
            print("[schema] init.sql applied successfully")
        except Exception as e:
            print(f"[schema] WARNING: init.sql failed: {e}")
            print("[schema] Attempting statements individually...")
            # Fall back to running each statement separately so partial
            # failures (e.g. IVFFlat index on empty table) don't block
            # the rest of the schema from being applied.
            statements = _split_sql(schema_sql)
            async with pool.acquire() as conn:
                for stmt in statements:
                    try:
                        await conn.execute(stmt)
                    except Exception as stmt_err:
                        print(f"[schema] Skipped statement: {stmt_err}")
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


# --- Shared resources tools ---


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


# --- Project tools ---


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


# --- Memory tools ---


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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
