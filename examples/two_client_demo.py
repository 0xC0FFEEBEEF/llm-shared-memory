#!/usr/bin/env python3
"""Two-client demo for the shared-memory MCP server.

Simulates two independent AI tools -- **Claude** and **Codex** -- writing to and
reading from the *same* canonical PostgreSQL database through the shared MCP tool
layer. It demonstrates cross-client visibility:

* Claude starts a session on the `acme` project and records facts/decisions.
* Codex starts a *different* session on the same project and records a preference.
* Neither client sees the other's session in its own `read_session`, but both can
  read across the project via `get_project_context` and see each other's writes
  via `find_changes(other_client=...)`.

Run it against the bundled embedded PostgreSQL:

    .venv/bin/python -m examples.two_client_demo

or point it at any database:

    DEMO_DATABASE_URL=postgresql+asyncpg://user:pass@host:5433/memory_demo \
        .venv/bin/python -m examples.two_client_demo

The demo writes to its own throwaway `memory_demo` database (read from
``DEMO_DATABASE_URL``, never ``DATABASE_URL``) and resets it to a blank slate so
it is safe to re-run.
"""
from __future__ import annotations

import os
import sys

# Make the package importable when run as a bare script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The demo always writes to its own throwaway memory_demo database, read from
# DEMO_DATABASE_URL (never DATABASE_URL). memory.database builds its engine from
# DATABASE_URL at import time, so set that here -- before importing memory -- to
# point the shared engine at the demo database.
os.environ["DATABASE_URL"] = os.environ.get(
    "DEMO_DATABASE_URL",
    "postgresql+asyncpg://memory:memory@localhost:5433/memory_demo",
)

from datetime import datetime, timezone  # noqa: E402

from memory._db_safety import (  # noqa: E402
    database_name_from_url,
    guard_db_name,
    ensure_database,
)
from memory.database import get_db, init_schema  # noqa: E402
from memory.mcp_server import server  # noqa: E402


def _demo_db_url() -> str:
    """The demo's own throwaway database, from DEMO_DATABASE_URL (never DATABASE_URL)."""
    return os.environ.get(
        "DEMO_DATABASE_URL",
        "postgresql+asyncpg://memory:memory@localhost:5433/memory_demo",
    )


