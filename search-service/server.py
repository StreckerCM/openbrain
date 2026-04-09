import os

import asyncpg
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "openbrain")
DB_USER = os.environ.get("DB_USER", "openbrain")
DB_PASS = os.environ.get("DB_PASS", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"

pool: asyncpg.Pool | None = None
http_client: httpx.AsyncClient | None = None


async def startup():
    global pool, http_client
    pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=5,
    )
    http_client = httpx.AsyncClient()
    print("[search-service] Started", flush=True)


async def shutdown():
    global pool, http_client
    if http_client:
        await http_client.aclose()
    if pool:
        await pool.close()


async def get_embedding(text: str) -> list[float] | None:
    if not OPENAI_API_KEY or not http_client:
        return None
    try:
        resp = await http_client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"input": text, "model": EMBEDDING_MODEL},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[search-service] embedding failed: {e}", flush=True)
        return None


async def _search_semantic(table: str, embedding_str: str, limit: int) -> list[dict]:
    if table == "knowledge":
        rows = await pool.fetch(
            """SELECT id, project, category, title, content, tags,
                      1 - (embedding <=> $1::vector) AS similarity,
                      updated_at
               FROM knowledge
               WHERE embedding IS NOT NULL
               ORDER BY embedding <=> $1::vector
               LIMIT $2""",
            embedding_str, limit,
        )
    elif table == "shared_resources":
        rows = await pool.fetch(
            """SELECT id, resource_type, name, description, url, projects, metadata,
                      1 - (embedding <=> $1::vector) AS similarity,
                      updated_at
               FROM shared_resources
               WHERE embedding IS NOT NULL
               ORDER BY embedding <=> $1::vector
               LIMIT $2""",
            embedding_str, limit,
        )
    elif table == "memories":
        rows = await pool.fetch(
            """SELECT id, memory_type, name, description, content, project,
                      1 - (embedding <=> $1::vector) AS similarity,
                      updated_at
               FROM memories
               WHERE embedding IS NOT NULL
               ORDER BY embedding <=> $1::vector
               LIMIT $2""",
            embedding_str, limit,
        )
    else:
        return []
    return [dict(r) for r in rows]


async def _search_exact(table: str, query: str, limit: int) -> list[dict]:
    if table == "knowledge":
        rows = await pool.fetch(
            """SELECT id, project, category, title, content, tags, updated_at
               FROM knowledge
               WHERE title ILIKE '%' || $1 || '%' OR content ILIKE '%' || $1 || '%'
               ORDER BY updated_at DESC
               LIMIT $2""",
            query, limit,
        )
    elif table == "shared_resources":
        rows = await pool.fetch(
            """SELECT id, resource_type, name, description, url, projects, metadata, updated_at
               FROM shared_resources
               WHERE name ILIKE '%' || $1 || '%' OR description ILIKE '%' || $1 || '%'
               ORDER BY updated_at DESC
               LIMIT $2""",
            query, limit,
        )
    elif table == "memories":
        rows = await pool.fetch(
            """SELECT id, memory_type, name, description, content, project, updated_at
               FROM memories
               WHERE name ILIKE '%' || $1 || '%' OR content ILIKE '%' || $1 || '%' OR description ILIKE '%' || $1 || '%'
               ORDER BY updated_at DESC
               LIMIT $2""",
            query, limit,
        )
    else:
        return []
    return [dict(r) for r in rows]


def _serialize_rows(rows: list[dict]) -> list[dict]:
    """Convert non-serializable types (datetime, Decimal) to strings."""
    serialized = []
    for row in rows:
        clean = {}
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                clean[k] = v.isoformat()
            elif isinstance(v, (list, dict)):
                clean[k] = v
            else:
                clean[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
        serialized.append(clean)
    return serialized


async def search(request: Request) -> JSONResponse:
    if pool is None:
        return JSONResponse({"error": "Service not ready"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    query = body.get("query", "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    exact = body.get("exact", False)
    types = body.get("types", ["knowledge", "shared_resources", "memories"])
    valid_types = {"knowledge", "shared_resources", "memories"}
    types = [t for t in types if t in valid_types]

    limit_per_type = 20
    results = {}

    if exact:
        for t in types:
            results[t] = _serialize_rows(
                await _search_exact(t, query, limit_per_type)
            )
    else:
        embedding = await get_embedding(query)
        if embedding is not None:
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            for t in types:
                results[t] = _serialize_rows(
                    await _search_semantic(t, embedding_str, limit_per_type)
                )
        else:
            # Fallback to text search if embedding fails
            for t in types:
                results[t] = _serialize_rows(
                    await _search_exact(t, query, limit_per_type)
                )

    return JSONResponse({"results": results})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/search", search, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ],
    on_startup=[startup],
    on_shutdown=[shutdown],
)
