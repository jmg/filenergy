"""Tier 2: conversation share links, source paragraph viewer, API key
scopes, expanded /api/v1."""
import json

import pytest

from filenergy.models import (
    ApiKey, Conversation, ConversationShareLink, Chunk, File,
    MessageCitation, ShareLink, WebhookSubscription,
)


# ---- conversation share links ----


def test_share_conversation_endpoint(auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]
    r = auth_client.post(f"/ask/c/{cid}/share")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["token"]
    assert payload["url"].startswith("/sc/")


def test_share_conversation_with_ttl(auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]
    r = auth_client.post(f"/ask/c/{cid}/share", json={"ttl_hours": 24})
    payload = r.get_json()
    assert payload["expires_at"] is not None


def test_share_conversation_404_other_workspace(auth_client, uploaded_file, db):
    from filenergy.models import User
    from filenergy.services import workspaces

    auth_client.post("/ask/", json={"question": "?"})
    cid = Conversation.query.first().id
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    workspaces.ensure_default_for(other)
    auth_client.get("/user/logout/")
    auth_client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = auth_client.post(f"/ask/c/{cid}/share")
    assert r.status_code == 404


def test_conversation_share_landing_renders_publicly(client, auth_client, uploaded_file):
    auth_client.post("/ask/", json={"question": "?"})
    cid = Conversation.query.first().id
    r = auth_client.post(f"/ask/c/{cid}/share")
    token = r.get_json()["token"]
    auth_client.get("/user/logout/")
    landing = client.get(f"/sc/{token}")
    assert landing.status_code == 200
    assert b"You" in landing.data
    assert b"Assistant" in landing.data


def test_conversation_share_404_for_unknown_token(client):
    assert client.get("/sc/nope").status_code == 404


def test_revoked_conversation_share_404(auth_client, client, uploaded_file):
    auth_client.post("/ask/", json={"question": "?"})
    cid = Conversation.query.first().id
    r = auth_client.post(f"/ask/c/{cid}/share")
    token = r.get_json()["token"]
    link = ConversationShareLink.query.first()
    auth_client.post(f"/ask/c/{cid}/share/{link.id}/revoke")
    r = client.get(f"/sc/{token}")
    assert r.status_code == 404


def test_revoke_share_404_for_unknown_id(auth_client, uploaded_file):
    auth_client.post("/ask/", json={"question": "?"})
    cid = Conversation.query.first().id
    r = auth_client.post(f"/ask/c/{cid}/share/9999/revoke")
    assert r.status_code == 404


def test_share_landing_increments_view_count(client, auth_client, uploaded_file):
    auth_client.post("/ask/", json={"question": "?"})
    cid = Conversation.query.first().id
    r = auth_client.post(f"/ask/c/{cid}/share")
    token = r.get_json()["token"]
    client.get(f"/sc/{token}")
    client.get(f"/sc/{token}")
    link = ConversationShareLink.query.first()
    assert link.view_count == 2


# ---- chunk char-offsets + source viewer ----


def test_chunk_offsets_persisted(auth_client, db, user, workspace):
    """Indexing stores char_offset_start + char_offset_end on each chunk."""
    import io
    text = "First paragraph.\n\n" + ("alpha " * 300) + "\n\n" + ("bravo " * 300)
    auth_client.post(
        "/file/upload/",
        data={"files[]": (io.BytesIO(text.encode()), "long.txt")},
        content_type="multipart/form-data",
    )
    f = File.query.filter_by(workspace_id=workspace.id).one()
    chunks = Chunk.query.filter_by(file_id=f.id).order_by(Chunk.position).all()
    assert chunks
    for c in chunks:
        assert c.char_offset_start is not None
        assert c.char_offset_end is not None
        # Recovered span matches the chunk content (modulo strip).
        assert c.content in (f.text_content[c.char_offset_start:c.char_offset_end])


def test_chunk_context_endpoint(auth_client, db, user, workspace, _stub_external_services):
    import io
    auth_client.post(
        "/file/upload/",
        data={"files[]": (io.BytesIO(b"alpha bravo charlie delta"), "x.txt")},
        content_type="multipart/form-data",
    )
    chunk = Chunk.query.first()
    r = auth_client.get(f"/file/chunk/{chunk.id}/context")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["file_name"] == "x.txt"
    assert "cited" in payload
    assert "before" in payload and "after" in payload


