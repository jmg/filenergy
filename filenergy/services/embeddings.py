"""Voyage AI embeddings + in-memory cosine retrieval over a user's chunks."""
from __future__ import annotations

import json
from functools import lru_cache

import numpy as np

from filenergy import settings
from filenergy.models import Chunk, File


class EmbeddingsUnavailable(RuntimeError):
    """Raised when VOYAGE_API_KEY is missing or the SDK isn't installed."""


@lru_cache(maxsize=1)
def _client():
    if not settings.VOYAGE_API_KEY:
        raise EmbeddingsUnavailable(
            "VOYAGE_API_KEY is not set. Set it in your environment to enable indexing."
        )
    try:
        import voyageai
    except ImportError as exc:
        raise EmbeddingsUnavailable("voyageai package not installed") from exc
    return voyageai.Client(api_key=settings.VOYAGE_API_KEY)


def is_configured() -> bool:
    return bool(settings.VOYAGE_API_KEY)


def embed_documents(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    response = _client().embed(
        texts=texts,
        model=settings.VOYAGE_EMBED_MODEL,
        input_type="document",
    )
    return response.embeddings


def embed_query(text: str) -> list[float]:
    response = _client().embed(
        texts=[text],
        model=settings.VOYAGE_EMBED_MODEL,
        input_type="query",
    )
    return response.embeddings[0]


def search(workspace, query: str, k: int) -> list[tuple[Chunk, float]]:
    """Return the top-k chunks in the workspace, ranked by cosine similarity."""
    if not is_configured() or workspace is None:
        return []

    rows = (
        Chunk.query.join(File, Chunk.file_id == File.id)
        .filter(File.workspace_id == workspace.id)
        .filter(Chunk.embedding.isnot(None))
        .all()
    )
    if not rows:
        return []

    matrix = np.array([json.loads(c.embedding) for c in rows], dtype=np.float32)
    query_vec = np.array(embed_query(query), dtype=np.float32)

    # Both Voyage embeddings and the query are unit-normalized by the API,
    # but renormalize defensively so cosine == dot.
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12
    query_vec /= np.linalg.norm(query_vec) + 1e-12

    scores = matrix @ query_vec
    top = np.argsort(-scores)[:k]
    return [(rows[i], float(scores[i])) for i in top]
