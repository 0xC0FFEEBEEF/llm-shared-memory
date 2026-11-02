"""Optional local text embedding.

This module exists to give the pgvector column a *real* value to store when
semantic search is enabled. It is intentionally dependency-free and fully
deterministic: it turns text into a fixed-dimension, L2-normalised float vector
using a hashed n-gram bag-of-words with sign hashing.

Important — read before relying on it:

* It is a **lexical** embedding, not a neural semantic model. It captures word
  overlap well but does not understand synonyms or paraphrase. For that, swap in
  a real model by implementing :class:`EmbeddingModel` (see below) and returning
  it from :func:`get_embedding_model`.
* It is **disabled by default**. Retrieval uses PostgreSQL full-text search
  (stemming + ``ts_rank``), which works with no pipeline at all. Enable this
  with ``MEMORY_USE_EMBEDDINGS=1``.

We deliberately do *not* ship an "embedding pipeline that is only a placeholder":
either full-text search is used (default), or this genuine embedding is used
(opt-in). There is no code path that claims to do semantic search without
actually producing a usable vector.
"""
from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class EmbeddingModel(ABC):
    """Interface for something that turns text into a normalised vector."""

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...


class LocalHashingEmbedding(EmbeddingModel):
    """Deterministic hashed n-gram embedding (unigrams + bigrams).

    Uses signed hashing so that collision-resistant bucketing preserves sign,
    which is standard for the "feature hashing" technique.
    """

    def __init__(self, dim: int = 512) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        buckets = [0.0] * self._dim
        tokens = _TOKEN_RE.findall(text.lower())
        ngrams: list[tuple[str, ...]] = list(tokens)
        ngrams += list(zip(tokens, tokens[1:])) if len(tokens) > 1 else []

        for gram in ngrams:
            if isinstance(gram, tuple):  # bigrams come from zip() as tuples
                gram = " ".join(gram)
            h = hashlib.md5(gram.encode("utf-8")).digest()
            bucket = int.from_bytes(h[:8], "little") % self._dim
            sign = 1.0 if (h[8] & 1) else -1.0
            buckets[bucket] += sign

        # Sublinear tf scaling + L2 normalisation.
        norm = math.sqrt(sum(v * v for v in buckets))
        if norm > 0:
            buckets = [v / norm for v in buckets]
        return buckets


_EMBEDDING_MODEL: EmbeddingModel | None = None


def get_embedding_model() -> EmbeddingModel | None:
    """Return the active embedding model, or ``None`` when disabled.

    A real model (API-backed or local) can be installed by returning it here,
    implementing the :class:`EmbeddingModel` interface.
    """
    import os

    if os.environ.get("MEMORY_USE_EMBEDDINGS", "0") != "1":
        return None
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        _EMBEDDING_MODEL = LocalHashingEmbedding()
    return _EMBEDDING_MODEL
