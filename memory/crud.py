"""Database CRUD logic for the shared memory system.

Everything here is synchronous SQL issued against an async session. The MCP
tools are thin wrappers around these functions.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from . import crud_secret
from . import embeddings as embedding_lib
from .models import (
    Decision,
    Event,
    Fact,
    Message,
    Preference,
    Project,
    ProjectState,
    Session,
)
from .schemas import MessageCreate

# Map memory kind -> model
_MEMORY_MODELS: dict[str, type] = {
    "fact": Fact,
    "decision": Decision,
    "preference": Preference,
}


def new_id() -> str:
    return str(uuid.uuid4())


def vector_literal(vec: Iterable[float]) -> str:
    """Serialise a float vector to a Postgres ``vector`` input literal.

    asyncpg does not (in every version) know how to bind a raw Python list as a
    pgvector ``vector`` parameter, so we format it ourselves as the ``{a,b,c}``
    literal and cast it with ``::vector`` in the query. Bound params are not
    subject to SQL injection, so this is safe.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# ---------------------------------------------------------------------------
# Errors surfaced to MCP clients as structured results
# ---------------------------------------------------------------------------
class MemoryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotFound(MemoryError):
    def __init__(self, message: str) -> None:
        super().__init__("not_found", message)


class Conflict(MemoryError):
    def __init__(self, message: str) -> None:
        super().__init__("conflict", message)


class PayloadError(MemoryError):
    def __init__(self, message: str) -> None:
        super().__init__("bad_payload", message)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
async def create_project(
    db: AsyncSession, name: str, description: str | None, source_client: str | None
) -> Project:
    project = Project(
        id=new_id(),
        name=name,
        description=description,
    )
    db.add(project)
    await db.flush()
    return project


async def get_project(db: AsyncSession, project_id: str) -> Project | None:
    return await db.get(Project, project_id)


async def list_projects(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(Project).order_by(Project.created_at.desc())
    )
    projects = list(result.scalars().all())
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "created_at": p.created_at,
        }
        for p in projects
    ]


async def get_or_create_session(
    db: AsyncSession,
    provider: str,
    source_session_id: str,
    title: str | None = None,
    project_id: str | None = None,
) -> Session:
    """Look up a session by (provider, source_session_id); create if absent."""
    result = await db.execute(
        select(Session).where(
            Session.provider == provider,
            Session.source_session_id == source_session_id,
        )
    )
    session = result.scalar_one_or_none()
    if session:
        if project_id and not session.project_id:
            session.project_id = project_id
        if title and not session.title:
            session.title = title
        await db.flush()
        return session
    session = Session(
        id=new_id(),
        provider=provider,
        source_session_id=source_session_id,
        title=title,
        project_id=project_id,
    )
    db.add(session)
    await db.flush()
    return session


async def get_session(db: AsyncSession, session_id: str) -> Session | None:
    return await db.get(Session, session_id)


# ---------------------------------------------------------------------------
# Messages (idempotent append)
# ---------------------------------------------------------------------------
async def append_message(
    db: AsyncSession, session_id: str, msg_in: MessageCreate
) -> dict[str, Any]:
    session = await get_session(db, session_id)
    if not session:
        raise NotFound(f"Session {session_id} not found")

    provider = msg_in.provider or session.provider
    source_client = msg_in.source_client or provider

    # Reject credential material before it ever touches the DB.
    if crud_secret.contains_credential(
        msg_in.content, msg_in.payload, extra_keys={"password"}
    ):
        raise PayloadError(
            "Refused to store a message that appears to contain credentials"
        )

    existing = await db.execute(
        select(Message).where(
            Message.session_id == session_id,
            Message.source_id == msg_in.source_message_id,
        )
    )
    existing = existing.scalar_one_or_none()
    if existing:
        # Idempotent retry: identical content is a no-op returning the same row.
        same = (
            existing.provider == provider
            and existing.order == msg_in.order
            and existing.role == msg_in.role
            and existing.original_timestamp == msg_in.original_timestamp
            and existing.content == msg_in.content
        )
        if not same:
            raise Conflict(
                "source_id "
                + msg_in.source_message_id
                + " already exists with different data"
            )
        return {
            "session_id": session_id,
            "message_id": existing.id,
            "created": False,
            "conflict": False,
            "order": existing.order,
            "source_id": existing.source_id,
        }

    message = Message(
        id=new_id(),
        session_id=session_id,
        project_id=session.project_id,
        provider=provider,
        role=msg_in.role,
        order=msg_in.order,
        original_timestamp=msg_in.original_timestamp,
        source_id=msg_in.source_message_id,
        content=msg_in.content,
        payload=msg_in.payload,
        source_client=source_client,
    )
    # Optional embedding (deterministic local model) for semantic search.
    model = embedding_lib.get_embedding_model()
    if model is not None:
        message.search_embedding = model.embed(msg_in.content)
    db.add(message)
    await db.flush()
    return {
        "session_id": session_id,
        "message_id": message.id,
        "created": True,
        "conflict": False,
        "order": message.order,
        "source_id": message.source_id,
    }


