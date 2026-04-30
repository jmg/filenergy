"""Tests for chunk-level provenance: MessageCitation rows + dashboard surface."""
import json

import pytest

from filenergy.models import (
    Chunk,
    Conversation,
    File,
    Message,
    MessageCitation,
)
from filenergy.services import conversations


def _make_chunk(db, user, workspace, content="apples"):
    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="note.txt", path="/x", url="hh",
    )
    db.session.add(f)
    db.session.commit()
    c = Chunk(
        file_id=f.id, position=0, content=content,
        embedding=json.dumps([1.0, 0.0, 0.0]),
    )
    db.session.add(c)
    db.session.commit()
    return c


def test_add_assistant_message_persists_citations(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        chunk = _make_chunk(db, user, workspace)
        msg = conversations.add_assistant_message(
            c, "Apples are red.", [],
            chunk_citations=[(chunk.id, 0.92)],
        )
    rows = MessageCitation.query.filter_by(message_id=msg.id).all()
    assert len(rows) == 1
    assert rows[0].chunk_id == chunk.id
    assert abs(rows[0].score - 0.92) < 1e-6


def test_add_assistant_message_skips_empty_citations(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        msg = conversations.add_assistant_message(c, "x", [], chunk_citations=[])
    assert MessageCitation.query.count() == 0


def test_add_assistant_message_skips_malformed_entries(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        chunk = _make_chunk(db, user, workspace)
        msg = conversations.add_assistant_message(
            c, "x", [],
            # Mix of valid, malformed, and None — only the valid one survives.
            chunk_citations=[
                (chunk.id, 0.9),
                "not-a-tuple",
                (None, 0.5),
                (chunk.id, "not-a-float"),  # ValueError on float() — skipped
            ],
        )
    rows = MessageCitation.query.filter_by(message_id=msg.id).all()
    assert len(rows) == 1
    assert rows[0].chunk_id == chunk.id


def test_chat_answer_question_returns_chunk_citations(
    db, user, workspace, _stub_external_services
):
    from filenergy.services import chat

    chunk = _make_chunk(db, user, workspace)
    answer = chat.answer_question(workspace, "?")
    assert answer.chunk_citations
    chunk_ids = {c[0] for c in answer.chunk_citations}
    assert chunk.id in chunk_ids


def test_chat_no_results_returns_empty_citations(db, workspace, monkeypatch):
    from filenergy.services import chat, embeddings

    monkeypatch.setattr(embeddings, "search", lambda w, q, k, **kw: [])
    answer = chat.answer_question(workspace, "?")
    assert answer.chunk_citations == []


def test_stream_answer_done_event_includes_chunk_citations(
    db, user, workspace, _stub_external_services
):
    from filenergy.services import chat

    _make_chunk(db, user, workspace)
    events = list(chat.stream_answer(workspace, "?"))
    done = [e for e in events if e.startswith("event: done")]
    assert done
    payload = json.loads(done[-1].split("data: ", 1)[1])
    assert "chunk_citations" in payload


def test_ask_view_persists_citations_e2e(auth_client, db, user, workspace,
                                           _stub_external_services):
    """Full path: POST /ask/ → MessageCitation rows show up."""
    chunk = _make_chunk(db, user, workspace)
    r = auth_client.post("/ask/", json={"question": "?"})
    assert r.status_code == 200
    assert MessageCitation.query.count() >= 1
    assert MessageCitation.query.first().chunk_id == chunk.id


def test_ask_stream_view_persists_citations(auth_client, db, user, workspace,
                                              _stub_external_services):
    chunk = _make_chunk(db, user, workspace)
    r = auth_client.post("/ask/stream", json={"question": "?"})
    assert r.status_code == 200
    # SSE consumer drains the body — done by the test client implicitly.
    r.get_data()
    assert MessageCitation.query.count() >= 1


def test_dashboard_surfaces_top_chunks(auth_client, db, user, workspace,
                                         _stub_external_services):
    chunk = _make_chunk(db, user, workspace, content="alpha bravo charlie delta")
    auth_client.post("/ask/", json={"question": "?"})
    r = auth_client.get("/dashboard/")
    assert r.status_code == 200
    assert b"Most-cited chunks" in r.data
    assert b"alpha bravo" in r.data


def test_dashboard_handles_no_citations(auth_client):
    """Dashboard renders even when there are zero citations yet."""
    r = auth_client.get("/dashboard/")
    assert r.status_code == 200


def test_chunk_deletion_cascades_citations(db, user, workspace, app):
    with app.test_request_context():
        chunk = _make_chunk(db, user, workspace)
        c = conversations.get_or_create(user, workspace, None)
        msg = conversations.add_assistant_message(
            c, "x", [], chunk_citations=[(chunk.id, 0.9)]
        )
        assert MessageCitation.query.count() == 1
        db.session.delete(chunk)
        db.session.commit()
    assert MessageCitation.query.count() == 0


def test_message_deletion_cascades_citations(db, user, workspace, app):
    with app.test_request_context():
        chunk = _make_chunk(db, user, workspace)
        c = conversations.get_or_create(user, workspace, None)
        msg = conversations.add_assistant_message(
            c, "x", [], chunk_citations=[(chunk.id, 0.9)]
        )
        db.session.delete(msg)
        db.session.commit()
    assert MessageCitation.query.count() == 0
