import json

import pytest

from filenergy import settings
from filenergy.models import Chunk, File, Message
from filenergy.services import chat, embeddings


def _make_chunk(db, user, content="hello"):
    f = File(user_id=user.id, name="note.txt", path="/tmp/x", url="hh")
    db.session.add(f)
    db.session.commit()
    c = Chunk(
        file_id=f.id,
        position=0,
        content=content,
        embedding=json.dumps([1.0, 0.0, 0.0]),
    )
    db.session.add(c)
    db.session.commit()
    return f, c


def test_answer_question_no_results_returns_message(db, user, monkeypatch):
    monkeypatch.setattr(embeddings, "search", lambda u, q, k: [])
    answer = chat.answer_question(user, "Where?")
    assert "No matching content" in answer.text
    assert answer.sources == []


def test_answer_question_with_results(db, user, _stub_external_services):
    _make_chunk(db, user, content="Apples are red.")
    answer = chat.answer_question(user, "What about apples?")
    assert "Apples are red" in answer.text
    assert len(answer.sources) == 1
    assert answer.sources[0].name == "note.txt"


def test_answer_question_includes_history(db, user, _stub_external_services):
    _make_chunk(db, user)
    history = [
        Message(role="user", content="prior question"),
        Message(role="assistant", content="prior answer"),
    ]
    chat.answer_question(user, "follow up", history=history)
    sent_messages = _stub_external_services.calls[-1]["messages"]
    # Two history turns + the new turn.
    assert len(sent_messages) == 3
    assert sent_messages[0]["role"] == "user"
    assert sent_messages[1]["role"] == "assistant"
    assert sent_messages[2]["role"] == "user"
    assert "<question>follow up</question>" in sent_messages[2]["content"]


def test_answer_question_uses_prompt_caching(db, user, _stub_external_services):
    _make_chunk(db, user)
    chat.answer_question(user, "q")
    kw = _stub_external_services.calls[-1]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["model"] == settings.CLAUDE_MODEL


def test_stream_answer_emits_token_and_done_events(db, user, _stub_external_services):
    _make_chunk(db, user)
    events = list(chat.stream_answer(user, "?"))
    assert any(e.startswith("event: token") for e in events)
    assert events[-1].startswith("event: done")


def test_stream_answer_no_results(db, user, monkeypatch):
    monkeypatch.setattr(embeddings, "search", lambda u, q, k: [])
    events = list(chat.stream_answer(user, "?"))
    assert events[0].startswith("event: token")
    assert "No matching content" in events[0]
    assert events[-1].startswith("event: done")


def test_stream_answer_handles_search_error(db, user, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("voyage down")

    monkeypatch.setattr(embeddings, "search", boom)
    events = list(chat.stream_answer(user, "?"))
    assert events[0].startswith("event: error")


def test_stream_answer_handles_stream_error(db, user, _stub_external_services):
    _make_chunk(db, user)

    class _BoomStream:
        @property
        def text_stream(self):
            raise RuntimeError("anthropic down")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _BoomMessages:
        def stream(self, **kw):
            return _BoomStream()

    class _BoomClient:
        messages = _BoomMessages()

    import filenergy.services.chat as chat_mod
    chat_mod._client = lambda: _BoomClient()  # type: ignore[assignment]

    events = list(chat.stream_answer(user, "?"))
    assert any(e.startswith("event: error") for e in events)


def test_chat_unavailable_without_api_key(monkeypatch, real_chat_client):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    with pytest.raises(chat.ChatUnavailable):
        real_chat_client()


def test_chat_unavailable_without_anthropic_package(monkeypatch, real_chat_client):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(chat.ChatUnavailable):
        real_chat_client()


def test_is_configured_requires_both_keys(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    assert not chat.is_configured()
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(settings, "VOYAGE_API_KEY", "")
    assert not chat.is_configured()


def test_answer_question_falls_back_when_text_empty(db, user, _stub_external_services):
    _make_chunk(db, user)
    _stub_external_services.final_text = ""
    answer = chat.answer_question(user, "x")
    assert answer.text == "(no answer)"


def test_build_context_dedupes_sources(db, user, _stub_external_services):
    """Two chunks from the same file collapse to one Source row."""
    f = File(user_id=user.id, name="multi.txt", path="/tmp/m", url="m1")
    db.session.add(f)
    db.session.commit()
    db.session.add_all([
        Chunk(file_id=f.id, position=0, content="A", embedding=json.dumps([1, 0, 0])),
        Chunk(file_id=f.id, position=1, content="B", embedding=json.dumps([0.9, 0.1, 0])),
    ])
    db.session.commit()
    answer = chat.answer_question(user, "?")
    assert len(answer.sources) == 1
