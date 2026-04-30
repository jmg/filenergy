"""Tests for URL ingestion."""
import pytest

from filenergy.models import File
from filenergy.services import ingestion


# ---- HTML/text parser ----


def test_text_extractor_strips_scripts_and_collapses_whitespace():
    html = (
        b"<html><head><title>Hi</title>"
        b"<style>.x{color:red}</style></head>"
        b"<body><script>bad()</script>"
        b"<p>Hello   world</p>"
        b"<p>Apples</p></body></html>"
    ).decode()
    p = ingestion._TextExtractor()
    p.feed(html)
    assert p.title == "Hi"
    assert "Hello world" in p.text
    assert "bad()" not in p.text
    assert "color:red" not in p.text


def test_safe_filename_falls_back_to_title():
    out = ingestion._safe_filename_from_url(
        "https://example.com/", "Some Article Title"
    )
    assert out.endswith(".html")
    assert "Some-Article-Title" in out


def test_safe_filename_uses_path_basename():
    out = ingestion._safe_filename_from_url("https://x.com/a/b/note.txt")
    assert out == "note.txt"


# ---- fetch_url ----


def _stub_urlopen(monkeypatch, *, body=b"<p>hi</p>", ctype="text/html",
                   status=200, raise_=None):
    class _R:
        status = 200
        def __init__(self):
            self.headers = {"Content-Type": ctype}
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, n=None):
            if n is None:
                return self._body
            return self._body[:n]

    def fake(req, timeout=None):
        if raise_ is not None:
            raise raise_
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake)


def test_fetch_url_rejects_non_http():
    with pytest.raises(ingestion.IngestionError):
        ingestion.fetch_url("ftp://example.com/")


def test_fetch_url_returns_text_for_html(monkeypatch):
    _stub_urlopen(monkeypatch, body=b"<title>T</title><p>Body text</p>")
    name, text, raw = ingestion.fetch_url("https://example.com/page")
    assert text.strip()
    assert "Body text" in text
    assert raw.startswith("<title>")


def test_fetch_url_rejects_non_text_types(monkeypatch):
    _stub_urlopen(monkeypatch, body=b"\x00\x01", ctype="image/png")
    with pytest.raises(ingestion.IngestionError):
        ingestion.fetch_url("https://example.com/img.png")


def test_fetch_url_rejects_oversize(monkeypatch):
    monkeypatch.setattr(ingestion, "_MAX_BYTES", 10)
    _stub_urlopen(monkeypatch, body=b"x" * 100)
    with pytest.raises(ingestion.IngestionError):
        ingestion.fetch_url("https://example.com/")


def test_fetch_url_handles_empty_body(monkeypatch):
    _stub_urlopen(monkeypatch, body=b"<html></html>")
    with pytest.raises(ingestion.IngestionError):
        ingestion.fetch_url("https://example.com/")


def test_fetch_url_handles_http_error(monkeypatch):
    from urllib.error import HTTPError
    _stub_urlopen(
        monkeypatch,
        raise_=HTTPError("https://x", 404, "not found", {}, None),
    )
    with pytest.raises(ingestion.IngestionError):
        ingestion.fetch_url("https://example.com/")


def test_fetch_url_handles_generic_error(monkeypatch):
    _stub_urlopen(monkeypatch, raise_=OSError("dns nope"))
    with pytest.raises(ingestion.IngestionError):
        ingestion.fetch_url("https://example.com/")


def test_fetch_url_passes_through_plaintext(monkeypatch):
    _stub_urlopen(monkeypatch, body=b"plain old text", ctype="text/plain")
    name, text, raw = ingestion.fetch_url("https://example.com/raw.txt")
    assert "plain old text" in text


def test_fetch_url_handles_html_parser_failure(monkeypatch):
    """If the HTMLParser raises, we still get the raw body as text."""
    _stub_urlopen(monkeypatch, body=b"<p>fine</p>")
    monkeypatch.setattr(ingestion._TextExtractor, "feed",
                        lambda self, x: (_ for _ in ()).throw(RuntimeError("boom")))
    name, text, raw = ingestion.fetch_url("https://example.com/")
    assert "fine" in text


# ---- ingest_url end-to-end ----


def test_ingest_url_persists_and_indexes(monkeypatch, db, user, workspace, app):
    _stub_urlopen(
        monkeypatch,
        body=b"<title>Doc</title><p>Apples are red.</p>",
    )
    with app.test_request_context():
        f = ingestion.ingest_url(
            user=user, workspace=workspace, url="https://example.com/x"
        )
    assert f.id is not None
    assert f.workspace_id == workspace.id
    assert f.text_content and "Apples" in f.text_content
    # Sync indexing in tests means chunks should exist.
    assert f.indexed_at is not None


def test_ingest_url_async_path(monkeypatch, db, user, workspace, app):
    """sync_index=False routes through threading.Thread."""
    _stub_urlopen(monkeypatch, body=b"<p>x</p>")
    captured = []

    class _FakeThread:
        def __init__(self, target, name=None, daemon=None):
            captured.append({"target": target, "name": name})
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("threading.Thread", _FakeThread)
    with app.test_request_context():
        f = ingestion.ingest_url(
            user=user, workspace=workspace,
            url="https://example.com/", sync_index=False,
        )
    assert any(c["name"].startswith("index-") for c in captured)
    db.session.expire_all()
    assert File.query.get(f.id).indexed_at is not None
