"""Tests for the optional pgvector store.

We don't run a real Postgres in CI; instead we mock the engine + the
session.execute path so the Postgres-only paths still get coverage, plus
we exercise the SQLite fallback in `embeddings.search`.
"""
import pytest

from filenergy.services import pgvector_store


# ---- defaults: SQLite ----


def test_is_postgres_false_for_sqlite(app):
    assert pgvector_store.is_postgres() is False


def test_is_available_false_when_not_postgres(app):
    assert pgvector_store.is_available() is False


def test_is_postgres_handles_engine_failure(app, monkeypatch):
    """If engine inspection blows up, return False — not raise."""

    def boom():
        raise RuntimeError("no engine here")

    monkeypatch.setattr(pgvector_store, "_engine", boom)
    assert pgvector_store.is_postgres() is False


def test_enable_pgvector_refuses_on_sqlite(app):
    with pytest.raises(RuntimeError, match="Postgres-only"):
        pgvector_store.enable_pgvector()


def test_reembed_existing_refuses_when_unavailable(app):
    with pytest.raises(RuntimeError):
        pgvector_store.reembed_existing()


def test_knn_search_refuses_when_unavailable(app, workspace):
    with pytest.raises(RuntimeError):
        pgvector_store.knn_search(workspace, [1.0, 0.0, 0.0], k=3)


def test_embeddings_search_falls_back_when_pgvector_unavailable(
    db, user, workspace, _stub_external_services
):
    """The standard JSON+numpy path runs when pgvector is not available."""
    import json
    from filenergy.models import Chunk, File
    from filenergy.services import embeddings

    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="x.txt", path="/x", url="hh",
    )
    db.session.add(f)
    db.session.commit()
    db.session.add(Chunk(
        file_id=f.id, position=0, content="hi",
        embedding=json.dumps([1.0, 0.0, 0.0]),
    ))
    db.session.commit()
    results = embeddings.search(workspace, "anything", k=2)
    assert results and results[0][0].content == "hi"


# ---- mocked Postgres paths ----


def _pretend_postgres(monkeypatch):
    monkeypatch.setattr(pgvector_store, "is_postgres", lambda: True)
    monkeypatch.setattr(pgvector_store, "is_available", lambda: True)


class _FakeConn:
    def __init__(self, captured: list, fetch_rows=()):
        self.captured = captured
        self._rows = list(fetch_rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def exec_driver_sql(self, sql, params=None):
        self.captured.append({"sql": sql, "params": params})

        class _R:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        return _R(self._rows)


class _FakeEngine:
    def __init__(self, captured: list, fetch_rows=()):
        self.captured = captured
        self._rows = fetch_rows

    def begin(self):
        return _FakeConn(self.captured, self._rows)


def test_enable_pgvector_runs_three_statements(monkeypatch, app):
    _pretend_postgres(monkeypatch)
    captured: list = []
    monkeypatch.setattr(
        pgvector_store, "_engine",
        lambda: _FakeEngine(captured),
    )
    pgvector_store.enable_pgvector(dim=512)
    sqls = [c["sql"] for c in captured]
    assert any("CREATE EXTENSION" in s for s in sqls)
    assert any("ADD COLUMN" in s and "vector(512)" in s for s in sqls)
    assert any("CREATE INDEX" in s and "ivfflat" in s for s in sqls)


def test_enable_pgvector_refuses_when_dep_missing(monkeypatch, app):
    monkeypatch.setattr(pgvector_store, "is_postgres", lambda: True)
    monkeypatch.setattr(pgvector_store, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="pgvector"):
        pgvector_store.enable_pgvector()


def test_reembed_existing_back_fills_rows(monkeypatch, app):
    _pretend_postgres(monkeypatch)
    captured: list = []
    rows = [(1, "[0.1, 0.2]"), (2, "[0.3, 0.4]")]
    monkeypatch.setattr(
        pgvector_store, "_engine",
        lambda: _FakeEngine(captured, fetch_rows=rows),
    )
    n = pgvector_store.reembed_existing(batch=10)
    assert n == 2
    updates = [c for c in captured if "UPDATE" in c["sql"]]
    assert len(updates) == 2


def test_reembed_existing_skips_invalid_json(monkeypatch, app):
    _pretend_postgres(monkeypatch)
    rows = [(1, "not-json")]
    monkeypatch.setattr(
        pgvector_store, "_engine",
        lambda: _FakeEngine([], fetch_rows=rows),
    )
    assert pgvector_store.reembed_existing() == 0


def test_knn_search_routes_to_postgres_via_search(
    monkeypatch, app, db, user, workspace
):
    """When pgvector reports available, embeddings.search delegates to knn_search."""
    import json
    from filenergy.models import Chunk, File
    from filenergy.services import embeddings

    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="x.txt", path="/x", url="hh",
    )
    db.session.add(f)
    db.session.commit()
    chunk = Chunk(
        file_id=f.id, position=0, content="apples",
        embedding=json.dumps([1.0, 0.0, 0.0]),
    )
    db.session.add(chunk)
    db.session.commit()

    captured = {}

    def fake_knn(ws, query_vec, k, *, collection_id=None, file_id=None):
        captured["called"] = True
        captured["k"] = k
        return [(chunk, 0.99)]

    monkeypatch.setattr(pgvector_store, "is_available", lambda: True)
    monkeypatch.setattr(pgvector_store, "knn_search", fake_knn)
    results = embeddings.search(workspace, "x", k=3)
    assert captured.get("called")
    assert results[0][0].content == "apples"


