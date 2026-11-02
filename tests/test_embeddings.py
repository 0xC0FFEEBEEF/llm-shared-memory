"""Tests for the optional pgvector semantic-search path.

These prove the embedding search is *real* (not a placeholder): when
``MEMORY_USE_EMBEDDINGS=1`` every message gets a genuine, deterministic local
vector stored in the pgvector column, and ``search_history`` ranks by cosine
similarity over that column. The default (embeddings off) path is covered in
``test_mcp_tools.py`` via full-text search.

The fixture toggles the env var so this suite neither disturbs nor is disturbed
by the other tests.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from memory import embeddings as embedding_lib
from memory.models import Message

TS = datetime.now(timezone.utc).isoformat()


async def _make_project(call, name):
    return (await call("create_project", name=name))["id"]


async def _make_session(call, pid, provider, src):
    return (
        await call("start_session", provider=provider, source_session_id=src, project_id=pid)
    )["session_id"]


@pytest.fixture
def embeddings_on(monkeypatch):
    monkeypatch.setenv("MEMORY_USE_EMBEDDINGS", "1")
    # Warm/cache a real model instance so tests can assert on its type.
    model = embedding_lib.get_embedding_model()
    assert isinstance(model, embedding_lib.LocalHashingEmbedding)
    yield model


def test_embedding_model_is_real_and_deterministic():
    m = embedding_lib.LocalHashingEmbedding(dim=512)
    v1 = m.embed("the quick brown fox")
    v2 = m.embed("the quick brown fox")
    assert len(v1) == 512
    assert v1 == v2  # deterministic
    import math

    assert abs(math.sqrt(sum(x * x for x in v1)) - 1.0) < 1e-6  # L2 normalised


async def test_embedding_column_is_populated(call, embeddings_on, db):
    pid = await _make_project(call, "embed-store")
    sid = await _make_session(call, pid, "claude", "s-embed-store")
    await call(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="deploy to us-east-1 on aws",
        role="user",
        order=0,
        original_timestamp=TS,
        source_client="claude",
    )
    rows = (
        await db.execute(select(Message.search_embedding).where(Message.session_id == sid))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0] is not None  # a real vector, not NULL
    assert len(rows[0]) == 512


async def test_vector_search_ranks(call, embeddings_on):
    pid = await _make_project(call, "embed-search")
    sid = await _make_session(call, pid, "claude", "s-embed-search")
    await call(
        "append_message",
        session_id=sid,
        source_message_id="m1",
        content="Kubernetes orchestrates containers in us-east-1",
        role="user",
        order=0,
        original_timestamp=TS,
        source_client="claude",
    )
    await call(
        "append_message",
        session_id=sid,
        source_message_id="m2",
        content="A chocolate cake needs sugar butter and flour",
        role="assistant",
        order=1,
        original_timestamp=TS,
        source_client="claude",
    )
    hits = await call("search_history", query="Kubernetes containers orchestrates", limit=10)
    assert hits["results"], "vector search returned nothing"
    # The Kubernetes message shares vocabulary with the query and ranks first.
    assert hits["results"][0]["content"].lower().startswith("kubernetes")
    # Hybrid search still returns the FTS match for the cake message too.
    contents = [r["content"].lower() for r in hits["results"]]
    assert any(c.startswith("a chocolate cake") for c in contents)
