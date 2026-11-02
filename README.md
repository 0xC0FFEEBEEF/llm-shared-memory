# Shared‑Memory MCP for AI Tools

A self‑hosted **shared memory system** that lets multiple AI tools — Claude Code,
Codex, and a local OpenCode‑powered model — share one canonical conversation
history. Everything is stored in a single **PostgreSQL** database and exposed to
the tools over **MCP (stdio)**. The memory is append‑only with *supersede without
delete*: a changed fact or decision simply appends a newer row, so the full
history is preserved and can be queried.

## Features

* **Canonical history** – raw messages, facts, decisions, preferences, events, and
  project states all live in one PostgreSQL database shared by every tool.
* **Idempotent appends** – messages are deduplicated by `source_id`, so retries and
  re‑sends never create duplicates.
* **Full‑text search** – `to_tsvector` / `ts_rank` over the message body, with a GIN
  index. Search spans *all* sessions/providers in one query.
* **Optional semantic search** – when `MEMORY_USE_EMBEDDINGS=1`, each message also
  stores a deterministic local embedding in a real **pgvector** column, and retrieval
  becomes a hybrid FTS + cosine‑similarity search. Off by default.
* **Pagination** – read a session's messages page by page.
* **Current context** – one tool returns the latest facts, decisions, preferences,
  project state, and recent events for a project *across all its sessions*.
* **Append‑only streams** – facts / decisions / preferences / events / project states
  are stored as an immutable log; changing a value just appends a new row.
* **Change tracking** – every write is tagged with a `source_client`, so you can ask
  "what did Codex change?" and get a diff of another client's writes.
* **Credential rejection** – appends that look like secrets (`sk-…` keys) are refused.
* **MCP, not REST** – the server speaks MCP over stdio (the transport Claude Code,
  Codex, and OpenCode use).

## Architecture

```
  +-----------------+     MCP / stdio      +--------------------+
  |  Claude Code    | ------------------> |                    |
  |  Codex          |  (shared-memory)    |  PostgreSQL        |
  |  OpenCode AI    | <------------------ |  (pgvector + FTS)  |
  +-----------------+                      +--------------------+
```

Each tool is a thin wrapper over `memory.crud`. The database is the single source
of truth; there is no in‑server cache to sync. Two clients writing concurrently
see each other's rows the next time they query — there is nothing to "sync", only
one shared store.

## Requirements

* **Python 3.12+**
* **PostgreSQL 16 + pgvector**. This repo ships with a bundled embedded
  PostgreSQL 16.2 (with the `vector` extension) so you can run everything
  locally with **no Docker and no system package installation**. You can also
  point it at any other PostgreSQL via `DATABASE_URL`.

## Quick Start (embedded PostgreSQL, no Docker)

```bash
git clone <this-repo> && cd shared-memory
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Start the bundled PostgreSQL on port 5433 (idempotent).
scripts/start-pg.sh start

# 2. Create the database + schema. Either path works and produces the same schema:
#    (a) migrations via Alembic (preferred for versioning/dev parity):
#        export DATABASE_URL="postgresql+asyncpg://memory:memory@localhost:5433/memory"
#        alembic upgrade head
#    (b) or just run the MCP server — it boots the schema itself on startup.

# 3. Run the MCP server (stdio).
python -m memory.mcp_server
```

### Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql+asyncpg://memory:memory@localhost:5433/memory` | async SQLAlchemy URL. Override for any PostgreSQL. |
| `MEMORY_USE_EMBEDDINGS` | `0` | `1` enables the optional local pgvector embedding + hybrid search. |
| `PG_POOL_SIZE` / `PG_MAX_OVERFLOW` | `10` / `10` | asyncpg pool sizing. |

## Database setup & migrations

The schema is defined once, in the ORM (`memory/models.py`), and two independent
paths produce an identical database:

* **`memory.database.init_schema()`** – idempotent bootstrap used by the runtime
  and tests. Creates the pgvector extension, all tables, and indexes.
* **Alembic** – `alembic/versions/2026_09_27_initial.py` mirrors the ORM exactly.
  Use it to evolve the schema over time:

```bash
export DATABASE_URL="postgresql+asyncpg://memory:memory@localhost:5433/memory"
alembic upgrade head     # apply migrations
alembic check            # verify no drift from the ORM ("No new upgrade operations detected.")
alembic downgrade -v -1  # step back one migration
```

> **Note:** the runtime app uses **asyncpg** (async), but Alembic's migration
> context is synchronous, so `alembic/env.py` transparently rewrites the URL to the
> pure‑Python **pg8000** driver for migrations only. See `alembic/env.py`.

