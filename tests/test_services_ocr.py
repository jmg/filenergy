"""Tests for the Claude-vision OCR fallback."""
import pytest

from filenergy.services import ocr


def test_is_configured_matches_chat(monkeypatch):
    from filenergy.services import chat as chat_module
    monkeypatch.setattr(chat_module, "is_configured", lambda: True)
    assert ocr.is_configured() is True
    monkeypatch.setattr(chat_module, "is_configured", lambda: False)
    assert ocr.is_configured() is False


def test_ocr_returns_none_when_unconfigured(tmp_path, monkeypatch):
    from filenergy.services import chat as chat_module
    monkeypatch.setattr(chat_module, "is_configured", lambda: False)
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    assert ocr.ocr_file(str(p)) is None


def test_ocr_returns_none_for_unsupported_type(tmp_path):
    p = tmp_path / "x.exe"
    p.write_bytes(b"binary")
    assert ocr.ocr_file(str(p)) is None


def test_ocr_returns_none_for_missing_file():
    assert ocr.ocr_file("/no/such/file.pdf") is None


def test_ocr_returns_none_for_empty_file(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"")
    assert ocr.ocr_file(str(p)) is None


def test_ocr_returns_none_for_too_large_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_MAX_BYTES", 10)
    p = tmp_path / "x.pdf"
    p.write_bytes(b"x" * 100)
    assert ocr.ocr_file(str(p)) is None


def test_ocr_pdf_routes_through_claude(tmp_path, _stub_external_services):
    """The fake Anthropic client gets called with a `document` content block."""
    _stub_external_services.final_text = "Apples are red. (transcribed)"
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake content\n")
    text = ocr.ocr_file(str(p))
    assert text == "Apples are red. (transcribed)"
    last_call = _stub_external_services.calls[-1]
    blocks = last_call["messages"][0]["content"]
    assert any(b["type"] == "document" for b in blocks)
    assert any(b["type"] == "text" for b in blocks)


def test_ocr_image_routes_as_image_block(tmp_path, _stub_external_services):
    _stub_external_services.final_text = "screenshot text"
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    text = ocr.ocr_file(str(p))
    assert text == "screenshot text"
    blocks = _stub_external_services.calls[-1]["messages"][0]["content"]
    assert any(b["type"] == "image" for b in blocks)


def test_ocr_returns_none_on_empty_response(tmp_path, _stub_external_services):
    _stub_external_services.final_text = ""
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 hi")
    assert ocr.ocr_file(str(p)) is None


def test_ocr_returns_none_on_exception(tmp_path, monkeypatch):
    """Any error in the Anthropic call swallows to None."""
    from filenergy.services import chat as chat_module

    class _BoomMessages:
        def stream(self, **kw):
            raise RuntimeError("anthropic down")

    class _BoomClient:
        messages = _BoomMessages()

    monkeypatch.setattr(chat_module, "_client", lambda: _BoomClient())
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 hi")
    assert ocr.ocr_file(str(p)) is None
