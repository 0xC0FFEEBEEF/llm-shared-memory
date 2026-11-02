"""Alembic migration environment.

Two things differ from the stock ``alembic init`` template:

* The database URL is read from the ``DATABASE_URL`` environment variable when
  present (tests, Docker, hosted Postgres all set it) and falls back to
  ``alembic.ini``'s default. This lets a single ``alembic.ini`` work everywhere.
* The target metadata comes from the ORM (``memory.models``) so autogeneration
  and ``alembic current`` stay in sync with the schema defined in code.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

# --- Standard Alembic config + logging -------------------------------------
config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except OSError:
        # Config file is optional (e.g. when invoked programmatically in tests).
        pass

# --- Database URL: environment override, else alembic.ini default ----------
db_url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")

# Migrations run through alembic's *synchronous* engine, but the runtime app
# uses asyncpg. Point migrations at the same database via the pure-Python pg8000
# driver instead, preserving host/port/user/password/database.
from sqlalchemy.engine import url as _sa_url  # noqa: E402

_conn_url = _sa_url.make_url(str(db_url)).set(drivername="postgresql+pg8000")
config.set_main_option("sqlalchemy.url", str(_conn_url))

# --- Target metadata from the ORM ------------------------------------------
from memory.models import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live database).

    The runtime application speaks asyncpg, but alembic's ``Context`` drives DDL
    synchronously and alembic 1.20's ``context.begin_transaction()`` proxy is
    sync-only (it errors against asyncpg). So the URL is rewritten (above) to the
    pure-Python ``pg8000`` driver and migrations use a plain *synchronous*
    engine here. This keeps migrations in sync with the app without pulling in
    any C extension or async machinery.
    """
    from sqlalchemy import create_engine

    url = config.get_main_option("sqlalchemy.url")
    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