async def list_messages(
    db: AsyncSession,
    session_id: str,
    offset: int = 0,
    limit: int = 50,
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.order, Message.ingestion_timestamp)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_messages(db: AsyncSession, session_id: str) -> int:
    result = await db.execute(
        select(Message).where(Message.session_id == session_id)
    )
    return len(result.scalars().all())


async def get_message(db: AsyncSession, message_id: str) -> Message | None:
    return await db.get(Message, message_id)


# ---------------------------------------------------------------------------
# Search across all raw history
# ---------------------------------------------------------------------------
async def search_messages(
    db: AsyncSession,
    query: str,
    limit: int = 25,
    source_client: str | None = None,
) -> list[dict[str, Any]]:
    """Search across *all* raw conversation history (every session/provider).

    Full-text search is always used. When embeddings are enabled we additionally
    rank by cosine similarity to the query vector (a hybrid).
    """
    client_filter = ""
    params: dict[str, Any] = {"q_limit": limit, "query": query}
    if source_client:
        client_filter = "AND message.source_client = :src"
        params["src"] = source_client

    model = embedding_lib.get_embedding_model()
    if model is not None:
        vec = model.embed(query)
        sql = """
            SELECT
                id, session_id, project_id, provider, role, "order",
                original_timestamp, source_id, content, payload, source_client,
                text_score, vec_score, (text_score + vec_score) AS score
            FROM (
                SELECT
                    message.id AS id,
                    message.session_id AS session_id,
                    message.project_id AS project_id,
                    message.provider AS provider,
                    message.role AS role,
                    message.order AS "order",
                    message.original_timestamp AS original_timestamp,
                    message.source_id AS source_id,
                    message.content AS content,
                    message.payload AS payload,
                    message.source_client AS source_client,
                    ts_rank(to_tsvector('english', message.content),
                            plainto_tsquery('english', :query)) AS text_score,
                    -1 * (message.search_embedding <-> CAST(:vec AS vector)) AS vec_score
                FROM (
                    SELECT id, session_id, project_id, provider, role, "order",
                           original_timestamp, source_id, content, payload,
                           source_client, search_embedding,
                           to_tsvector('english', content) AS tsv
                    FROM messages
                ) message
                WHERE message.search_embedding IS NOT NULL
                      {cf}
            ) scored
            ORDER BY score DESC
            LIMIT :q_limit
        """.format(cf=client_filter)
        params["vec"] = vector_literal(vec)
    else:
        sql = """
            SELECT
                message.id AS id,
                message.session_id AS session_id,
                message.project_id AS project_id,
                message.provider AS provider,
                message.role AS role,
                message.order AS "order",
                message.original_timestamp AS original_timestamp,
                message.source_id AS source_id,
                message.content AS content,
                message.payload AS payload,
                message.source_client AS source_client,
                ts_rank(to_tsvector('english', message.content),
                        plainto_tsquery('english', :query)) AS score
            FROM messages message
            WHERE to_tsvector('english', message.content) @@
                  plainto_tsquery('english', :query)
                  {cf}
            ORDER BY score DESC
            LIMIT :q_limit
        """.format(cf=client_filter)

    result = await db.execute(text(sql), params)
    return [dict(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Memory streams: facts / decisions / preferences
# ---------------------------------------------------------------------------
async def record_memory(
    db: AsyncSession,
    kind: str,
    key: str,
    value: str,
    session_id: str | None,
    project_id: str | None,
    source_client: str | None,
    source_id: str | None,
) -> dict[str, Any]:
    if kind not in _MEMORY_MODELS:
        raise MemoryError("bad_kind", f"Unknown memory kind: {kind}")

    model = _MEMORY_MODELS[kind]
    if session_id:
        session = await get_session(db, session_id)
        if not session:
            raise NotFound(f"Session {session_id} not found")
        project_id = project_id or session.project_id

    record = model(
        id=new_id(),
        session_id=session_id,
        project_id=project_id,
        key=key,
        value=value,
        source_client=source_client,
        source_id=source_id,
    )
    db.add(record)
    await db.flush()
    return {
        "id": record.id,
        "kind": kind,
        "key": record.key,
        "value": record.value,
        "source_client": record.source_client,
        "source_id": record.source_id,
        "session_id": session_id,
        "project_id": project_id,
        "created_at": record.created_at,
    }


def _in_sql(column: str, values: Iterable[Any]) -> tuple[str, dict[str, Any]]:
    """Build a safe `column IN (:v0, :v1, ...)` with bound params."""
    values = list(values)
    if not values:
        return "(false)", {}
    placeholders = ", ".join(f":v{i}" for i in range(len(values)))
    params = {f"v{i}": v for i, v in enumerate(values)}
    return f"({column} IN ({placeholders}))", params


async def _latest_per_key(
    db: AsyncSession, model: type, where: str, params: dict
) -> list[dict[str, Any]]:
    """Return the most recent record per `key` (supersede without delete)."""
    sql = f"""
        SELECT DISTINCT ON (r.key)
            r.id, r.key, r.value, r.source_client, r.source_id,
            r.session_id, r.project_id, r.created_at
        FROM {model.__tablename__} r
        WHERE {where}
        ORDER BY r.key, r.created_at DESC, r.id DESC
    """
    result = await db.execute(text(sql), params)
    return [dict(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
async def record_event(
    db: AsyncSession,
    type: str,
    payload: dict | None,
    session_id: str | None,
    project_id: str | None,
    source_client: str | None,
) -> dict[str, Any]:
    if session_id:
        session = await get_session(db, session_id)
        if not session:
            raise NotFound(f"Session {session_id} not found")
        project_id = project_id or session.project_id
    event = Event(
        id=new_id(),
        session_id=session_id,
        project_id=project_id,
        type=type,
        payload=payload,
        source_client=source_client,
    )
    db.add(event)
    await db.flush()
    return {
        "id": event.id,
        "type": event.type,
        "payload": event.payload,
        "source_client": event.source_client,
        "session_id": session_id,
        "project_id": project_id,
        "created_at": event.created_at,
    }


# ---------------------------------------------------------------------------
# Project states
# ---------------------------------------------------------------------------
async def record_project_state(
    db: AsyncSession,
    project_id: str,
    state_json: dict,
    source_client: str | None,
    session_id: str | None = None,
) -> dict[str, Any]:
    project = await get_project(db, project_id)
    if not project:
        raise NotFound(f"Project {project_id} not found")
    state = ProjectState(
        id=new_id(),
        project_id=project_id,
        session_id=session_id,
        state_json=state_json,
        source_client=source_client,
    )
    db.add(state)
    await db.flush()
    return {
        "id": state.id,
        "project_id": state.project_id,
        "session_id": session_id,
        "state_json": state.state_json,
        "source_client": source_client,
        "created_at": state.created_at,
    }


async def current_project_state(
    db: AsyncSession, project_id: str
) -> dict | None:
    result = await db.execute(
        text(
            """
            SELECT DISTINCT ON (project_id)
                id, project_id, session_id, state_json, source_client, created_at
            FROM project_states
            WHERE project_id = :pid
            ORDER BY project_id, created_at DESC
            """
        ),
        {"pid": project_id},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row else None


async def inspect_project_history(
    db: AsyncSession,
    project_id: str,
    limit: int = 200,
    after: datetime | None = None,
) -> dict[str, Any]:
    """Ordered timeline of everything that changed in a project."""
    timeline: list[dict[str, Any]] = []

    async def add(kind: str, sql: str, extra: dict) -> None:
        if after is not None:
            sql += " AND r.created_at >= :after"
        sql += " ORDER BY r.created_at DESC, r.id DESC"
        result = await db.execute(text(sql), {**extra, "after": after})
        for r in result.mappings().all():
            item = dict(r)
            item["kind"] = kind
            item["direction"] = "created"
            timeline.append(item)

    await add(
        "project_state",
        """SELECT r.id, r.project_id, r.session_id, r.state_json AS value,
                  r.source_client, r.created_at
           FROM project_states r WHERE r.project_id = :pid""",
        {"pid": project_id},
    )
    await add(
        "event",
        """SELECT r.id, r.project_id AS "project_id", r.session_id, r.type AS key,
                  r.payload AS value, r.source_client, r.created_at
           FROM events r WHERE r.project_id = :pid""",
        {"pid": project_id},
    )
    for kind, model in _MEMORY_MODELS.items():
        await add(
            kind,
            f"""SELECT r.id, r.project_id AS "project_id", r.session_id,
                      r.key, r.value, r.source_client, r.created_at
               FROM {model.__tablename__} r WHERE r.project_id = :pid""",
            {"pid": project_id},
        )

    timeline.sort(key=lambda x: (x["created_at"] or datetime.max, x["id"]), reverse=True)
    timeline = timeline[:limit]
    return {"timeline": timeline}


# ---------------------------------------------------------------------------
# Project context (current, aggregated)
# ---------------------------------------------------------------------------
async def get_project_context(
    db: AsyncSession,
    project_id: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    if not project_id and session_id:
        session = await get_session(db, session_id)
        if not session:
            raise NotFound(f"Session {session_id} not found")
        project_id = session.project_id
    if not project_id:
        raise MemoryError("bad_request", "project_id or session_id required")

    project = await get_project(db, project_id)
    sessions = (
        (await db.execute(
            select(Session).where(Session.project_id == project_id)
        )).scalars().all()
    )
    session_ids = [s.id for s in sessions]

    context: dict[str, Any] = {
        "project_id": project_id,
        "project": (
            {"id": project.id, "name": project.name, "description": project.description}
            if project
            else None
        ),
        "session_count": len(session_ids),
        "sessions": [
            {"id": s.id, "provider": s.provider, "source_session_id": s.source_session_id, "title": s.title}
            for s in sessions
        ],
        "messages_count": 0,
    }
    if session_ids:
        result = await db.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id.in_(session_ids)
            )
        )
        context["messages_count"] = result.scalar_one()

    # Facts/decisions/preferences may be recorded either on a session or at the
    # project level (session_id NULL). They carry a denormalised project_id, so we
    # filter on that -- exactly like the events/project_states queries below -- to
    # surface both.
    params: dict[str, Any] = {"pid": project_id}
    context["current_facts"] = await _latest_per_key(
        db, Fact, "r.project_id = :pid", params
    )
    context["current_decisions"] = await _latest_per_key(
        db, Decision, "r.project_id = :pid", params
    )
    context["current_preferences"] = await _latest_per_key(
        db, Preference, "r.project_id = :pid", params
    )
    context["current_project_state"] = await current_project_state(db, project_id)

    result = await db.execute(
        text(
            """SELECT id, type, payload, source_client, created_at
               FROM events WHERE project_id = :pid
               ORDER BY created_at DESC LIMIT :lim"""
        ),
        {"pid": project_id, "lim": limit},
    )
    context["recent_events"] = [dict(r) for r in result.mappings().all()]
    return context


# ---------------------------------------------------------------------------
# Find changes made by another client
# ---------------------------------------------------------------------------
async def find_changes(
    db: AsyncSession,
    other_client: str,
    since: datetime | None = None,
    project_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return records written by `other_client`, newest first."""
    timeline: list[dict[str, Any]] = []
    params: dict[str, Any] = {"src": other_client}
    where = "r.source_client = :src"
    if since is not None:
        where += " AND r.created_at >= :since"
        params["since"] = since
    if project_id:
        where += " AND r.project_id = :pid"
        params["pid"] = project_id

    for kind, model in _MEMORY_MODELS.items():
        sql = f"""SELECT r.id, '{kind}' AS kind, r.key, r.value, r.source_client,
                      r.source_id, r.session_id, r.project_id, r.created_at
                  FROM {model.__tablename__} r WHERE {where}
                  ORDER BY r.created_at DESC, r.id DESC LIMIT :lim"""
        result = await db.execute(text(sql), {**params, "lim": limit})
        for r in result.mappings().all():
            timeline.append(dict(r))

    for table in ("events", "project_states"):
        if table == "project_states":
            value_col = "state_json AS value"
            kind = "project_state"
        else:
            value_col = "type AS key, payload AS value"
            kind = "event"
        sql = f"""SELECT id, '{kind}' AS kind, {value_col}, source_client,
                      session_id, project_id, created_at
                 FROM {table} r WHERE {where}
                 ORDER BY created_at DESC, id DESC LIMIT :lim"""
        result = await db.execute(text(sql), {**params, "lim": limit})
        for r in result.mappings().all():
            timeline.append(dict(r))

    timeline.sort(key=lambda x: (x["created_at"] or datetime.max, x["id"]), reverse=True)
    return {"other_client": other_client, "changes": timeline[:limit]}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {key: getattr(row, key) for key in row._mapping.keys()}
