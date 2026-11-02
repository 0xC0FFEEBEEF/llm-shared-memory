"""SQLAlchemy ORM models for the shared memory system.

All application data lives in a single PostgreSQL database. The design is
append-by-default with *supersede without delete*: facts, decisions,
preferences, events and project states are stored as an append-only stream, so a
changed value or decision simply appends a newer row; the history of the old one
is preserved and can be queried.

Optional semantic search: when ``MEMORY_USE_EMBEDDINGS=1``, each message also
stores a ``search_embedding`` in a pgvector column. Retrieval otherwise relies on
PostgreSQL full-text search over an indexed ``tsvector`` expression.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from .database import USE_EMBEDDINGS

# Dimensionality of the optional local embedding. Kept modest; only used when
# MEMORY_USE_EMBEDDINGS=1.
EMBEDDING_DIM = 512


class Base(DeclarativeBase):
    pass


def _now() -> object:
    return func.now()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    sessions = relationship(
        "Session", back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Project {self.name} id={self.id}>"


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "provider", "source_session_id", name="uq_session_provider_source"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project = relationship("Project", back_populates="sessions")
    messages = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    """A single raw conversation message. Append-only per (session, source_id).

    ``search_vector`` gives real full-text search (stemming + ranking via
    ``ts_rank``). ``search_embedding`` is populated only when embeddings are
    enabled; otherwise it stays NULL and retrieval uses full-text search.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
    session = relationship("Session", back_populates="messages")
    project_id: Mapped[str | None] = mapped_column(String(36))  # denormalised
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    original_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingestion_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_client: Mapped[str | None] = mapped_column(String(64))
    search_embedding: Mapped[object | None] = mapped_column(
        Vector(dim=EMBEDDING_DIM), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("session_id", "source_id", name="uq_msg_session_source"),
        UniqueConstraint("session_id", "order", name="uq_msg_session_order"),
        # Full-text search over the message body (REGCONFIG literal needs raw
        # DDL in the migration; declaring it here keeps `alembic check` honest).
        Index(
            "idx_messages_fts",
            text("to_tsvector('english', content)"),
            postgresql_using="gin",
        ),
        # Cosine-similarity index for optional pgvector search over embeddings.
        Index(
            "idx_messages_embedding",
            "search_embedding",
            postgresql_using="ivfflat",
            postgresql_ops={"search_embedding": "vector_cosine_ops"},
        ),
    )


class MemoryBase(Base):
    """Common columns for the append-only 'memory' streams."""

    __abstract__ = True

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
    project_id: Mapped[str | None] = mapped_column(String(36))  # denormalised
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_client: Mapped[str | None] = mapped_column(String(64))
    source_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Fact(MemoryBase):
    __tablename__ = "facts"


class Decision(MemoryBase):
    __tablename__ = "decisions"


class Preference(MemoryBase):
    """A user preference (a subtype of fact, tracked separately for clarity)."""

    __tablename__ = "preferences"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
    project_id: Mapped[str | None] = mapped_column(String(36))  # denormalised
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_client: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectState(Base):
    """An append-only snapshot of the current state of a project."""

    __tablename__ = "project_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL")
    )
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_client: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

