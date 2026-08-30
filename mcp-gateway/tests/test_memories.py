"""Tests for the memory MCP tools."""
import json

import pytest

import db
import server


class _RowPool:
    """Stands in for asyncpg.Pool, recording the query it was handed.

    _db_update_memory builds its SET clause dynamically, so the query text is
    the only place the "which columns did we actually touch" decision is
    observable without a live Postgres.
    """

    def __init__(self, row):
        self._row = row
        self.query = None
        self.params = None

    async def fetchrow(self, query, *params):
        self.query = query
        self.params = params
        return self._row


class _NullPool:
    """Stands in for asyncpg.Pool when the row is expected to be missing."""

    async def fetchrow(self, query, *params):
        return None


@pytest.fixture
def install_pool(monkeypatch):
    """Point the module-level app context at a fake pool.

    `_get_app_ctx` ignores its `ctx` argument and reads `_rest_app_ctx`, so this
    is how a tool is given a database in isolation.
    """

    def _install(pool):
        monkeypatch.setattr(
            server, "_rest_app_ctx", server.AppContext(pool=pool, http=None)
        )

    return _install


def _row(**overrides):
    row = {
        "id": 6,
        "memory_type": "reference",
        "name": "mcp-config-resolution",
        "description": "where MCP servers are defined",
        "content": "...",
        "project": "General",
        "status": "active",
        "updated_at": "2026-08-30 04:49:54+00:00",
    }
    row.update(overrides)
    return row


async def test_update_memory_changes_only_the_fields_given(install_pool):
    """Partial update: omitted fields must not appear in the SET clause, or an
    agent correcting a description would blank the content it never passed.
    """
    pool = _RowPool(_row(description="corrected"))
    install_pool(pool)

    result = json.loads(await server.update_memory(id=6, description="corrected"))

    assert result["id"] == 6
    assert "description = $1" in pool.query
    assert "content" not in pool.query.split("WHERE")[0]
    assert pool.params == ("corrected", 6)


async def test_update_memory_clears_the_embedding_on_a_text_change(install_pool):
    """Re-embedding is driven by setting embedding = NULL. Without it a
    corrected memory keeps matching semantic searches on its old wording.
    """
    pool = _RowPool(_row())
    install_pool(pool)

    await server.update_memory(id=6, content="new text")

    assert "embedding = NULL" in pool.query


async def test_update_memory_leaves_the_embedding_alone_for_a_metadata_change(
    install_pool,
):
    """`project` is a provenance label, not embedded text. Nulling the vector
    for it would force a needless re-embed on every relabel.
    """
    pool = _RowPool(_row(project="OpenBrain"))
    install_pool(pool)

    await server.update_memory(id=6, project="OpenBrain")

    assert "embedding = NULL" not in pool.query


async def test_update_memory_rejects_an_invalid_memory_type(install_pool):
    """The type vocabulary is enforced on insert by _db_save_memory. An update
    path that skipped it would be a back door: recall_memory's memory_type
    filter and the web UI facets stop matching a row set outside the set.
    """
    pool = _RowPool(_row())
    install_pool(pool)

    result = json.loads(await server.update_memory(id=6, memory_type="note"))

    assert "memory_type must be one of" in result["error"]
    assert pool.query is None, "must not reach the database"


async def test_update_memory_accepts_the_memory_id_alias(install_pool):
    """LLM callers pass the qualified form learned from link_to_project."""
    pool = _RowPool(_row())
    install_pool(pool)

    result = json.loads(await server.update_memory(memory_id=6, name="renamed"))

    assert result["id"] == 6
    assert pool.params[-1] == 6


async def test_update_memory_rejects_conflicting_id_and_alias(install_pool):
    pool = _RowPool(_row())
    install_pool(pool)

    result = json.loads(await server.update_memory(id=6, memory_id=7, name="x"))

    assert "Conflicting values" in result["error"]
    assert pool.query is None, "must not reach the database"


async def test_update_memory_requires_an_id(install_pool):
    install_pool(_RowPool(_row()))

    result = json.loads(await server.update_memory(name="x"))

    assert "Must provide 'id'" in result["error"]


async def test_update_memory_reports_a_missing_row_as_an_error(install_pool):
    """Archived and nonexistent rows both fall out of the `status = 'active'`
    predicate, so the tool cannot tell them apart — the message says so.
    """
    install_pool(_NullPool())

    result = json.loads(await server.update_memory(id=999, name="x"))

    assert "not found" in result["error"]


async def test_update_memory_with_no_fields_is_an_error(install_pool):
    """_db_update_memory returns None rather than issuing a no-op UPDATE."""
    install_pool(_RowPool(_row()))

    result = json.loads(await server.update_memory(id=6))

    assert "no fields to update" in result["error"]


async def test_db_update_memory_raises_on_an_invalid_type():
    """The guard lives in db.py so the REST front door inherits it too."""
    with pytest.raises(ValueError, match="memory_type must be one of"):
        await db._db_update_memory(_RowPool(_row()), 6, memory_type="note")
