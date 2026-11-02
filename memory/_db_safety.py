"""Shared safety utilities for the throwaway test/demo databases.

The test suite and the two-client demo both TRUNCATE their target database on
startup to get a blank slate. To protect a real installation from an accidental
`pytest` (or demo) run against the shared application database, they target
dedicated databases (``memory_test`` / ``memory_demo``) and refuse to touch
anything whose name does not end in ``_test`` or ``_demo``.

This module has one pure guard (:func:`guard_db_name`) shared by the test suite
and the demo, plus :func:`ensure_database`, which creates the target database
and enables pgvector if missing. ``ensure_database`` shells out to the bundled
``psql`` client (the same one ``scripts/bootstrap-pg.sh`` uses) rather than the
bundled ``asyncpg``, because that ``asyncpg`` is a slimmer build without the
client-side helpers needed to run arbitrary SQL.
"""
from __future__ import annotations

import os
import subprocess


def guard_db_name(name: str) -> bool:
    """True iff ``name`` names a throwaway ``_test`` / ``_demo`` database.

    ``x_test`` and ``x_demo`` pass; ``memory`` (and anything else) is refused.
    This is the single guard shared by the test suite and the demo.
    """
    return name.endswith("_test") or name.endswith("_demo")


def database_name_from_url(url: str) -> str:
    """Return the database name parsed from an async SQLAlchemy URL string."""
    from sqlalchemy.engine import url as _sa_url

    return _sa_url.make_url(str(url)).database


def _locate_psql() -> str:
    """Path to a usable ``psql``: the bundled one, else ``psql`` on ``PATH``."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bundled = os.path.join(root, ".embedded-pg", "pginstall", "bin", "psql")
    if os.path.exists(bundled):
        return bundled
    return "psql"


async def ensure_database(url: str) -> None:
    """Create the database named in ``url`` (and enable pgvector) if missing.

    Connects to the maintenance ``postgres`` database to create the target
    database, then connects to it to enable the ``vector`` extension. Idempotent
    and never deletes or truncates anything -- it is only safe to call on a
    database that the guard has already approved.
    """
    from sqlalchemy.engine import url as _sa_url

    parsed = _sa_url.make_url(str(url))
    psql = _locate_psql()

    argv = [psql, "-U", str(parsed.username), "-v", "ON_ERROR_STOP=1"]
    if parsed.host is not None:
        argv += ["-h", str(parsed.host)]
    if parsed.port is not None:
        argv += ["-p", str(parsed.port)]
    env = dict(os.environ)
    env["PGPASSWORD"] = parsed.password or ""

    name = parsed.database

    # Create the database (if absent) from the maintenance 'postgres' db.
    # Passed via stdin (not -c) so psql processes the \gexec meta-command.
    create_sql = (
        "SELECT 'CREATE DATABASE \"" + name + "\"' "
        "WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '" + name
        + "') \\gexec\n"
    )
    subprocess.run(
        [*argv, "-d", "postgres"],
        env=env,
        input=create_sql,
        check=True,
        capture_output=True,
        text=True,
    )

    # Enable pgvector inside the target database.
    subprocess.run(
        [*argv, "-d", name],
        env=env,
        input="CREATE EXTENSION IF NOT EXISTS vector;\n",
        check=True,
        capture_output=True,
        text=True,
    )
