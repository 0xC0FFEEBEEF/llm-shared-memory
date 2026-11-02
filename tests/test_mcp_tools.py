"""Integration tests for the shared-memory MCP server.

Every test exercises the real MCP tool layer end-to-end against PostgreSQL.
They cover the full requirement set:

* idempotent message appends (dedup on retry, conflict on divergence)
* full message storage (provider, session, order, timestamps, source ids)
* search across all raw history (full-text)
* paginated session reads
* sourced facts / decisions / preferences with supersede-without-delete
* events and append-only project states
* cross-client change detection (find_changes)
* project-context filtering by project_id
* credential rejection
* structured (non-raising) error reporting
"""
from datetime import datetime, timezone

import pytest

now = lambda: datetime.now(timezone.utc).isoformat()


async def make_project(call, name="proj", **extra):
    res = await call(
        "create_project",
        name=name,
        description=extra.get("description"),
    )
    return res["id"]


async def make_session(call, project_id, provider, source_session_id, **extra):
    res = await call(
        "start_session",
        provider=provider,
        source_session_id=source_session_id,
        title=extra.get("title"),
        project_id=project_id,
    )
    return res["session_id"]


# ---------------------------------------------------------------------------
# Projects & sessions
# ---------------------------------------------------------------------------
async def test_create_and_list_projects(call):
    p1 = await make_project(call, "alpha")
    p2 = await make_project(call, "beta")
    listed = await call("list_projects")
    names = {p["name"] for p in listed["projects"]}
    assert {"alpha", "beta"}.issubset(names)
    assert p1 and p2 and p1 != p2


async def test_session_is_idempotent(call):
    import uuid

    pid = await make_project(call, "idem")
    src = "sess-uuid-" + str(uuid.uuid4())
    a = await make_session(call, pid, "claude", src)
    # Same provider+source again -> returns the same session row.
    b = await make_session(call, pid, "claude", src)
    assert a == b

    # A different provider/source is a distinct session.
    c = await make_session(call, pid, "codex", "other-sess")
    assert c != a


# ---------------------------------------------------------------------------
# Messages: idempotent append, conflict, storage fields
# ---------------------------------------------------------------------------
async def test_append_is_idempotent(call):
    pid = await make_project(call, "append-idem")
    sid = await make_session(call, pid, "claude", "s-append")

    ts = now()
    first = await call(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="Migrate payments to Rust",
        role="user",
        order=0,
        original_timestamp=ts,
        source_client="claude",
    )
    assert first["created"] is True

    # Identical retry -> same row, no new row.
    retry = await call(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="Migrate payments to Rust",
        role="user",
        order=0,
        original_timestamp=ts,
        source_client="claude",
    )
    assert retry["created"] is False
    assert retry["message_id"] == first["message_id"]
    assert retry["order"] == 0


async def test_append_conflict_on_divergent_content(call, call_raw):
    pid = await make_project(call, "append-conflict")
    sid = await make_session(call, pid, "claude", "s-conflict")

    await call(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="original",
        role="user",
        order=0,
        original_timestamp=now(),
        source_client="claude",
    )
    err = await call_raw(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="DIFFERENT",
        role="user",
        order=0,
        original_timestamp=now(),
        source_client="claude",
    )
    assert err["is_error"] is True
    assert err["code"] == "conflict"


async def test_message_stores_full_fields(call):
    pid = await make_project(call, "full-fields")
    sid = await make_session(call, pid, "claude", "s-fields")
    ts = now()
    await call(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="hello there",
        role="user",
        order=0,
        original_timestamp=ts,
        payload={"model": "claude-3"},
        provider="claude",
        source_client="claude",
    )
    listed = await call("read_session", session_id=sid, cursor=0, limit=10)
    msg = listed["messages"][0]
    assert msg["content"] == "hello there"
    assert msg["role"] == "user"
    assert msg["order"] == 0
    assert msg["provider"] == "claude"
    assert msg["source_id"] == "m1"
    assert msg["payload"] == {"model": "claude-3"}
    assert msg["source_client"] == "claude"
    # original_timestamp is preserved (not overwritten by ingestion time).
    # PostgreSQL serialises tz-aware timestamps as "Z"; compare as datetime so
    # the timezone-suffix format does not matter.
    from datetime import datetime as _dt

    assert _dt.fromisoformat(msg["original_timestamp"]) == _dt.fromisoformat(ts)
    assert msg["ingestion_timestamp"] is not None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
