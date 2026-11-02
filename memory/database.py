"""Database connection, session management, and schema bootstrapping.

All application data lives in PostgreSQL. This module owns the async engine and
session factory, exposes a small context manager for handler code, and provides
``init_db`` which brings the schema up to date (creating the pgvector extension
and all tables).

Configuration (environment variables):

* ``DATABASE_URL``         -- async SQLAlchemy URL, e.g.
  ``postgresql+asyncpg://memory:memory@localhost:5433/memory``.
* ``MEMORY_USE_EMBEDDINGS``-- when ``1``, messages are also embedded (via the
  optional local embedding function in :mod:`memory.embeddings`) and stored in a
  pgvector column so semantic search is available. Disabled by default; retrieval
  then relies on PostgreSQL full-text search.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# Default points at the bundled embedded PostgreSQL. Override with DATABASE_URL
# (e.g. when running against the docker-compose stack or a hosted Postgres).
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://memory:memory@localhost:5433/memory"
)

DATABASE_URL = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
USE_EMBEDDINGS = os.environ.get("MEMORY_USE_EMBEDDINGS", "0") == "1"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_size=int(os.environ.get("PG_POOL_SIZE", "10")),
    max_overflow=int(os.environ.get("PG_MAX_OVERFLOW", "10")),
    pool_recycle=1800,
    pool_pre_ping=True,
)

AsyncSessionLocal = sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


@asynccontextmanager
async def get_db() -> AsyncSession:
    """Yield a scoped async DB session and always close it."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_extensions() -> None:
    """Enable pgvector (idempotent). Must run before tables using it exist."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


async def init_schema() -> None:
    """Create the pgvector extension, all tables, and supporting indexes.

    Idempotent and safe to run on every start. This is the dev/quickstart path.
    In production you may instead run the Alembic migrations with
    ``alembic upgrade head`` -- the two produce the same schema.

    Note: the full-text search index uses ``to_tsvector('english', content)``
    which SQLAlchemy cannot compile inside an ORM ``Index`` (REGCONFIG literal),
    so we create it with explicit DDL here and in the Alembic migration.
    """
    from .models import Base

    await init_extensions()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Full-text search GIN index over the message body.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_messages_fts "
                "ON messages USING GIN (to_tsvector('english', content))"
            )
        )
        # Cosine-similarity index for optional vector search over embeddings.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_messages_embedding "
                "ON messages USING ivfflat (search_embedding vector_cosine_ops)"
            )
        )


async def health() -> bool:
    """True if the database is reachable."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