## Using the MCP server from a client

The server is launched over **stdio** (see `memory/mcp_server.py` entrypoint).
Configure any MCP client to spawn it. Every client starts the server from its
own working directory, so set `PYTHONPATH` to the project root, or the import
`python -m memory.mcp_server` fails with `ModuleNotFoundError`. The examples
below point `DATABASE_URL` at the shared `memory` database so every client sees
the same history.

### Claude Code

Use `-s user` to make it available in every project (there is no
`~/.claude/projects/.claude-mcp.json`); `-s project` writes `.mcp.json` in the
project root instead.

```bash
claude mcp add -s user shared-memory \
  -e DATABASE_URL=postgresql+asyncpg://memory:memory@localhost:5433/memory \
  -e PYTHONPATH=/path/to/llmmemory \
  -- /path/to/llmmemory/.venv/bin/python -m memory.mcp_server
```

### Codex

Codex is configured in `~/.codex/config.toml`, which is **TOML**, not JSON. You
can add the entry by hand:

```toml
[mcp_servers.shared-memory]
command = "/path/to/llmmemory/.venv/bin/python"
args = ["-m", "memory.mcp_server"]
env = { DATABASE_URL = "postgresql+asyncpg://memory:memory@localhost:5433/memory", PYTHONPATH = "/path/to/llmmemory" }
```

or equivalently with the CLI (`codex mcp add shared-memory --env KEY=VALUE ... -- <command>`).

### OpenCode

OpenCode v2 uses `mcp.servers.<name>` in its config, not `mcpServers`. Use the
CLI so the format comes out right:

```bash
opencode mcp add --global shared-memory \
  --env DATABASE_URL=postgresql+asyncpg://memory:memory@localhost:5433/memory \
  --env PYTHONPATH=/path/to/llmmemory \
  -- /path/to/llmmemory/.venv/bin/python -m memory.mcp_server
```

