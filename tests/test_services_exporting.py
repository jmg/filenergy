"""Tests for PDF / DOCX / Markdown conversation export."""
import json

import pytest

from filenergy.models import Conversation, Message
from filenergy.services import exporting


def _make_conv(db, user, workspace, with_sources=True):
    c = Conversation(user_id=user.id, workspace_id=workspace.id, title="Q4 review")
    db.session.add(c)
    db.session.commit()
    db.session.add(Message(
        conversation_id=c.id, role="user", content="What's our Q4 plan?",
    ))
    db.session.add(Message(
        conversation_id=c.id, role="assistant", content="Ship things.",
        sources_json=json.dumps([
            {"file_id": 1, "name": "plan.md", "url": "h", "score": 0.9}
        ]) if with_sources else None,
    ))
    db.session.commit()
    return c


def test_to_markdown(db, user, workspace, app):
    with app.test_request_context():
        c = _make_conv(db, user, workspace)
        md = exporting.to_markdown(c)
    assert "Q4 review" in md
    assert "**You**" in md
    assert "**Assistant**" in md
    assert "plan.md" in md


def test_to_markdown_skips_sources_when_empty(db, user, workspace, app):
    with app.test_request_context():
        c = _make_conv(db, user, workspace, with_sources=False)
        md = exporting.to_markdown(c)
    assert "Sources:" not in md


def test_to_markdown_handles_invalid_sources_json(db, user, workspace, app):
    with app.test_request_context():
        c = Conversation(user_id=user.id, workspace_id=workspace.id, title="x")
        db.session.add(c)
        db.session.commit()
        db.session.add(Message(
            conversation_id=c.id, role="assistant", content="a",
            sources_json="not json",
        ))
        db.session.commit()
        md = exporting.to_markdown(c)
    assert "Sources:" not in md


def test_to_pdf_returns_bytes(db, user, workspace, app):
    with app.test_request_context():
        c = _make_conv(db, user, workspace)
        body = exporting.to_pdf(c)
    assert isinstance(body, (bytes, bytearray))
    assert body.startswith(b"%PDF")


def test_to_pdf_handles_unicode_safely(db, user, workspace, app):
    with app.test_request_context():
        c = Conversation(user_id=user.id, workspace_id=workspace.id, title="日本語")
        db.session.add(c)
        db.session.commit()
        db.session.add(Message(
            conversation_id=c.id, role="user", content="¿Qué tal? — émojis 🎉",
        ))
        db.session.commit()
        # Latin-1 fallback strips characters but doesn't crash.
        body = exporting.to_pdf(c)
    assert body.startswith(b"%PDF")


def test_to_pdf_unavailable_without_fpdf(monkeypatch, db, user, workspace, app):
    import builtins
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "fpdf":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    with app.test_request_context():
        c = _make_conv(db, user, workspace)
        with pytest.raises(exporting.ExportUnavailable):
            exporting.to_pdf(c)


def test_to_docx_returns_zip_bytes(db, user, workspace, app):
    with app.test_request_context():
        c = _make_conv(db, user, workspace)
        body = exporting.to_docx(c)
    # DOCX is a ZIP; magic bytes start with PK.
    assert body[:2] == b"PK"


def test_to_docx_unavailable_without_docx(monkeypatch, db, user, workspace, app):
    import builtins
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    with app.test_request_context():
        c = _make_conv(db, user, workspace)
        with pytest.raises(exporting.ExportUnavailable):
            exporting.to_docx(c)
