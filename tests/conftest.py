"""Shared pytest fixtures for the shared-memory integration tests.

The suite runs against a *real* PostgreSQL instance (the bundled embedded one by
default). It always targets a dedicated ``memory_test`` database -- never the
application's ``memory`` database -- so running ``pytest`` on a real install can
never erase shared memory.

The target database is read from ``TEST_DATABASE_URL`` (default below). It is
assigned to ``DATABASE_URL`` *unconditionally* -- not with ``setdefault`` -- and
before anything from :mod:`memory` is imported, so an exported ``DATABASE_URL``
can never redirect the suite to a real database.
"""
from __future__ import annotations

import os

# Tests must never touch the application database. Always target memory_test,
# read from TEST_DATABASE_URL. Assign unconditionally and before importing
# anything from memory, so an exported DATABASE_URL cannot redirect us.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://memory:memory@localhost:5433/memory_test",
)

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from memory._db_safety import (  # noqa: E402
    database_name_from_url,
    guard_db_name,
    ensure_database,
)
from memory.database import get_db, init_schema  # noqa: E402
from memory.mcp_server import server  # noqa: E402


# ---------------------------------------------------------------------------
# MCP tool-call helpers
#
# MCP tools do NOT raise on business errors -- they return
# {"is_error": true, "code": ..., "error": ...} inside structured_content.
# So ``call`` asserts that no error was reported, while ``call_raw`` returns
# whatever the tool returned (so tests can assert on the error code).
# ---------------------------------------------------------------------------
async def call_tool(tool_name: str, **args) -> dict:
    result = await server.call_tool(tool_name, args)
    sc = result.structured_content or {}
    if sc.get("is_error"):
        raise AssertionError(f"tool {tool_name!r} unexpectedly errored: {sc}")
    return sc


async def call_tool_raw(tool_name: str, **args) -> dict:
    result = await server.call_tool(tool_name, args)
    return result.structured_content or {}


@pytest.fixture
def call():
    return call_tool


@pytest.fixture
def call_raw():
    return call_tool_raw


# ---------------------------------------------------------------------------
# Database lifecycle
#
# Every operation that mutates the database is preceded by ``_refuse_unsafe``,
# which refuses (via ``pytest.exit``) unless the target database name ends in
# ``_test`` or ``_demo``. This protects a real database from accidental TRUNCATE.
# ---------------------------------------------------------------------------
_TRUNCATE_SQL = """
    TRUNCATE project_states, events, facts, decisions, preferences,
             messages, sessions, projects
    RESTART IDENTITY CASCADE
"""


def _refuse_unsafe(url: str) -> None:
    """``pytest.exit`` unless ``url`` names a ``_test`` / ``_demo`` database."""
    name = database_name_from_url(url)
    if not guard_db_name(name):
        pytest.exit(
            f"Refusing to run the test suite against {url!r}: database "
            f"{name!r} does not end in '_test' or '_demo'. Aborting to protect "
            f"your shared memory.",
            returncode=2,
        )


async def _reset_blank_slate(url: str) -> None:
    """Guard, then TRUNCATE every table for a blank slate (idempotent)."""
    _refuse_unsafe(url)  # guard immediately before every TRUNCATE
    async with get_db() as db:
        await db.execute(text(_TRUNCATE_SQL))
        await db.commit()


@pytest.fixture(autouse=True)
async def clean_db():
    """Ensure the schema exists and give each test a blank slate.

    The guard runs before connecting or creating anything, and again right
    before each TRUNCATE, so a database pointed at by mistake can never be
    mutated.
    """
    url = os.environ["DATABASE_URL"]
    _refuse_unsafe(url)  # refuse before connecting or creating anything
    await ensure_database(url)
    await init_schema()
    await _reset_blank_slate(url)
    yield
    await _reset_blank_slate(url)


@pytest.fixture
async def db() -> AsyncSession:
    """A fresh async session for tests that need to inspect the DB directly."""
    async with get_db() as session:
        yield session
