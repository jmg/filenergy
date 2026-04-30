"""Optional pgvector-backed embedding store.

When the SQLAlchemy URI is Postgres and the `pgvector` Python package is
installed, calling `enable_pgvector()` creates the extension + a vector
column on `chunk` and switches `embeddings.search` to a server-side
ORDER BY cosine distance query.

For SQLite (the default and what tests use) this module is a no-op.

Public surface:
    is_postgres() -> bool
    is_available() -> bool                # postgres + pgvector dep installed
    enable_pgvector()                     # idempotent migration
    knn_search(workspace, query_vec, k, *, collection_id=None, file_id=None)
        -> list[(Chunk, score)]
"""
from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np

from filenergy import db
from filenergy.models import Chunk, File

log = logging.getLogger(__name__)


def _engine():
    """Indirection so tests can swap in a fake engine."""
    return db.engine


def is_postgres() -> bool:
    try:
        return _engine().url.get_backend_name() == "postgresql"
    except Exception:
        return False


def is_available() -> bool:
    if not is_postgres():
        return False
    try:
        import pgvector  # noqa: F401
        return True
    except ImportError:
        return False


def enable_pgvector(dim: int = 512) -> None:
    """Create the pgvector extension + a `chunk.embedding_vec` column.

    Idempotent. Safe to call from a one-off CLI command. The column type
    is `vector(dim)`; default 512 fits Voyage's `voyage-3-lite`.
    """
    if not is_postgres():
        raise RuntimeError("pgvector is Postgres-only")
    if not is_available():
        raise RuntimeError("Install the `pgvector` Python package first")
    with _engine().begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        conn.exec_driver_sql(
            f"ALTER TABLE chunk ADD COLUMN IF NOT EXISTS "
            f"embedding_vec vector({dim})"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_chunk_embedding_vec "
            "ON chunk USING ivfflat (embedding_vec vector_cosine_ops) "
            "WITH (lists = 100)"
        )


def reembed_existing(batch: int = 500) -> int:
    """Copy `chunk.embedding` (JSON text) into the new vector column.

    Run once after `enable_pgvector()` to back-fill the column. Returns
    the row count touched.
    """
    if not is_available():
        raise RuntimeError("pgvector not available")
    n = 0
    with _engine().begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT id, embedding FROM chunk "
            "WHERE embedding IS NOT NULL AND embedding_vec IS NULL "
            f"LIMIT {batch}"
        ).fetchall()
        for cid, raw in rows:
            try:
                vec = json.loads(raw)
            except Exception:
                continue
            literal = "[" + ",".join(repr(float(v)) for v in vec) + "]"
            conn.exec_driver_sql(
                "UPDATE chunk SET embedding_vec = %s::vector WHERE id = %s",
                (literal, cid),
            )
            n += 1
    return n


def knn_search(
    workspace, query_vec: list[float], k: int,
    *, collection_id: int | None = None, file_id: int | None = None,
) -> list[tuple[Chunk, float]]:
    """ORDER BY cosine distance ASC, LIMIT k. Postgres + pgvector only."""
    if not is_available():
        raise RuntimeError("pgvector not available")
    literal = "[" + ",".join(repr(float(v)) for v in query_vec) + "]"
    sql_filters = ["c.embedding_vec IS NOT NULL", "f.workspace_id = :ws"]
    params: dict[str, Any] = {"ws": workspace.id, "qv": literal, "k": k}
    if file_id is not None:
        sql_filters.append("f.id = :fid")
        params["fid"] = file_id
    elif collection_id is not None:
        sql_filters.append("f.collection_id = :cid")
        params["cid"] = collection_id

    sql = (
        "SELECT c.id, (1 - (c.embedding_vec <=> :qv::vector)) AS score "
        "FROM chunk c JOIN file f ON f.id = c.file_id "
        f"WHERE {' AND '.join(sql_filters)} "
        "ORDER BY c.embedding_vec <=> :qv::vector "
        "LIMIT :k"
    )
    rows = db.session.execute(db.text(sql), params).fetchall()
    if not rows:
        return []
    chunk_ids = [r[0] for r in rows]
    scores = {r[0]: float(r[1]) for r in rows}
    chunks = Chunk.query.filter(Chunk.id.in_(chunk_ids)).all()
    chunks.sort(key=lambda c: -scores[c.id])
    return [(c, scores[c.id]) for c in chunks]
