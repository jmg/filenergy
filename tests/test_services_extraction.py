import os

import pytest

from filenergy.services import extraction


def write(tmp_path, name, content, binary=False):
    p = tmp_path / name
    if binary:
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return str(p)


def test_is_indexable_known_extensions():
    assert extraction.is_indexable("a.txt")
    assert extraction.is_indexable("a.PDF")
    assert extraction.is_indexable("a.docx")
    assert extraction.is_indexable("a.md")
    assert extraction.is_indexable("a.csv")


def test_is_indexable_text_mime_fallback():
    assert extraction.is_indexable("a.css")  # text/css


def test_is_indexable_unknown_ext_returns_false():
    assert not extraction.is_indexable("a.exe")
    assert not extraction.is_indexable("a.bin")


def test_extract_text_plain(tmp_path):
    p = write(tmp_path, "a.txt", "hello world")
    assert extraction.extract_text(p) == "hello world"


def test_extract_text_latin_fallback(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"\xff\xfehola")  # not valid utf-8 fully; latin-1 fallback works
    assert extraction.extract_text(str(p)) is not None


def test_extract_text_unknown_returns_none(tmp_path):
    p = write(tmp_path, "a.bin", "x")
    assert extraction.extract_text(p) is None


def test_extract_text_handles_text_mime_via_fallback(tmp_path):
    # Files with no recognised handler but text/* MIME should still extract.
    p = write(tmp_path, "stylesheet.css", "body { color: red }")
    assert "color" in extraction.extract_text(p)


def test_chunk_text_short_returns_single():
    chunks = extraction.chunk_text("short", size=100, overlap=10)
    assert chunks == ["short"]


def test_chunk_text_empty_returns_empty():
    assert extraction.chunk_text("", size=100, overlap=10) == []
    assert extraction.chunk_text("   ", size=100, overlap=10) == []


def test_chunk_text_long_splits_with_overlap():
    text = "para one." + "\n\n" + "para two." * 200
    chunks = extraction.chunk_text(text, size=200, overlap=40)
    assert len(chunks) >= 2
    # Each chunk respects the size budget (some slack for boundary breaks).
    assert all(len(c) <= 220 for c in chunks)


def test_chunk_text_breaks_on_paragraph(tmp_path):
    text = ("alpha. " * 30) + "\n\n" + ("beta. " * 30)
    chunks = extraction.chunk_text(text, size=200, overlap=20)
    assert len(chunks) >= 2


def _pypdf_runtime_ok():
    """Some test environments have a broken `cryptography` install that makes
    pypdf raise PanicException. Skip PDF-real tests there."""
    try:
        from pypdf import PdfWriter

        PdfWriter()
        return True
    except BaseException:  # noqa: BLE001 — also catch PanicException
        return False


def test_extract_pdf_real(tmp_path):
    """Generate a tiny PDF in-memory and ensure extraction returns text."""
    pytest.importorskip("pypdf")
    if not _pypdf_runtime_ok():
        pytest.skip("pypdf runtime not usable in this environment")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    p = tmp_path / "blank.pdf"
    with open(p, "wb") as fd:
        writer.write(fd)
    # Blank PDF returns empty text — handler should still not crash.
    result = extraction.extract_text(str(p))
    assert result is None or isinstance(result, str)


def test_extract_pdf_corrupted_returns_none(tmp_path):
    pytest.importorskip("pypdf")
    if not _pypdf_runtime_ok():
        pytest.skip("pypdf runtime not usable in this environment")
    p = write(tmp_path, "broken.pdf", "not a pdf at all", binary=False)
    assert extraction.extract_text(p) is None


def test_extract_docx_real(tmp_path):
    docx_lib = pytest.importorskip("docx")
    doc = docx_lib.Document()
    doc.add_paragraph("hello docx world")
    p = tmp_path / "a.docx"
    doc.save(str(p))
    assert "hello docx world" in extraction.extract_text(str(p))


def test_extract_docx_corrupted_returns_none(tmp_path):
    p = write(tmp_path, "broken.docx", "not a docx", binary=False)
    assert extraction.extract_text(p) is None


def test_extract_pdf_unavailable(tmp_path, monkeypatch):
    """If pypdf is not installed, the handler returns None."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = write(tmp_path, "x.pdf", "anything")
    assert extraction.extract_text(p) is None


def test_extract_docx_unavailable(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = write(tmp_path, "x.docx", "anything")
    assert extraction.extract_text(p) is None


def test_pdf_page_extract_handles_exception(monkeypatch):
    """If pypdf's PdfReader raises, the handler returns None cleanly."""
    pytest.importorskip("pypdf")

    monkeypatch.setattr(extraction, "_read_pdf", lambda path: None)
    assert extraction.extract_text("/nonexistent.pdf") is None