def test_chunk_context_404_other_workspace(client, auth_client, db, uploaded_file):
    from filenergy.models import User
    from filenergy.services import workspaces

    chunk = Chunk.query.first()
    assert chunk is not None
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    workspaces.ensure_default_for(other)
    auth_client.get("/user/logout/")
    auth_client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = auth_client.get(f"/file/chunk/{chunk.id}/context")
    assert r.status_code == 404


def test_chunk_context_404_unknown(auth_client):
    assert auth_client.get("/file/chunk/9999/context").status_code == 404


def test_chunk_text_with_offsets_short_text():
    from filenergy.services.extraction import chunk_text_with_offsets
    out = chunk_text_with_offsets("short text", size=100, overlap=10)
    assert out == [("short text", 0, len("short text"))]


def test_chunk_text_with_offsets_empty_text():
    from filenergy.services.extraction import chunk_text_with_offsets
    assert chunk_text_with_offsets("", size=100, overlap=10) == []


def test_chunk_text_with_offsets_long_text_has_consistent_spans():
    from filenergy.services.extraction import chunk_text_with_offsets
    text = "para one. " + "para body. " * 200
    out = chunk_text_with_offsets(text, size=200, overlap=20)
    assert len(out) >= 2
    # Each (start, end) recovered span should equal the chunk after strip.
    for content, start, end in out:
        assert text[start:end].strip() == content


# ---- per-API-key scopes ----