def _refuse_unsafe(url: str) -> None:
    """Exit non-zero unless ``url`` names a ``_test`` / ``_demo`` database."""
    name = database_name_from_url(url)
    if not guard_db_name(name):
        print(
            f"Refusing to run the demo against {url!r}: database {name!r} does "
            f"not end in '_test' or '_demo'. Aborting to protect your data.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _e(*args):
    print(*args, file=sys.stderr)


class Client:
    """One AI tool talking to the shared server, tagged with a source_client."""

    # Writes carry the writer's identity so it can be attributed / diffed later.
    # Reads (search, context, find_changes, ...) must NOT stamp an identity --
    # otherwise a query would filter itself out.
    WRITES = {
        "append_message",
        "record_fact",
        "record_decision",
        "record_preference",
        "record_event",
        "record_project_state",
    }

    def __init__(self, name: str):
        self.name = name

    async def call(self, tool, **kwargs):
        result = await server.call_tool(tool, kwargs)
        sc = result.structured_content or {}
        if sc.get("is_error"):
            raise RuntimeError(f"[{self.name}] {tool} errored: {sc}")
        return sc

    async def write(self, tool, **kwargs):
        """Like ``call`` but stamps this client's identity (write attribution)."""
        kwargs.setdefault("source_client", self.name)
        return await self.call(tool, **kwargs)


async def reset_db() -> None:
    """Give the demo a blank slate (guard, auto-create, idempotent schema + truncate)."""
    url = _demo_db_url()
    _refuse_unsafe(url)  # never touch anything but a _demo database
    await ensure_database(url)
    await init_schema()
    from sqlalchemy import text

    sql = (
        "TRUNCATE project_states, events, facts, decisions, preferences, "
        "messages, sessions, projects RESTART IDENTITY CASCADE"
    )
    async with get_db() as db:
        await db.execute(text(sql))
        await db.commit()


async def main() -> int:
    await reset_db()

    claude = Client("claude")
    codex = Client("codex")

    # --- Claude bootstraps the project + his session -------------------------
    proj = await claude.call(
        "create_project", name="acme", description="Demo workspace"
    )
    project_id = proj["id"]
    _e(f"project:        {project_id}  (acme)")

    claude_session = await claude.call(
        "start_session",
        provider="Claude",
        source_session_id="claude-sess-1",
        title="Acme architecture review",
        project_id=project_id,
    )
    claude_session_id = claude_session["session_id"]

    # Claude appends two messages and sources a fact + a decision.
    await claude.write(
        "append_message",
        session_id=claude_session_id,
        source_message_id="c-msg-1",
        role="user",
        order=0,
        content="Which database should we use for shared agent memory?",
        original_timestamp=_now(),
        payload={"provider": "Claude"},
    )
    await claude.write(
        "append_message",
        session_id=claude_session_id,
        source_message_id="c-msg-2",
        role="assistant",
        order=1,
        content="PostgreSQL with pgvector gives us full-text and semantic search.",
        original_timestamp=_now(),
        payload={"provider": "Claude"},
    )
    await claude.write(
        "record_fact",
        session_id=claude_session_id,
        project_id=project_id,
        key="db_engine",
        value="postgresql",
    )
    await claude.write(
        "record_decision",
        session_id=claude_session_id,
        project_id=project_id,
        key="storage",
        value="shared PostgreSQL, append-only with supersede",
    )

    # --- Codex joins the SAME project on a DIFFERENT session -----------------
    codex_session = await codex.call(
        "start_session",
        provider="Codex",
        source_session_id="codex-sess-1",
        title="Acme storage migration",
        project_id=project_id,
    )
    codex_session_id = codex_session["session_id"]

    await codex.write(
        "append_message",
        session_id=codex_session_id,
        source_message_id="x-msg-1",
        role="user",
        order=0,
        content="Let's migrate the schema so shared memory stays consistent.",
        original_timestamp=_now(),
        payload={"provider": "Codex"},
    )
    await codex.write(
        "record_preference",
        session_id=codex_session_id,
        project_id=project_id,
        key="migration_tool",
        value="alembic",
    )
    await codex.write(
        "record_project_state",
        project_id=project_id,
        session_id=codex_session_id,
        state_json={"status": "migrating", "step": "apply migrations"},
    )

    # --- Cross-client visibility ---------------------------------------------
    _e("\n-- Claude's own session (only his messages) --")
    my = await claude.call("read_session", session_id=claude_session_id)
    _e(f"  messages in {claude_session_id[:8]}: "
       f"{[m['source_id'] for m in my['messages']]}")

    _e("\n-- Can Claude SEE Codex's session in his own read_session? --")
    _e(f"  messages in {codex_session_id[:8]} visible to Claude: "
       f"{[m['source_id'] for m in (await claude.call('read_session', session_id=codex_session_id))['messages']]}")

    _e("\n-- Project context across BOTH sessions (project_id filter) --")
    ctx = await claude.call("get_project_context", project_id=project_id)
    _e(f"  sessions:        {ctx['session_count']} ({', '.join(s['provider'] for s in ctx['sessions'])})")
    _e(f"  messages:        {ctx['messages_count']}")
    _e(f"  current_facts:   {ctx['current_facts']}")
    _e(f"  current_decisions: {ctx['current_decisions']}")
    _e(f"  current_preferences: {ctx['current_preferences']}")
    _e(f"  current_project_state: {ctx['current_project_state']}")
    _e(f"  recent_events:   {len(ctx['recent_events'])}")

    _e("\n-- Each client finds the other's writes via find_changes --")
    from_claude = await codex.call("find_changes", other_client="claude", project_id=project_id)
    _e(f"  Codex sees {len(from_claude)} change(s) written by claude")
    from_codex = await claude.call("find_changes", other_client="codex", project_id=project_id)
    _e(f"  Claude sees {len(from_codex)} change(s) written by codex")

    _e("\n-- Full-text search across ALL raw history (both clients) --")
    hits = await claude.call("search_history", query="memory")
    _e(f"  search_history('memory') -> {len(hits['results'])} message(s) across all sessions")

    # --- Verification --------------------------------------------------------
    assert len(from_claude) > 0, "expected Claude's writes visible to Codex"
    assert len(from_codex) > 0, "expected Codex's writes visible to Claude"
    assert len(hits["results"]) >= 2, "expected both clients' messages to surface"

    _e("\nTWO-CLIENT DEMO OK: writes from both clients are visible to each other "
       "through the shared PostgreSQL store.\n")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
