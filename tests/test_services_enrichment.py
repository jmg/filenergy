"""Tests for the on-index enrichment (summary + suggested questions)."""
import json

from filenergy.models import File
from filenergy.services import enrichment


def _make_indexed_file(db, user, workspace):
    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="brief.txt", path="/x", url="hh",
        text_content="Apples are red. Bananas are yellow." * 10,
    )
    db.session.add(f)
    db.session.commit()
    return f


def test_enrich_returns_false_without_text(db, user, workspace, app):
    f = File(user_id=user.id, workspace_id=workspace.id, name="x", path="/x", url="u")
    db.session.add(f)
    db.session.commit()
    with app.test_request_context():
        assert enrichment.enrich_file(f) is False


def test_enrich_returns_false_when_unconfigured(db, user, workspace, app, monkeypatch):
    from filenergy.services import chat as chat_mod

    monkeypatch.setattr(chat_mod, "is_configured", lambda: False)
    f = _make_indexed_file(db, user, workspace)
    with app.test_request_context():
        assert enrichment.enrich_file(f) is False


def test_enrich_persists_summary_and_questions(
    db, user, workspace, app, _stub_external_services
):
    """The fake client returns a fixed final_text — set it to valid JSON."""
    _stub_external_services.final_text = json.dumps({
        "summary": "A short brief.",
        "questions": ["What's it about?", "Why apples?", "Why yellow?"],
    })
    f = _make_indexed_file(db, user, workspace)
    with app.test_request_context():
        ok = enrichment.enrich_file(f)
    assert ok is True
    assert f.summary == "A short brief."
    assert f.suggested_questions == ["What's it about?", "Why apples?", "Why yellow?"]


def test_enrich_caps_questions_at_three(db, user, workspace, app, _stub_external_services):
    _stub_external_services.final_text = json.dumps({
        "summary": "Hi.",
        "questions": ["q1", "q2", "q3", "q4", "q5"],
    })
    f = _make_indexed_file(db, user, workspace)
    with app.test_request_context():
        enrichment.enrich_file(f)
    assert len(f.suggested_questions) == 3


def test_enrich_drops_non_string_questions(db, user, workspace, app, _stub_external_services):
    _stub_external_services.final_text = json.dumps({
        "summary": "Hi.",
        "questions": "not an array",
    })
    f = _make_indexed_file(db, user, workspace)
    with app.test_request_context():
        ok = enrichment.enrich_file(f)
    assert ok is True
    assert f.suggested_questions == []


def test_enrich_handles_invalid_json(db, user, workspace, app, _stub_external_services):
    _stub_external_services.final_text = "not json at all"
    f = _make_indexed_file(db, user, workspace)
    with app.test_request_context():
        ok = enrichment.enrich_file(f)
    assert ok is False


def test_enrich_handles_empty_response(db, user, workspace, app, _stub_external_services):
    _stub_external_services.final_text = ""
    f = _make_indexed_file(db, user, workspace)
    with app.test_request_context():
        ok = enrichment.enrich_file(f)
    assert ok is False


def test_file_suggested_questions_property_invalid_json(db, user, workspace):
    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="x", path="/x", url="u",
        suggested_questions_json="not json",
    )
    assert f.suggested_questions == []


def test_file_suggested_questions_property_empty(db, user, workspace):
    f = File(user_id=user.id, workspace_id=workspace.id, name="x", path="/x", url="u")
    assert f.suggested_questions == []