def test_full_access_key_can_use_any_endpoint(client, workspace, user, app):
    from filenergy.services import api_keys
    with app.test_request_context():
        _, token = api_keys.mint(workspace, user, "full")
    r = client.get("/api/v1/files", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_scoped_key_blocks_disallowed_endpoint(client, workspace, user, app):
    from filenergy.services import api_keys
    with app.test_request_context():
        _, token = api_keys.mint(
            workspace, user, "readonly", scopes=["files:read"],
        )
    # files:read should let us list files...
    r = client.get("/api/v1/files", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    # ...but ask:write is required for /ask.
    r = client.post(
        "/api/v1/ask",
        json={"question": "?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_files_write_implies_files_read(client, workspace, user, app, uploaded_file):
    from filenergy.services import api_keys
    with app.test_request_context():
        _, token = api_keys.mint(
            workspace, user, "writer", scopes=["files:write"],
        )
    r = client.get("/api/v1/files", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_api_key_scope_normalisation(workspace, user, app):
    from filenergy.services import api_keys
    with app.test_request_context():
        record, _ = api_keys.mint(
            workspace, user, "k",
            scopes=["FILES:READ", "garbage", "ask:write", "files:read"],
        )
    # Lowercased + dedup'd + filtered to known scopes.
    assert sorted(record.scopes) == sorted(["files:read", "ask:write"])


def test_scope_error_response_shape(client, workspace, user, app):
    from filenergy.services import api_keys
    with app.test_request_context():
        _, token = api_keys.mint(workspace, user, "k", scopes=["files:read"])
    r = client.post(
        "/api/v1/ask",
        json={"question": "?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    assert "Missing scope" in r.get_json()["error"]


# ---- expanded /api/v1 ----


def _full_token(workspace, user, app):
    from filenergy.services import api_keys
    with app.test_request_context():
        _, token = api_keys.mint(workspace, user, "test")
    return token


def test_api_get_file_by_id(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    r = client.get(
        f"/api/v1/files/{uploaded_file.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.get_json()["name"] == uploaded_file.name


def test_api_get_file_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.get(
        "/api/v1/files/99999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_delete_file(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    r = client.delete(
        f"/api/v1/files/{uploaded_file.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert File.query.count() == 0


def test_api_delete_file_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.delete(
        "/api/v1/files/99999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_list_collections(client, workspace, user, app):
    from filenergy.services import collections as coll_service
    with app.test_request_context():
        coll_service.create(workspace, "Engineering")
    token = _full_token(workspace, user, app)
    r = client.get("/api/v1/collections", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.get_json()["collections"][0]["name"] == "Engineering"


def test_api_create_collection(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/collections",
        json={"name": "New", "description": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.get_json()["name"] == "New"


def test_api_create_collection_missing_name(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/collections",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


def test_api_delete_collection(client, workspace, user, app):
    from filenergy.services import collections as coll_service
    with app.test_request_context():
        coll = coll_service.create(workspace, "trash")
    token = _full_token(workspace, user, app)
    r = client.delete(
        f"/api/v1/collections/{coll.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_api_delete_collection_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.delete(
        "/api/v1/collections/9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_assign_file_to_collection(client, workspace, user, app, uploaded_file):
    from filenergy.services import collections as coll_service
    with app.test_request_context():
        coll = coll_service.create(workspace, "Eng")
    token = _full_token(workspace, user, app)
    r = client.put(
        f"/api/v1/collections/{coll.id}/files/{uploaded_file.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    from filenergy import db
    db.session.refresh(uploaded_file)
    assert uploaded_file.collection_id == coll.id


def test_api_assign_file_404_collection(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    r = client.put(
        f"/api/v1/collections/9999/files/{uploaded_file.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_assign_file_404_file(client, workspace, user, app):
    from filenergy.services import collections as coll_service
    with app.test_request_context():
        coll = coll_service.create(workspace, "x")
    token = _full_token(workspace, user, app)
    r = client.put(
        f"/api/v1/collections/{coll.id}/files/9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_list_conversations(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    client.post(
        "/api/v1/ask",
        json={"question": "?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert len(r.get_json()["conversations"]) >= 1


def test_api_get_conversation_with_messages(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    client.post(
        "/api/v1/ask",
        json={"question": "What about apples?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    cid = Conversation.query.first().id
    r = client.get(
        f"/api/v1/conversations/{cid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["id"] == cid
    assert len(payload["messages"]) == 2


def test_api_get_conversation_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.get(
        "/api/v1/conversations/9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_delete_conversation(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    client.post(
        "/api/v1/ask", json={"question": "?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    cid = Conversation.query.first().id
    r = client.delete(
        f"/api/v1/conversations/{cid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert Conversation.query.count() == 0


def test_api_delete_conversation_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.delete(
        "/api/v1/conversations/9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_create_share_link(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    r = client.post(
        f"/api/v1/files/{uploaded_file.id}/share-links",
        json={"ttl_hours": 24, "max_downloads": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.get_json()["token"]


def test_api_create_share_link_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/files/9999/share-links",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_revoke_share_link(client, workspace, user, app, uploaded_file):
    from filenergy.services import share_links
    token = _full_token(workspace, user, app)
    with app.test_request_context():
        link = share_links.create(uploaded_file, created_by=user)
    r = client.delete(
        f"/api/v1/share-links/{link.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_api_revoke_share_link_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.delete(
        "/api/v1/share-links/9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_list_webhooks(client, workspace, user, app):
    from filenergy.services import webhooks
    with app.test_request_context():
        webhooks.create(workspace, "https://x.test/", ["file.uploaded"])
    token = _full_token(workspace, user, app)
    r = client.get("/api/v1/webhooks", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert len(r.get_json()["webhooks"]) == 1


def test_api_create_webhook(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/webhooks",
        json={"url": "https://x.test/", "events": ["file.uploaded"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert "secret" in r.get_json()


def test_api_create_webhook_missing_fields(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/webhooks",
        json={"url": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


def test_api_delete_webhook(client, workspace, user, app):
    from filenergy.services import webhooks
    with app.test_request_context():
        sub, _ = webhooks.create(workspace, "https://x.test/", ["file.uploaded"])
    token = _full_token(workspace, user, app)
    r = client.delete(
        f"/api/v1/webhooks/{sub.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_api_delete_webhook_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.delete(
        "/api/v1/webhooks/9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_api_list_members(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.get("/api/v1/members", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    members = r.get_json()["members"]
    assert any(m["email"] == user.email for m in members)


def test_api_create_invitation(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/invitations",
        json={"email": "joiner@x", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.get_json()["token"]


def test_api_create_invitation_missing_email(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/invitations",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


def test_api_create_conversation_share(client, workspace, user, app, uploaded_file):
    token = _full_token(workspace, user, app)
    client.post(
        "/api/v1/ask", json={"question": "?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    cid = Conversation.query.first().id
    r = client.post(
        f"/api/v1/conversations/{cid}/share-links",
        json={"ttl_hours": 12},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["url"].startswith("/sc/")


def test_api_create_conversation_share_404(client, workspace, user, app):
    token = _full_token(workspace, user, app)
    r = client.post(
        "/api/v1/conversations/9999/share-links",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