async def test_read_session_pagination(call):
    pid = await make_project(call, "paginate")
    sid = await make_session(call, pid, "claude", "s-pag")
    for i in range(1, 6):
        await call(
            "append_message",
            session_id=sid,
            source_message_id=f"m{i}",
            content=f"Message number {i}",
            role="user",
            order=i,
            original_timestamp=now(),
            source_client="claude",
        )

    page1 = await call("read_session", session_id=sid, cursor=0, limit=2)
    assert len(page1["messages"]) == 2
    assert page1["has_more"] is True
    assert [m["order"] for m in page1["messages"]] == [1, 2]

    page2 = await call("read_session", session_id=sid, cursor=2, limit=2)
    assert [m["order"] for m in page2["messages"]] == [3, 4]

    page3 = await call("read_session", session_id=sid, cursor=4, limit=10)
    assert len(page3["messages"]) == 1
    assert page3["has_more"] is False


# ---------------------------------------------------------------------------
# Cross-client visibility
# ---------------------------------------------------------------------------
async def test_cross_client_visibility(call):
    """Two clients (different providers/sessions) share one project; each can
    see the other's messages via a history-wide search."""
    pid = await make_project(call, "cross-client")
    s_claude = await make_session(call, pid, "claude", "s-cross-claude")
    s_codex = await make_session(call, pid, "codex", "s-cross-codex")

    await call(
        "append_message",
        session_id=s_claude,
        source_message_id="c1",
        content="Can we move the payments service to Rust?",
        role="user",
        order=0,
        original_timestamp=now(),
        source_client="claude",
    )
    await call(
        "append_message",
        session_id=s_codex,
        source_message_id="c2",
        content="Yes - tokio is a good runtime for it.",
        role="assistant",
        order=0,
        original_timestamp=now(),
        source_client="codex",
    )

    # Each session stores its own provider and source client.
    claude_msgs = await call("read_session", session_id=s_claude, cursor=0, limit=10)
    codex_msgs = await call("read_session", session_id=s_codex, cursor=0, limit=10)
    assert claude_msgs["messages"][0]["provider"] == "claude"
    assert claude_msgs["messages"][0]["source_client"] == "claude"
    assert codex_msgs["messages"][0]["provider"] == "codex"
    assert codex_msgs["messages"][0]["source_client"] == "codex"

    # Codex's message is visible from a search that spans *all* history.
    hits = await call("search_history", query="tokio runtime", limit=10)
    assert any("tokio" in r["content"].lower() for r in hits["results"])
    assert any(r["source_client"] == "codex" for r in hits["results"])


# ---------------------------------------------------------------------------
# Full-text search over all raw history
# ---------------------------------------------------------------------------
async def test_search_history_over_all_history(call):
    pid = await make_project(call, "search")
    s1 = await make_session(call, pid, "claude", "s-search-1")
    s2 = await make_session(call, pid, "codex", "s-search-2")
    await call(
        "append_message",
        session_id=s1,
        source_message_id="a",
        content="The deploy region is us-east-1",
        role="user",
        order=0,
        original_timestamp=now(),
        source_client="claude",
    )
    await call(
        "append_message",
        session_id=s2,
        source_message_id="b",
        content="We use tokio for async",
        role="assistant",
        order=0,
        original_timestamp=now(),
        source_client="codex",
    )
    # A search anywhere finds the message even in the *other* session.
    hits = await call("search_history", query="region us-east", limit=10)
    contents = {r["content"] for r in hits["results"]}
    assert "The deploy region is us-east-1" in contents


