"""Initial migration for shared memory schema.

This mirrors ``memory.models`` exactly: projects, sessions, messages, the
append-only memory streams (facts/decisions/preferences), events, project
states, the optional pgvector embedding column, and the full-text / vector
search indexes. Running it produces the same schema as
``memory.database.init_schema()``.
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "20260927_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Optional semantic search needs the pgvector extension first.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- projects ----------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- sessions ----------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("source_session_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider", "source_session_id", name="uq_session_provider_source"
        ),
    )

    # --- messages ----------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(36)),  # denormalised
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("order", sa.Integer, nullable=False),
        sa.Column(
            "original_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "ingestion_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON),
        sa.Column("source_client", sa.String(64)),
        sa.Column("search_embedding", Vector(dim=512), nullable=True),
        sa.UniqueConstraint(
            "session_id", "source_id", name="uq_msg_session_source"
        ),
        sa.UniqueConstraint("session_id", "order", name="uq_msg_session_order"),
    )

    # --- memory streams (append-only, supersede without delete) ------------
    for table in ("facts", "decisions", "preferences"):
        op.create_table(
            table,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            ),
            sa.Column("project_id", sa.String(36)),  # denormalised
            sa.Column("key", sa.String(255), nullable=False),
            sa.Column("value", sa.Text, nullable=False),
            sa.Column("source_client", sa.String(64)),
            sa.Column("source_id", sa.String(128)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # --- events ------------------------------------------------------------
    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
        ),
        sa.Column("project_id", sa.String(36)),  # denormalised
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON),
        sa.Column("source_client", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- project states (append-only current state snapshot) ---------------
    op.create_table(
        "project_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="SET NULL"),
        ),
        sa.Column("state_json", sa.JSON, nullable=False),
        sa.Column("source_client", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- search indexes ----------------------------------------------------
    # Full-text search over the message body (REGCONFIG literal needs raw DDL).
    op.create_index(
        "idx_messages_fts",
        "messages",
        [sa.text("to_tsvector('english', content)")],
        postgresql_using="gin",
    )
    # Cosine-similarity index for optional vector search over embeddings.
    op.create_index(
        "idx_messages_embedding",
        "messages",
        ["search_embedding"],
        postgresql_using="ivfflat",
        postgresql_ops={"search_embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_messages_embedding", table_name="messages")
    op.drop_index("idx_messages_fts", table_name="messages")
    op.drop_table("project_states")
    op.drop_table("events")
    op.drop_table("preferences")
    op.drop_table("decisions")
    op.drop_table("facts")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("projects")
    op.execute("DROP EXTENSION IF EXISTS vector")
