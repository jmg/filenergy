import json

import pytest

from filenergy import settings
from filenergy.models import Chunk, File
from filenergy.services import embeddings


def test_is_configured_true_in_test(monkeypatch):
    # default fixture stubs is_configured() to True
    assert embeddings.is_configured()


def test_search_returns_empty_when_unconfigured(db, user, monkeypatch):
    monkeypatch.setattr(embeddings, "is_configured", lambda: False)
    assert embeddings.search(user, "hi", k=3) == []


def test_search_returns_empty_when_no_chunks(db, user):
    assert embeddings.search(user, "hi", k=3) == []


def test_search_ranks_by_cosine(db, user):
    f = File(user_id=user.id, name="a.txt", path="/tmp/a.txt", url="h")
    db.session.add(f)
    db.session.commit()
    db.session.add_all([
        Chunk(file_id=f.id, position=0, content="match", embedding=json.dumps([1.0, 0, 0])),
        Chunk(file_id=f.id, position=1, content="other", embedding=json.dumps([0.0, 1.0, 0])),
    ])
    db.session.commit()
    results = embeddings.search(user, "anything", k=2)
    assert results[0][0].content == "match"
    assert results[0][1] > results[1][1]


def test_search_filters_by_user(db, user):
    from filenergy.models import User

    other = User(email="b@b.co", username="b")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()

    f1 = File(user_id=user.id, name="mine.txt", path="/tmp/mine", url="h1")
    f2 = File(user_id=other.id, name="theirs.txt", path="/tmp/theirs", url="h2")
    db.session.add_all([f1, f2])
    db.session.commit()
    db.session.add_all([
        Chunk(file_id=f1.id, position=0, content="mine", embedding=json.dumps([1, 0, 0])),
        Chunk(file_id=f2.id, position=0, content="theirs", embedding=json.dumps([1, 0, 0])),
    ])
    db.session.commit()

    results = embeddings.search(user, "x", k=10)
    assert len(results) == 1
    assert results[0][0].content == "mine"


def test_embeddings_unavailable_without_key(monkeypatch, real_emb_client):
    monkeypatch.setattr(settings, "VOYAGE_API_KEY", "")
    with pytest.raises(embeddings.EmbeddingsUnavailable):
        real_emb_client()


def test_embed_documents_empty_returns_empty(monkeypatch):
    # Even with stubs, the empty short-circuit should fire.
    assert embeddings.embed_documents([]) == []


def test_embed_documents_calls_voyage():
    """The autouse FakeVoyageClient is recorded — assert it was called."""
    result = embeddings.embed_documents(["hello"])
    assert result == [[1.0, 0.0, 0.0]]


def test_embed_query_calls_voyage():
    assert embeddings.embed_query("hi") == [1.0, 0.0, 0.0]


def test_embeddings_unavailable_without_voyageai_package(monkeypatch, real_emb_client):
    """Forced ImportError of voyageai should raise EmbeddingsUnavailable."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "voyageai":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(settings, "VOYAGE_API_KEY", "fake")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(embeddings.EmbeddingsUnavailable):
        real_emb_client()