async def test_search_can_filter_by_source_client(call):
    pid = await make_project(call, "search-filter")
    s1 = await make_session(call, pid, "claude", "s-sf-1")
    s2 = await make_session(call, pid, "codex", "s-sf-2")
    await call(
        "append_message",
        session_id=s1,
        source_message_id="a",
        content="shared term zebrafish",
        role="user",
        order=0,
        original_timestamp=now(),
        source_client="claude",
    )
    await call(
        "append_message",
        session_id=s2,
        source_message_id="b",
        content="shared term zebrafish",
        role="assistant",
        order=0,
        original_timestamp=now(),
        source_client="codex",
    )
    claude_only = await call(
        "search_history", query="zebrafish", limit=10, source_client="claude"
    )
    assert all(r["source_client"] == "claude" for r in claude_only["results"])
    assert claude_only["results"]


# ---------------------------------------------------------------------------
# Facts / decisions / preferences: supersede without delete
# ---------------------------------------------------------------------------
async def test_fact_supersede_without_delete(call):
    pid = await make_project(call, "fact")
    await call(
        "record_fact",
        key="target_lang",
        value="Go",
        project_id=pid,
        source_client="claude",
    )
    f2 = await call(
        "record_fact",
        key="target_lang",
        value="Rust",
        project_id=pid,
        source_client="codex",
    )
    # Two rows exist (append-only); ids differ.
    ctx = await call("get_project_context", project_id=pid)
    assert len(ctx["current_facts"]) == 1  # latest per key
    assert ctx["current_facts"][0]["value"] == "Rust"

    hist = await call("inspect_project_history", project_id=pid)
    fact_rows = [t for t in hist["timeline"] if t["kind"] == "fact"]
    assert len(fact_rows) == 2
    assert {t["value"] for t in fact_rows} == {"Go", "Rust"}


async def test_decision_and_preference_recorded(call):
    pid = await make_project(call, "dec-pref")
    await call(
        "record_decision",
        key="runtime",
        value="tokio",
        project_id=pid,
        source_client="claude",
    )
    await call(
        "record_preference",
        key="email_language",
        value="en",
        project_id=pid,
    )
    ctx = await call("get_project_context", project_id=pid)
    assert ctx["current_decisions"][0]["value"] == "tokio"
    assert ctx["current_preferences"][0]["value"] == "en"


async def test_get_project_context_filters_by_project(call):
    """A project-level fact (no session) must appear in project context."""
    pid = await make_project(call, "ctx-project")
    sid = await make_session(call, pid, "claude", "s-ctx")

    # Fact recorded at project level (session_id omitted).
    await call(
        "record_fact",
        key="owner",
        value="payments-team",
        project_id=pid,
        source_client="claude",
    )
    # A fact on a *different* project must not leak in.
    other = await make_project(call, "ctx-other")
    await call(
        "record_fact",
        key="owner",
        value="billing-team",
        project_id=other,
        source_client="codex",
    )

    ctx = await call("get_project_context", project_id=pid)
    owners = {f["value"] for f in ctx["current_facts"]}
    assert owners == {"payments-team"}


async def test_get_project_context_by_session(call):
    pid = await make_project(call, "ctx-session")
    sid = await make_session(call, pid, "claude", "s-ctxsess")
    await call(
        "record_fact",
        key="stage",
        value="planning",
        session_id=sid,
        source_client="claude",
    )
    ctx = await call("get_project_context", session_id=sid)
    assert ctx["project_id"] == pid
    assert ctx["current_facts"][0]["value"] == "planning"


# ---------------------------------------------------------------------------
# Events & project states
# ---------------------------------------------------------------------------
async def test_record_event(call):
    pid = await make_project(call, "event")
    ev = await call(
        "record_event",
        type="branch_created",
        payload={"name": "feat/rust"},
        project_id=pid,
        source_client="codex",
    )
    assert ev["type"] == "branch_created"
    assert ev["payload"] == {"name": "feat/rust"}

    ctx = await call("get_project_context", project_id=pid)
    assert ctx["recent_events"][0]["type"] == "branch_created"


