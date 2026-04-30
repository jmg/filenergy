import io
import os

from filenergy.models import Chunk, Event, File
from filenergy.services import embeddings, events, extraction
from filenergy.services.file import FileService


class _FormStub:
    def __init__(self, **kwargs):
        self._d = kwargs

    def get(self, key, default=""):
        return self._d.get(key, default)


class _FilesStub:
    def __init__(self, file_obj):
        self._f = file_obj

    def get(self, key):
        return self._f


class _RequestStub:
    def __init__(self, file_obj, form=None):
        self.files = _FilesStub(file_obj)
        self.form = _FormStub(**(form or {}))


class _UploadFile:
    def __init__(self, name, data):
        self.filename = name
        self._data = data

    def save(self, path):
        with open(path, "wb") as fd:
            fd.write(self._data)


def test_save_file_no_file_returns_empty_json(db, user, app):
    with app.test_request_context():
        req = _RequestStub(file_obj=None)
        out = FileService().save_file(req, user)
    assert out == "[]"


def test_save_file_indexes_text_file(db, user, app):
    req = _RequestStub(_UploadFile("hello.txt", b"hello world"))
    with app.test_request_context():
        out = FileService().save_file(req, user)
    assert "hello.txt" in out
    f = File.query.filter_by(user_id=user.id).one()
    assert f.size_bytes > 0
    assert f.text_content == "hello world"
    assert f.indexed_at is not None
    assert f.url and len(f.url) == 64
    assert Chunk.query.filter_by(file_id=f.id).count() >= 1


def test_save_file_skips_index_when_voyage_not_configured(
    db, user, app, monkeypatch
):
    monkeypatch.setattr(embeddings, "is_configured", lambda: False)
    req = _RequestStub(_UploadFile("hello.txt", b"hello"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.filter_by(user_id=user.id).one()
    assert f.indexed_at is None
    assert Chunk.query.filter_by(file_id=f.id).count() == 0


def test_save_file_skips_index_for_unindexable_type(db, user, app):
    req = _RequestStub(_UploadFile("blob.bin", b"\x00\x01"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.filter_by(user_id=user.id).one()
    assert f.indexed_at is None
    assert f.text_content is None


def test_index_file_handles_extraction_returning_none(
    db, user, app, monkeypatch
):
    monkeypatch.setattr(extraction, "extract_text", lambda path: None)
    req = _RequestStub(_UploadFile("a.txt", b"x"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.filter_by(user_id=user.id).one()
    assert f.indexed_at is None
    assert f.index_error == "no text extracted"


def test_index_file_handles_empty_chunks(db, user, app, monkeypatch):
    monkeypatch.setattr(extraction, "extract_text", lambda path: "ok")
    monkeypatch.setattr(extraction, "chunk_text", lambda *a, **k: [])
    req = _RequestStub(_UploadFile("a.txt", b"ok"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.filter_by(user_id=user.id).one()
    assert f.indexed_at is None
    assert f.index_error == "empty after chunking"


def test_index_file_handles_voyage_error(db, user, app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("voyage 502")

    monkeypatch.setattr(embeddings, "embed_documents", boom)
    req = _RequestStub(_UploadFile("a.txt", b"hello"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.filter_by(user_id=user.id).one()
    assert f.indexed_at is None
    assert f.index_error and "voyage 502" in f.index_error
    assert any(e.type == events.FILE_INDEX_FAILED for e in Event.query.all())


def test_reindex_replaces_existing_chunks(db, user, app):
    req = _RequestStub(_UploadFile("a.txt", b"first version"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.filter_by(user_id=user.id).one()
    first_chunk_count = Chunk.query.filter_by(file_id=f.id).count()
    # Re-write file content and re-index
    with open(f.path, "wb") as fd:
        fd.write(b"second version, longer this time " * 50)
    with app.test_request_context():
        ok = FileService().index_file(f)
    assert ok
    chunks = Chunk.query.filter_by(file_id=f.id).all()
    assert len(chunks) >= first_chunk_count


def test_delete_removes_db_row_and_file(db, user, app):
    req = _RequestStub(_UploadFile("kill.txt", b"data"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.filter_by(user_id=user.id).one()
    path = f.path
    with app.test_request_context():
        assert FileService().delete(f) is True
    assert File.query.count() == 0
    assert not os.path.exists(path)


def test_delete_with_none_returns_false(app):
    with app.test_request_context():
        assert FileService().delete(None) is False


def test_delete_handles_missing_file_on_disk(db, user, app):
    req = _RequestStub(_UploadFile("ghost.txt", b"x"))
    with app.test_request_context():
        FileService().save_file(req, user)
    f = File.query.first()
    os.remove(f.path)
    with app.test_request_context():
        assert FileService().delete(f) is True


def test_get_size_uses_size_bytes_first(db, user):
    f = File(user_id=user.id, name="a", path="/nope", url="h", size_bytes=2000)
    assert FileService().get_size(f) == 2.0


def test_get_size_falls_back_to_disk(db, user, tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"abcd")
    f = File(user_id=user.id, name="x.txt", path=str(p), url="h", size_bytes=0)
    assert FileService().get_size(f) > 0


def test_get_size_returns_zero_for_missing_file(db, user):
    f = File(user_id=user.id, name="x", path="/nope", url="h", size_bytes=0)
    assert FileService().get_size(f) == 0


def test_get_content_reads_binary(db, user, tmp_path):
    p = tmp_path / "b.bin"
    p.write_bytes(b"\x00\xffabc")
    f = File(user_id=user.id, name="b", path=str(p), url="h")
    assert FileService().get_content(f) == b"\x00\xffabc"


def test_search_authenticated_user_sees_own_and_public(db, user, app):
    from filenergy.models import User as U

    other = U(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    db.session.add_all([
        File(user_id=user.id, name="mine.txt", path="/x", url="u1"),
        File(user_id=other.id, name="theirs-public.txt", path="/x", url="u2", is_public=True),
        File(user_id=other.id, name="theirs-private.txt", path="/x", url="u3"),
    ])
    db.session.commit()
    found = FileService().search(user, "theirs")
    names = sorted(f.name for f in found)
    assert names == ["theirs-public.txt"]


def test_search_anonymous_only_public(db, user):
    class _Anon:
        is_authenticated = False

    db.session.add(File(user_id=user.id, name="public.txt", path="/x", url="u1", is_public=True))
    db.session.add(File(user_id=user.id, name="private.txt", path="/x", url="u2"))
    db.session.commit()
    found = FileService().search(_Anon(), "")
    assert {f.name for f in found} == {"public.txt"}
