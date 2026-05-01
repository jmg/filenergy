"""Tier-6 feature tests:

- Universal /search endpoint powering the cmdk command palette.
- Evals dashboard at /dashboard/evals (totals, ratio, time series, triage).
- Vision: chat.stream_answer + /ask/stream accept image content blocks.
"""
from __future__ import annotations

import base64
import json

import pytest


# ---------- Universal search ---------------------------------------------


def test_search_returns_nav_only_for_empty_query(auth_client):
    r = auth_client.get("/search?q=")
    assert r.status_code == 200
    body = r.get_json()
    assert "results" in body
    # Empty query → all nav commands.
    labels = [r["label"] for r in body["results"]]
    assert "Go to Ask" in labels
    assert "Audit log" in labels


def test_search_finds_files_by_name(auth_client, db, user, workspace):
    from filenergy.models import File

    f = File(
        name="Acme MSA — 2026.pdf",
        path="/tmp/x", url="abc", size_bytes=1024,
        user_id=user.id, workspace_id=workspace.id,
    )
    db.session.add(f); db.session.commit()

    r = auth_client.get("/search?q=acme")
    body = r.get_json()
    file_results = [x for x in body["results"] if x["kind"] == "file"]
    assert any("Acme MSA" in x["label"] for x in file_results)
    # File hit links to the detail page.
    assert any(x["href"].startswith("/file/") for x in file_results)


def test_search_finds_conversations_by_title(auth_client, db, user, workspace):
    from filenergy.models import Conversation

    c = Conversation(
        user_id=user.id, workspace_id=workspace.id,
        title="Q4 contract review",
    )
    db.session.add(c); db.session.commit()

    r = auth_client.get("/search?q=contract")
    body = r.get_json()
    conv_results = [x for x in body["results"] if x["kind"] == "conversation"]
    assert any("Q4 contract" in x["label"] for x in conv_results)


def test_search_finds_collections_by_name(auth_client, db, user, workspace):
    from filenergy.models import Collection

    c = Collection(
        workspace_id=workspace.id, name="Customer interviews", slug="customer-interviews",
    )
    db.session.add(c); db.session.commit()

    r = auth_client.get("/search?q=customer")
    body = r.get_json()
    coll_results = [x for x in body["results"] if x["kind"] == "collection"]
    assert any("Customer" in x["label"] for x in coll_results)


def test_search_filters_nav_by_query(auth_client):
    r = auth_client.get("/search?q=audit")
    body = r.get_json()
    labels = [x["label"] for x in body["results"]]
    assert "Audit log" in labels
    # Filter actually narrowed — most nav items are gone.
    assert len(labels) <= 5


def test_search_requires_login(client):
    r = client.get("/search?q=anything")
    assert r.status_code in (302, 401)


# ---------- Evals dashboard ---------------------------------------------


def _make_feedback(db, user, workspace, rating="up"):
    from filenergy.models import Conversation, Message, MessageFeedback

    conv = Conversation(user_id=user.id, workspace_id=workspace.id, title="t")
    db.session.add(conv); db.session.commit()
    msg = Message(conversation_id=conv.id, role="assistant", content="answer body")
    db.session.add(msg); db.session.commit()
    fb = MessageFeedback(message_id=msg.id, user_id=user.id, rating=rating)
    db.session.add(fb); db.session.commit()
    return msg, fb


def test_evals_dashboard_renders_empty_state(auth_client):
    r = auth_client.get("/dashboard/evals")
    assert r.status_code == 200
    assert b"No feedback yet" in r.data


def test_evals_dashboard_renders_stats(auth_client, db, user, workspace):
    _make_feedback(db, user, workspace, "up")
    _make_feedback(db, user, workspace, "up")
    _make_feedback(db, user, workspace, "down")
    r = auth_client.get("/dashboard/evals")
    assert r.status_code == 200
    # 67% satisfaction.
    assert b"67%" in r.data
    assert b"Thumbs up" in r.data
    assert b"Thumbs down" in r.data


def test_evals_dashboard_lists_recent_lows(auth_client, db, user, workspace):
    _make_feedback(db, user, workspace, "down")
    r = auth_client.get("/dashboard/evals")
    assert r.status_code == 200
    assert b"Recent low-rated answers" in r.data
    # The conversation title surfaces in the triage list.
    assert b"answer body" in r.data or b"Untitled" in r.data


