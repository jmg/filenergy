"""Tier-5 feature tests:

- Conversation rename endpoint.
- Message feedback (thumbs up / down) endpoint + idempotent flip.
- Chat SSE meta event with `message_id` after `done`.
- File-list bulk delete via JSON.
- Onboarding starters render in chat as deep-linkable suggestions.
- Base.html ships icon sprite + command palette trigger.
"""
from __future__ import annotations

import json

import pytest


# ---------- Conversation rename ------------------------------------------


def test_conversation_rename_updates_title(auth_client, db, user, workspace):
    from filenergy.models import Conversation

    conv = Conversation(
        user_id=user.id, workspace_id=workspace.id, title="Original",
    )
    db.session.add(conv)
    db.session.commit()

    r = auth_client.post(
        f"/ask/c/{conv.id}/rename",
        data=json.dumps({"title": "Renamed thread"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    db.session.refresh(conv)
    assert conv.title == "Renamed thread"


def test_conversation_rename_rejects_other_users(client, db, workspace, user):
    from filenergy.models import Conversation, User
    from filenergy.services import workspaces

    bob = User(email="bob@example.com", username="bob@example.com")
    bob.set_password("password")
    db.session.add(bob)
    db.session.commit()
    workspaces.ensure_default_for(bob)

    conv = Conversation(
        user_id=user.id, workspace_id=workspace.id, title="Alice's",
    )
    db.session.add(conv)
    db.session.commit()

    client.post("/user/login/", data={"email": bob.email, "password": "password"})
    r = client.post(
        f"/ask/c/{conv.id}/rename",
        data=json.dumps({"title": "stolen"}),
        content_type="application/json",
    )
    assert r.status_code == 404


def test_conversation_rename_requires_title(auth_client, db, user, workspace):
    from filenergy.models import Conversation

    conv = Conversation(user_id=user.id, workspace_id=workspace.id, title="x")
    db.session.add(conv); db.session.commit()
    r = auth_client.post(
        f"/ask/c/{conv.id}/rename",
        data=json.dumps({"title": "  "}),
        content_type="application/json",
    )
    assert r.status_code == 400


# ---------- Message feedback ---------------------------------------------


def _make_message(db, user, workspace, role="assistant", content="Hi"):
    from filenergy.models import Conversation, Message
    conv = Conversation(user_id=user.id, workspace_id=workspace.id, title="t")
    db.session.add(conv); db.session.commit()
    msg = Message(conversation_id=conv.id, role=role, content=content)
    db.session.add(msg); db.session.commit()
    return msg


def test_feedback_records_thumbs_up(auth_client, db, user, workspace):
    from filenergy.models import MessageFeedback

    msg = _make_message(db, user, workspace)
    r = auth_client.post(
        "/ask/feedback",
        data=json.dumps({"message_id": msg.id, "rating": "up"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    fb = MessageFeedback.query.filter_by(message_id=msg.id).first()
    assert fb.rating == "up"


def test_feedback_flips_existing_rating(auth_client, db, user, workspace):
    from filenergy.models import MessageFeedback

    msg = _make_message(db, user, workspace)
    auth_client.post(
        "/ask/feedback",
        data=json.dumps({"message_id": msg.id, "rating": "up"}),
        content_type="application/json",
    )
    r = auth_client.post(
        "/ask/feedback",
        data=json.dumps({"message_id": msg.id, "rating": "down"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    rows = MessageFeedback.query.filter_by(message_id=msg.id).all()
    # Idempotent: still one row, but flipped.
    assert len(rows) == 1
    assert rows[0].rating == "down"


def test_feedback_rejects_invalid_rating(auth_client, db, user, workspace):
    msg = _make_message(db, user, workspace)
    r = auth_client.post(
        "/ask/feedback",
        data=json.dumps({"message_id": msg.id, "rating": "meh"}),
        content_type="application/json",
    )
    assert r.status_code == 400


def test_feedback_rejects_unknown_message(auth_client):
    r = auth_client.post(
        "/ask/feedback",
        data=json.dumps({"message_id": 99999, "rating": "up"}),
        content_type="application/json",
    )
    assert r.status_code == 404


def test_feedback_rejects_other_users_message(client, db, user, workspace):
    from filenergy.models import User
    from filenergy.services import workspaces

    msg = _make_message(db, user, workspace)
    bob = User(email="bob@example.com", username="bob@example.com")
    bob.set_password("password"); db.session.add(bob); db.session.commit()
    workspaces.ensure_default_for(bob)

    client.post("/user/login/", data={"email": bob.email, "password": "password"})
    r = client.post(
        "/ask/feedback",
        data=json.dumps({"message_id": msg.id, "rating": "up"}),
        content_type="application/json",
    )
    assert r.status_code == 404


# ---------- Chat stream emits message_id meta -----------------------------


def test_ask_stream_emits_message_id_meta(auth_client, db, user, workspace):
    """The browser needs the saved message id on the assistant bubble so
    feedback / regenerate can target the correct row."""
    r = auth_client.post(
        "/ask/stream",
        data=json.dumps({"question": "hello world"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Two `meta` events: one with conversation_id at the start, one with
    # message_id at the end. Assert the message_id meta is present.
    assert '"message_id"' in body


# ---------- File list bulk delete ----------------------------------------


def test_bulk_delete_via_json(auth_client, db, user, workspace, uploaded_file):
    """Soft delete: row stays in the DB with `deleted_at` set so the
    user can Undo. A cron job hard-deletes after the grace window."""
    from filenergy.models import File

    payload = json.dumps({"ids": [uploaded_file.id]})
    r = auth_client.post(
        "/file/bulk_delete/",
        data=payload, content_type="application/json",
    )
    assert r.status_code == 200
    assert r.get_json()["deleted"] == 1
    # Row still exists but is soft-deleted.
    f = File.query.get(uploaded_file.id)
    assert f is not None
    assert f.deleted_at is not None


def test_bulk_delete_handles_empty_payload(auth_client):
    r = auth_client.post(
        "/file/bulk_delete/",
        data=json.dumps({"ids": []}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert r.get_json()["deleted"] == 0


# ---------- Onboarding starters render in chat ---------------------------


def test_chat_renders_starters_when_no_history(auth_client):
    r = auth_client.get("/ask/")
    assert r.status_code == 200
    body = r.data
    # Empty-state starter buttons appear.
    assert b"Try one of these starters" in body
    assert b"Summarize my docs" in body


# ---------- Base shell --------------------------------------------------


def test_base_renders_icon_sprite(auth_client):
    r = auth_client.get("/")
    assert r.status_code == 200
    # Sprite definitions cover the main icons.
    for icon_id in [b"i-upload", b"i-files", b"i-chat", b"i-search", b"i-x", b"i-cog"]:
        assert icon_id in r.data


def test_base_renders_command_palette_trigger(auth_client):
    """⌘K trigger button is present for authenticated users."""
    r = auth_client.get("/")
    assert b"cmdk-trigger" in r.data
    assert b"\xe2\x8c\x98K" in r.data  # ⌘K


def test_base_includes_mobile_nav_drawer(auth_client):
    """Hamburger toggle + drawer are present on every authenticated page."""
    r = auth_client.get("/")
    assert b"mobile-nav-toggle" in r.data
    assert b'id="mobile-nav"' in r.data


def test_anon_user_sees_no_app_nav(client):
    r = client.get("/")
    # Anonymous users see the marketing landing — no nav buttons rendered.
    assert b'id="mobile-nav-toggle"' not in r.data
    assert b'id="cmdk-trigger"' not in r.data


# ---------- Onboarding stepper renders ----------------------------------


def test_onboarding_renders_step_layout(auth_client):
    r = auth_client.get("/onboarding/")
    assert r.status_code == 200
    assert b"Welcome to Filenergy" in r.data
    assert b"Three quick steps" in r.data
    # Starter cards.
    assert b"List the key dates" in r.data


# ---------- Conversation list filter inputs (smoke) ---------------------


def test_chat_renders_conversation_filter_input(auth_client):
    r = auth_client.get("/ask/")
    assert b'id="conv-filter"' in r.data


def test_file_list_renders_filter_and_select_all(auth_client, uploaded_file):
    r = auth_client.get("/file/list/")
    assert r.status_code == 200
    assert b'id="file-filter"' in r.data
    assert b'id="select-all"' in r.data
    assert b'id="bulk-bar"' in r.data
