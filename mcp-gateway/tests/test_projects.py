"""Tests for the project MCP tools."""
import json

import asyncpg
import pytest

import server


class _RaisingPool:
    """Stands in for asyncpg.Pool, raising on fetchrow.

    `projects.name` is the only UNIQUE column in the schema (`init.sql:22`), so
    a duplicate name is the one place asyncpg raises UniqueViolationError out of
    this tool. Faking the pool keeps the test to the behavior under change —
    translating that exception into a tool-shaped error — without standing up
    Postgres. The exception is the real asyncpg type, not a stub.
    """

    def __init__(self, exc):
        self._exc = exc

    async def fetchrow(self, *args, **kwargs):
        raise self._exc


class _RowPool:
    """Stands in for asyncpg.Pool, returning a fixed row."""

    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *args, **kwargs):
        return self._row


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


async def test_add_project_reports_a_duplicate_name_as_an_error(install_pool):
    """Regression: add_project let asyncpg.UniqueViolationError escape, so an
    agent registering an existing project got an unhandled database exception
    instead of the {"error": ...} every other path in the tool returns.
    rest_projects_create has always returned a clean 409 for the same case.
    """
    install_pool(
        _RaisingPool(
            asyncpg.UniqueViolationError(
                'duplicate key value violates unique constraint "projects_name_key"'
            )
        )
    )

    result = await server.add_project(name="openbrain")

    assert json.loads(result) == {"error": "Project 'openbrain' already exists"}


async def test_add_project_returns_the_created_row(install_pool):
    """Guard: the duplicate-name handling must not swallow the success path."""
    install_pool(
        _RowPool(
            {
                "id": 7,
                "name": "openbrain",
                "status": "active",
                "orphan_policy": None,
                "created_at": "2026-08-22T00:00:00Z",
            }
        )
    )

    result = await server.add_project(name="openbrain")

    assert json.loads(result) == [
        {
            "id": 7,
            "name": "openbrain",
            "status": "active",
            "orphan_policy": None,
            "created_at": "2026-08-22T00:00:00Z",
        }
    ]


async def test_add_project_rejects_an_invalid_orphan_policy(install_pool):
    """Guard: validation runs before the insert is attempted."""
    install_pool(_RaisingPool(AssertionError("pool must not be touched")))

    result = await server.add_project(name="openbrain", orphan_policy="delete")

    assert json.loads(result) == {
        "error": "orphan_policy must be 'archive' or 'reassign'"
    }