def test_evals_dashboard_forbidden_for_member(client, db, user, workspace):
    """Plain members can't see eval data — owner/admin only."""
    from filenergy.models import User, WorkspaceMember

    bob = User(email="bob@example.com", username="bob@example.com")
    bob.set_password("password"); db.session.add(bob); db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=bob.id, role="member",
    ))
    db.session.commit()
    client.post("/user/login/", data={"email": bob.email, "password": "password"})
    client.post(f"/w/switch/{workspace.id}")
    r = client.get("/dashboard/evals")
    assert r.status_code == 403


# ---------- Vision in chat ----------------------------------------------


def _tiny_png_b64() -> str:
    # 1x1 transparent PNG.
    return ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkAAIA"
            "AAUAAen/PWcAAAAASUVORK5CYII=")


def test_build_messages_attaches_image_blocks_when_provided():
    from filenergy.services import chat

    images = [{"media_type": "image/png", "data": _tiny_png_b64()}]
    msgs = chat._build_messages(
        conversation_messages=[], context="ctx",
        question="What's in this image?", images=images,
    )
    assert len(msgs) == 1
    new_turn = msgs[0]
    assert new_turn["role"] == "user"
    # When images are attached the content becomes a list of blocks.
    assert isinstance(new_turn["content"], list)
    block_types = [b.get("type") for b in new_turn["content"]]
    assert "image" in block_types
    assert "text" in block_types


def test_build_messages_uses_string_content_without_images():
    """Backwards compat: no images → original string-shape content."""
    from filenergy.services import chat

    msgs = chat._build_messages(
        conversation_messages=[], context="ctx", question="Hello",
    )
    assert isinstance(msgs[0]["content"], str)


def test_ask_stream_accepts_images_payload(auth_client):
    """The endpoint round-trips an images list to chat.stream_answer."""
    body = {
        "question": "Compare to my contracts",
        "images": [{"media_type": "image/png", "data": _tiny_png_b64()}],
    }
    r = auth_client.post(
        "/ask/stream", data=json.dumps(body), content_type="application/json",
    )
    assert r.status_code == 200
    # Stream finalises with the standard meta {message_id} event.
    text = r.get_data(as_text=True)
    assert '"message_id"' in text or '"conversation_id"' in text


def test_ask_stream_filters_invalid_image_entries(auth_client, monkeypatch):
    """Non-image MIME types and oversized data get dropped server-side."""
    from filenergy.services import chat

    captured = {}
    real_stream = chat.stream_answer

    def stub_stream(*args, **kwargs):
        captured["images"] = kwargs.get("images")
        # Yield a synthetic done event so the view can finalise.
        yield 'event: token\ndata: {"text":"ok"}\n\n'
        yield 'event: done\ndata: {"text":"ok","sources":[],"chunk_citations":[]}\n\n'

    monkeypatch.setattr(chat, "stream_answer", stub_stream)

    body = {
        "question": "Look",
        "images": [
            {"media_type": "text/plain", "data": "x"},      # not an image
            "not-a-dict",                                    # not a dict
            {"media_type": "image/png"},                     # missing data
            {"media_type": "image/png", "data": _tiny_png_b64()},  # valid
        ],
    }
    r = auth_client.post(
        "/ask/stream", data=json.dumps(body), content_type="application/json",
    )
    assert r.status_code == 200
    # Consume the streamed body so the generator actually runs the call.
    r.get_data(as_text=True)
    assert captured.get("images") is not None
    # Only the valid entry survives.
    assert len(captured["images"]) == 1
    assert captured["images"][0]["media_type"] == "image/png"


def test_ask_stream_caps_image_count(auth_client, monkeypatch):
    """No more than 5 images reach the stream layer per turn."""
    from filenergy.services import chat

    captured = {}

    def stub_stream(*args, **kwargs):
        captured["count"] = len(kwargs.get("images") or [])
        yield 'event: done\ndata: {"text":"ok","sources":[],"chunk_citations":[]}\n\n'

    monkeypatch.setattr(chat, "stream_answer", stub_stream)
    body = {
        "question": "Look",
        "images": [{"media_type": "image/png", "data": _tiny_png_b64()}] * 8,
    }
    r = auth_client.post(
        "/ask/stream", data=json.dumps(body), content_type="application/json",
    )
    assert r.status_code == 200
    r.get_data(as_text=True)
    assert captured.get("count") == 5


# ---------- README assets exist -----------------------------------------


def test_readme_screenshots_present():
    """Make sure the SVG mockups referenced by README live in the repo."""
    import os
    base = os.path.join(os.path.dirname(__file__), "..", "docs", "screenshots")
    for name in ("chat.svg", "files.svg", "evals.svg"):
        assert os.path.isfile(os.path.join(base, name)), f"missing {name}"
