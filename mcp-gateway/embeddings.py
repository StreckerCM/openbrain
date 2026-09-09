"""Embedding generation for semantic search.

Split out of server.py so the data layer can call it without importing the
MCP tool definitions. Kept separate from db.py because generating an
embedding is an outbound HTTP call to a model provider, not database access
— and because a local embedding backend would land here rather than there.
"""
import os

import httpx

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"


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