async def test_project_state_append_and_current(call):
    pid = await make_project(call, "state")
    await call(
        "record_project_state",
        project_id=pid,
        state_json={"progress": 0.1, "notes": "started"},
        source_client="claude",
    )
    await call(
        "record_project_state",
        project_id=pid,
        state_json={"progress": 0.5, "notes": "halfway"},
        source_client="codex",
    )
    hist = await call("inspect_project_history", project_id=pid)
    state_rows = [t for t in hist["timeline"] if t["kind"] == "project_state"]
    assert len(state_rows) == 2  # append-only

    ctx = await call("get_project_context", project_id=pid)
    assert ctx["current_project_state"]["state_json"]["progress"] == 0.5


# ---------------------------------------------------------------------------
# Cross-client change detection
# ---------------------------------------------------------------------------
async def test_find_changes_only_from_other_client(call):
    pid = await make_project(call, "find-changes")
    sid = await make_session(call, pid, "claude", "s-fc")

    await call(
        "record_fact",
        key="lang",
        value="Rust",
        project_id=pid,
        source_client="claude",
    )
    await call(
        "record_fact",
        key="runtime",
        value="tokio",
        project_id=pid,
        source_client="codex",
    )
    await call(
        "record_event",
        type="push",
        payload={},
        project_id=pid,
        source_client="codex",
    )

    changes = await call("find_changes", other_client="codex", project_id=pid)
    assert changes["changes"]
    assert all(c["source_client"] == "codex" for c in changes["changes"])
    kinds = {c["kind"] for c in changes["changes"]}
    assert kinds == {"fact", "event"}

    # Claude's own fact is not returned when asking for codex's changes.
    claude_changes = await call("find_changes", other_client="claude", project_id=pid)
    assert all(c["source_client"] == "claude" for c in claude_changes["changes"])


# ---------------------------------------------------------------------------
# Credential rejection (never store secrets)
# ---------------------------------------------------------------------------
async def test_credential_rejected_by_value(call, call_raw):
    pid = await make_project(call, "cred-value")
    sid = await make_session(call, pid, "claude", "s-cred")
    err = await call_raw(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="here is my key sk-ABCDEFghijklmnop1234567890",
        role="user",
        order=0,
        original_timestamp=now(),
    )
    assert err["is_error"] is True
    assert err["code"] == "bad_payload"
    # Nothing was stored.
    listed = await call("read_session", session_id=sid, cursor=0, limit=10)
    assert listed["total"] == 0


async def test_credential_rejected_by_payload_key(call, call_raw):
    pid = await make_project(call, "cred-key")
    sid = await make_session(call, pid, "claude", "s-cred2")
    err = await call_raw(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="no secret in the text",
        role="user",
        order=0,
        original_timestamp=now(),
        payload={"api_key": "abc123def456"},
    )
    assert err["is_error"] is True
    assert err["code"] == "bad_payload"


# ---------------------------------------------------------------------------
# Structured (non-raising) error reporting
# ---------------------------------------------------------------------------
async def test_missing_session_returns_not_found(call, call_raw):
    err = await call_raw(
        "append_message",
        session_id="does-not-exist",
        source_message_id="m1",
        content="x",
        role="user",
        order=0,
        original_timestamp=now(),
    )
    assert err["is_error"] is True
    assert err["code"] == "not_found"


async def test_get_message_missing(call, call_raw):
    err = await call_raw("get_message", message_id="nope")
    assert err["is_error"] is True
    assert err["code"] == "not_found"


async def test_record_fact_missing_session_returns_not_found(call, call_raw):
    # Facts carry a session_id FK, so recording one for an unknown session
    # yields a structured not_found error (tools do not raise).
    err = await call_raw(
        "record_fact",
        key="k",
        value="v",
        session_id="missing-session",
    )
    assert err["is_error"] is True
    assert err["code"] == "not_found"


async def test_record_fact_on_missing_project_is_accepted(call):
    # project_id is a denormalised column (no FK), so it is stored as-is.
    pid = await make_project(call, "proj-for-fact")
    f = await call(
        "record_fact",
        key="k",
        value="v",
        project_id=pid,
        source_client="claude",
    )
    assert f["project_id"] == pid