def _stub_session_execute(monkeypatch, captured: dict, rows=()):
    """Replace db.session.execute with a recorder."""
    from filenergy import db as _db

    class _Result:
        def fetchall(self):
            return rows

    def fake(sql, params=None):
        captured["sql"] = str(sql)
        captured["params"] = params
        return _Result()

    monkeypatch.setattr(_db.session, "execute", fake)


def _stub_chunk_lookup(monkeypatch, chunks):
    """Make Chunk.query.filter(...).all() return our fake list."""
    from filenergy.models import Chunk

    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return list(chunks)

    monkeypatch.setattr(Chunk, "query", _Q())


def test_knn_search_query_includes_workspace_filter(monkeypatch, app, workspace):
    _pretend_postgres(monkeypatch)
    captured: dict = {}
    _stub_session_execute(monkeypatch, captured, rows=[])
    out = pgvector_store.knn_search(workspace, [0.1, 0.2], k=2)
    assert out == []
    assert "embedding_vec" in captured["sql"]
    assert captured["params"]["k"] == 2
    assert captured["params"]["ws"] == workspace.id


def test_knn_search_with_file_id_filter(monkeypatch, app, workspace):
    _pretend_postgres(monkeypatch)
    captured: dict = {}
    _stub_session_execute(monkeypatch, captured, rows=[])
    pgvector_store.knn_search(workspace, [1.0, 0.0], k=5, file_id=42)
    assert captured["params"]["fid"] == 42


def test_knn_search_with_collection_filter(monkeypatch, app, workspace):
    _pretend_postgres(monkeypatch)
    captured: dict = {}
    _stub_session_execute(monkeypatch, captured, rows=[])
    pgvector_store.knn_search(workspace, [1.0, 0.0], k=5, collection_id=99)
    assert captured["params"]["cid"] == 99


def test_knn_search_returns_chunks_in_score_order(monkeypatch, app, db, user, workspace):
    _pretend_postgres(monkeypatch)
    from filenergy.models import Chunk, File

    f = File(user_id=user.id, workspace_id=workspace.id,
             name="x", path="/x", url="hh")
    db.session.add(f)
    db.session.commit()
    c1 = Chunk(file_id=f.id, position=0, content="A", embedding="[]")
    c2 = Chunk(file_id=f.id, position=1, content="B", embedding="[]")
    db.session.add_all([c1, c2])
    db.session.commit()

    captured: dict = {}
    _stub_session_execute(
        monkeypatch, captured,
        # Higher score first; pgvector_store should preserve that order.
        rows=[(c1.id, 0.95), (c2.id, 0.50)],
    )
    out = pgvector_store.knn_search(workspace, [1.0, 0.0], k=2)
    assert [c.content for c, _ in out] == ["A", "B"]
    assert out[0][1] > out[1][1]


def test_is_available_handles_missing_pgvector(monkeypatch):
    monkeypatch.setattr(pgvector_store, "is_postgres", lambda: True)
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pgvector":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Bust the import cache so the import statement actually re-runs.
    import sys
    sys.modules.pop("pgvector", None)
    assert pgvector_store.is_available() is False
