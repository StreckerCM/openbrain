"""Shared database access, used by both the MCP tools and the REST endpoints.

Moved verbatim out of server.py, which had grown to 2,100 lines with this
layer buried in the middle. The MCP tools historically reimplemented much of
it inline rather than calling it — 32 raw SQL sites against the REST
handlers' 6 — because a helper three hundred lines up in one large file is a
discovery problem, not a discipline problem. An importable module with an
obvious name makes reuse the path of least resistance.

The `_db_` prefix is kept so this move changes no call sites. These functions
are this module's public API despite the leading underscore; renaming them is
follow-on work, deliberately not bundled with a pure move.

VALID_MEMORY_TYPES lives here rather than in server.py because it constrains
what `_db_save_memory` will write. In server.py it was defined 800 lines
BELOW its first use — legal only because the reference sits inside a function
body, and a hazard waiting for someone to hoist that call to module scope.
"""
import os

import asyncpg
import httpx

from embeddings import get_embedding

ORPHAN_POLICY = os.environ.get("ORPHAN_POLICY", "archive")
VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}


async def _db_add_knowledge(
    pool: asyncpg.Pool,
    title: str,
    content: str,
    project: str = "General",
    category: str = "General",
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
    text_fields = {"title", "content", "category", "project"}
    sets = []
    params = []
    idx = 1
    needs_reembed = False
    for col, val in fields.items():
        if col in allowed and val is not None:
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
            if col in text_fields:
                needs_reembed = True
    if not sets:
        return None
    if needs_reembed:
        sets.append("embedding = NULL")
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
    project: str = "General",
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
    text_fields = {"name", "content", "description", "memory_type"}
    sets = []
    params = []
    idx = 1
    needs_reembed = False
    for col, val in fields.items():
        if col in allowed and val is not None:
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
            if col in text_fields:
                needs_reembed = True
    if not sets:
        return None
    if needs_reembed:
        sets.append("embedding = NULL")
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
    if name == "General":
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
    embedding = None
    if mode != "exact":
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
                    if name == "General":
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


async def _db_archive_project(pool: asyncpg.Pool, name: str) -> dict:
    """Archive a project with full orphan cascade logic.

    Returns a result dict with archived_project, orphan_policy, and counts.
    Raises ValueError if the project is not found, is a system project, or
    is already archived.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            proj = await conn.fetchrow(
                "SELECT id, status, orphan_policy FROM projects WHERE name = $1",
                name,
            )
            if proj is None:
                raise ValueError(f"Project '{name}' not found")
            if proj["status"] == "system":
                raise ValueError(f"Cannot archive system project '{name}'")
            if proj["status"] == "archived":
                raise ValueError(f"Project '{name}' is already archived")

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
                "SELECT id FROM projects WHERE name = 'General'"
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

    return {
        "archived_project": name,
        "orphan_policy": policy,
        "orphaned_knowledge": len(orphaned_knowledge),
        "orphaned_memories": len(orphaned_memories),
    }