> **OpenCode note:** the user's OpenCode runs inside a network‑isolated sandbox
> that cannot reach the host's PostgreSQL. Reaching it requires a bridge added to
> the sandbox wrapper — outside this project, so it is listed under
> [What still needs the user](#what-still-needs-the-user).

All three clients point at the **same** `DATABASE_URL`, so they share one
canonical history — there is nothing to "sync", only one shared store.

### MCP tool reference

Every tool returns a plain dict wrapped as `{"is_error": false, …}`, or a
structured error `{"is_error": true, "code": …, "error": …}` — **tools never raise
on business errors**, so clients can inspect results programmatically.

| Tool | Purpose |
|------|---------|
| `list_projects()` | List all projects. |
| `create_project(name, description?)` | Create a project. |
| `start_session(provider, source_session_id, title?, project_id?)` | Create/find a session (idempotent by provider+source). |
| `append_message(session_id, source_message_id, content, original_timestamp, role?, order?, payload?, provider?, source_client?)` | Append a message (idempotent by source_id). |
| `read_session(session_id, cursor?, limit?)` | Read a session's messages, paginated. |
| `get_message(message_id)` | Fetch one message. |
| `search_history(query, limit?, source_client?)` | Full‑text (+ optional vector) search across **all** history. |
| `record_fact` / `record_decision` / `record_preference` | Record a sourced, append‑only memory value. |
| `record_event(type, payload?, …)` | Record an event. |
| `get_project_context(project_id?, session_id?, limit?)` | Latest facts/decisions/preferences/project state + recent events for a project. |
| `inspect_project_history(project_id, limit?, after?)` | Full append‑only timeline (facts/decisions/preferences/events/project_states). |
| `record_project_state(project_id, state_json, …)` | Snapshot a project's current state (append‑only). |
| `find_changes(other_client, since?, project_id?, limit?)` | List records written by another `source_client`. |

## Capture: automatic, hook, or manual

There are three ways data gets **into** memory. Be explicit about which one your
integration uses.

* **Automatic capture is not built yet.** Nothing is captured automatically
  today: none of these three clients sends its conversation to an MCP server on
  its own. The server only sees what the model chooses to send through tool
  calls — that is **manual capture**, and it will miss turns.

  Automatic capture needs a small importer per client. The importer reads the
  client's own session store and calls `append_message`, which is idempotent by
  `source_id`, so re‑reading is safe. Planned importers:

  * **Claude Code** – hooks configured in `settings.json` (e.g. `Stop`,
    `SessionEnd`) receive JSON on stdin that includes `session_id` and
    `transcript_path`. Transcripts are JSONL files at
    `~/.claude/projects/<project>/<session-id>.jsonl`.
  * **Codex** – sessions are JSONL files at
    `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`; a `notify` program set in
    `config.toml` runs after each turn.
  * **OpenCode v2** – sessions live in its SQLite database `opencode.db`, in
    tables `session_v2` and `session_message`. The sandboxed install keeps that
    file under the sandbox's own home directory.

* **Hook / on‑demand capture** – the client captures only at specific points
  (when a tool call returns, on session end, when a fact is surfaced). It is
  still client‑code deciding *when* to call `append_message` / `record_fact`.

* **Manual capture** – a human or agent calls `record_fact`, `record_decision`,
  `record_preference`, `record_event`, or `record_project_state` directly. This
  is how structured, high‑signal knowledge (a decision, a preference) gets in
  without a full message stream.

All three write to the same tables, so the data is indistinguishable downstream —
each write is tagged with `source_client` and can be found via
`find_changes` / `inspect_project_history`.

> **Known limitation:** the credential filter refuses a whole message rather than
> blanking out just the secret, so a captured transcript will have gaps wherever
> a message trips it. This is intentional for now; the filter is deliberately
> not changed.

## Two‑client demo

`examples/two_client_demo.py` simulates **Claude** and **Codex** sharing one
database and proves cross‑client visibility:

```bash
scripts/start-pg.sh start
python -m examples.two_client_demo
```

The demo writes to its **own** throwaway `memory_demo` database, read from
`DEMO_DATABASE_URL` (default `postgresql+asyncpg://memory:memory@localhost:5433/memory_demo`),
so it never needs `DATABASE_URL` and never touches the shared `memory` database.
It resets `memory_demo` before running, so re‑running is safe.

It shows Claude writing one session and Codex another on the same project, each
client seeing the other's writes via `find_changes`, and project context + search
spanning both sessions.

## Security

The store contains conversation history and derived knowledge. Two guards are in
place:

* **Credential rejection** – appends whose content looks like a secret
  (e.g. `sk-…` API keys) are refused by `memory.crud_secret`.
* **Never store secrets** – it is the caller's responsibility not to send secrets;
  the guard is a safety net, not a substitute. For production, front the database
  with TLS + auth and audit access.

## Running the tests

The suite runs against a **real PostgreSQL** instance (the bundled embedded one by
default).

```bash
scripts/start-pg.sh start
export LD_LIBRARY_PATH="$PWD/.embedded-pg/pginstall/lib"
export PYTHONPATH="$PWD"
python -m pytest
```

Tests cover: idempotent appends, conflict detection, full message field storage,
pagination, cross‑client visibility, full‑text search (+ `source_client` filter),
fact supersession, decisions/preferences, project context, events, project states,
`find_changes`, credential rejection, and structured error reporting. There is also
`test_embeddings.py`, which exercises the real pgvector semantic search path
(`MEMORY_USE_EMBEDDINGS=1`).

## Project layout

```
memory/            ORM models, async engine, CRUD, MCP server, embeddings
alembic/           schema migrations (mirror the ORM)
scripts/           start-pg.sh / bootstrap-pg.sh for the embedded PostgreSQL
examples/          two_client_demo.py — cross-client write/read demo
tests/             pytest integration suite against real PostgreSQL
```

## What still needs the user

A few things are out of scope for this repo and must be done on the user's side.

* **OpenCode sandbox → host PostgreSQL.** The user's OpenCode runs inside a
  network‑isolated sandbox that cannot reach the host's PostgreSQL. Pointing the
  MCP server's `DATABASE_URL` at a reachable database (or adding a bridge to the
  sandbox wrapper) is outside this project and must be arranged there.

* **Automatic capture.** None of the three clients streams its conversation to
  this server automatically (see [Capture](#capture-automatic-hook-or-manual)).
  The per‑client importers described there still need to be written.

* **`DATABASE_URL` in production.** The examples default to the local embedded
  server on port `5433`. Point `DATABASE_URL` (and `DEMO_DATABASE_URL` for the
  demo) at your real PostgreSQL to run against it.

## Contributing

Please run `pytest` locally and add tests for new features. Keep the ORM
(`memory/models.py`) and the latest Alembic migration in sync, and verify with
`alembic check`.

## About this copy

A public snapshot of my private working repo. More of my projects: [nbdevlab.com](https://www.nbdevlab.com).


## About this copy

A public snapshot of my private working repo. More of my projects: [nbdevlab.com](https://www.nbdevlab.com).

## License

GNU General Public License v3.0 or later, see [`LICENSE`](LICENSE).

<sub>Preparing this public copy from my private repo was done with help from Claude.</sub>
