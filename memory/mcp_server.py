"""The shared-memory MCP server.

Exposes the full API as MCP tools. Every tool is a thin, async wrapper over
:mod:`memory.crud`. Tools return plain dicts (or lists); the MCP runtime
serialises them to JSON text for the client. Errors are returned as structured
``{"is_error": true, "code": ..., "error": ...}`` results rather than raising,
so a client can inspect them programmatically.

Run over stdio (the transport Claude Code, Codex and OpenCode use):

    python -m memory.mcp_server
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from mcp.server import MCPServer

from . import crud

server = MCPServer(
    name="shared-memory",
    version="0.1.0",
    description=(
        "Canonical, shared conversation memory for the AI tools you use. "
        "Stores raw messages, facts, decisions, preferences, events and project "
        "states in PostgreSQL and exposes them over MCP."
    ),
)


def _ok(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return {"is_error": False, **payload}
    # Defensive: any non-dict return (e.g. a bare list) is wrapped so the
    # tool always yields a consistent {"is_error": ..., ...} payload.
    return {"is_error": False, "result": payload}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"is_error": True, "code": code, "error": message}


async def _wrap(fn):
    """Run a crud-backed coroutine and translate exceptions into results."""
    from .database import get_db

    try:
        async with get_db() as db:
            return _ok(await fn(db))
    except crud.MemoryError as exc:
        return _fail(exc.code, exc.message)
    except Exception as exc:  # pragma: no cover - defensive
        return _fail("internal_error", str(exc))


# ---------------------------------------------------------------------------
# Projects & sessions
# ---------------------------------------------------------------------------
@server.tool()
async def list_projects() -> dict[str, Any]:
    async def _run(db):
        return {"projects": await crud.list_projects(db)}

    return await _wrap(_run)


@server.tool()
async def create_project(
    name: str, description: Optional[str] = None
) -> dict[str, Any]:
    async def _run(db):
        project = await crud.create_project(db, name, description, source_client="cli")
        return {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "created_at": project.created_at,
        }

    return await _wrap(_run)


@server.tool()
async def start_session(
    provider: str,
    source_session_id: str,
    title: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    async def _run(db):
        session = await crud.get_or_create_session(
            db, provider, source_session_id, title=title, project_id=project_id
        )
        return {
            "session_id": session.id,
            "provider": session.provider,
            "source_session_id": session.source_session_id,
            "project_id": session.project_id,
            "title": session.title,
            "created_at": session.created_at,
        }

    return await _wrap(_run)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
@server.tool()
async def append_message(
    session_id: str,
    source_message_id: str,
    content: str,
    original_timestamp: datetime,
    role: str = "assistant",
    order: int = 0,
    payload: Optional[dict[str, Any]] = None,
    provider: Optional[str] = None,
    source_client: Optional[str] = None,
) -> dict[str, Any]:
    from .schemas import MessageCreate

    msg_in = MessageCreate(
        source_message_id=source_message_id,
        content=content,
        role=role,
        order=order,
        original_timestamp=original_timestamp,
        payload=payload,
        provider=provider,
        source_client=source_client,
    )

    async def _run(db):
        return await crud.append_message(db, session_id, msg_in)

    return await _wrap(_run)


@server.tool()
async def read_session(
    session_id: str, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    async def _run(db):
        messages = await crud.list_messages(db, session_id, offset=cursor, limit=limit)
        total = await crud.count_messages(db, session_id)
        return {
            "session_id": session_id,
            "total": total,
            "cursor": cursor,
            "limit": limit,
            "has_more": cursor + len(messages) < total,
            "messages": [
                {
                    "id": m.id,
                    "session_id": m.session_id,
                    "provider": m.provider,
                    "role": m.role,
                    "order": m.order,
                    "original_timestamp": m.original_timestamp,
                    "ingestion_timestamp": m.ingestion_timestamp,
                    "source_id": m.source_id,
                    "content": m.content,
                    "payload": m.payload,
                    "source_client": m.source_client,
                }
                for m in messages
            ],
        }

    return await _wrap(_run)


@server.tool()
async def get_message(message_id: str) -> dict[str, Any]:
    async def _run(db):
        m = await crud.get_message(db, message_id)
        if not m:
            raise crud.NotFound(f"Message {message_id} not found")
        return {
            "id": m.id,
            "session_id": m.session_id,
            "provider": m.provider,
            "role": m.role,
            "order": m.order,
            "original_timestamp": m.original_timestamp,
            "source_id": m.source_id,
            "content": m.content,
            "payload": m.payload,
            "source_client": m.source_client,
        }

    return await _wrap(_run)


@server.tool()
async def search_history(
    query: str, limit: int = 25, source_client: Optional[str] = None
) -> dict[str, Any]:
    async def _run(db):
        return {"query": query, "results": await crud.search_messages(
            db, query, limit=limit, source_client=source_client
        )}

    return await _wrap(_run)


# ---------------------------------------------------------------------------
# Facts / decisions / preferences
# ---------------------------------------------------------------------------
def _memory_tool(kind: str):
    async def _tool(
        key: str,
        value: str,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        source_client: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        async def _run(db):
            return await crud.record_memory(
                db, kind, key, value, session_id, project_id, source_client, source_id
            )

        return await _wrap(_run)

    _tool.__name__ = f"record_{kind}"
    _tool.__qualname__ = f"record_{kind}"
    _tool.__doc__ = f"Record a sourced {kind} (append-only; supersede preserves history)."
    return _tool


for _kind in ("fact", "decision", "preference"):
    server.tool()(_memory_tool(_kind))


@server.tool()
async def record_event(
    type: str,
    payload: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    source_client: Optional[str] = None,
) -> dict[str, Any]:
    async def _run(db):
        return await crud.record_event(
            db, type, payload, session_id, project_id, source_client
        )

    return await _wrap(_run)


# ---------------------------------------------------------------------------
# Project context & history
# ---------------------------------------------------------------------------
@server.tool()
async def get_project_context(
    project_id: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 20,
) -> dict[str, Any]:
    async def _run(db):
        return await crud.get_project_context(
            db, project_id=project_id, session_id=session_id, limit=limit
        )

    return await _wrap(_run)


@server.tool()
async def inspect_project_history(
    project_id: str,
    limit: int = 200,
    after: Optional[datetime] = None,
) -> dict[str, Any]:
    async def _run(db):
        return await crud.inspect_project_history(db, project_id, limit=limit, after=after)

    return await _wrap(_run)


@server.tool()
async def record_project_state(
    project_id: str,
    state_json: dict[str, Any],
    source_client: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    async def _run(db):
        return await crud.record_project_state(
            db, project_id, state_json, source_client, session_id
        )

    return await _wrap(_run)


# ---------------------------------------------------------------------------
# Cross-client diff
# ---------------------------------------------------------------------------
@server.tool()
async def find_changes(
    other_client: str,
    since: Optional[datetime] = None,
    project_id: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    async def _run(db):
        return await crud.find_changes(
            db, other_client, since=since, project_id=project_id, limit=limit
        )

    return await _wrap(_run)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    from .database import init_schema, health

    async def _main() -> None:
        if not await health():
            raise SystemExit(
                "Cannot reach PostgreSQL. Start it with scripts/start-pg.sh "
                "or set DATABASE_URL."
            )
        await init_schema()
        server.run_stdio_async()

    asyncio.run(_main())
