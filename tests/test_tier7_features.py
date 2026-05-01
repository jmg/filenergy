"""Tier-7 feature tests:

- File soft-delete + undo (bulk_delete sets deleted_at, bulk_restore clears it).
- File rename endpoint.
- Conversation pin / archive toggles + sidebar ordering / filtering.
- Collection share link (mint + public read-only landing).
- Reranker: claude backend reorders, noop is passthrough, fallback on error.
- Email-to-ingest: address derivation, payload routing, attachment ingest.
- SCIM: auth headers, list/get/create/replace/patch/delete users.
- Landing page renders the new hero + pricing.
"""
from __future__ import annotations

import base64
import json
import os

import pytest


# ---------- File soft-delete + undo --------------------------------------


def test_bulk_restore_clears_deleted_at(auth_client, db, user, workspace, uploaded_file):
    """After a soft-delete, /file/bulk_restore/ clears `deleted_at`."""
    from filenergy.models import File

    auth_client.post(
        "/file/bulk_delete/",
        data=json.dumps({"ids": [uploaded_file.id]}),
        content_type="application/json",
    )
    f = File.query.get(uploaded_file.id)
    assert f.deleted_at is not None

    r = auth_client.post(
        "/file/bulk_restore/",
        data=json.dumps({"ids": [uploaded_file.id]}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert r.get_json()["restored"] == 1
    f = File.query.get(uploaded_file.id)
    assert f.deleted_at is None


def test_soft_deleted_files_hidden_from_listing(auth_client, db, user, workspace, uploaded_file):
    auth_client.post(
        "/file/bulk_delete/",
        data=json.dumps({"ids": [uploaded_file.id]}),
        content_type="application/json",
    )
    r = auth_client.get("/file/list/")
    assert r.status_code == 200
    assert uploaded_file.name.encode() not in r.data


def test_purge_cli_hard_deletes_after_grace(monkeypatch, app, db, user, workspace, uploaded_file):
    """The cron-style `flask purge-deleted-files` command hard-deletes
    rows whose `deleted_at` is older than the grace window."""
    from datetime import timedelta
    from filenergy.models import File, utcnow

    # Soft-delete the file.
    auth_client_post = uploaded_file.id
    f = File.query.get(uploaded_file.id)
    f.deleted_at = utcnow() - timedelta(hours=48)
    db.session.commit()

    monkeypatch.setenv("FILENERGY_DELETE_GRACE_HOURS", "24")
    runner = app.test_cli_runner()
    result = runner.invoke(args=["purge-deleted-files"])
    assert result.exit_code == 0
    assert "purged" in result.output
    assert File.query.get(uploaded_file.id) is None


def test_purge_cli_keeps_within_grace(monkeypatch, app, db, user, workspace, uploaded_file):
    from filenergy.models import File, utcnow

    # Soft-deleted just now → should NOT be purged.
    f = File.query.get(uploaded_file.id)
    f.deleted_at = utcnow()
    db.session.commit()

    monkeypatch.setenv("FILENERGY_DELETE_GRACE_HOURS", "24")
    runner = app.test_cli_runner()
    runner.invoke(args=["purge-deleted-files"])
    assert File.query.get(uploaded_file.id) is not None


# ---------- File rename --------------------------------------------------


def test_file_rename_endpoint_updates_name(auth_client, db, uploaded_file):
    r = auth_client.post(
        f"/file/{uploaded_file.id}/rename",
        data=json.dumps({"name": "renamed.pdf"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    db.session.refresh(uploaded_file)
    assert uploaded_file.name == "renamed.pdf"


def test_file_rename_rejects_empty_name(auth_client, uploaded_file):
    r = auth_client.post(
        f"/file/{uploaded_file.id}/rename",
        data=json.dumps({"name": ""}),
        content_type="application/json",
    )
    assert r.status_code == 400


def test_file_rename_rejects_other_workspaces(client, db, user, workspace, uploaded_file):
    """A user from a different workspace can't rename your files."""
    from filenergy.models import User
    from filenergy.services import workspaces

    bob = User(email="bob@example.com", username="bob@example.com")
    bob.set_password("password"); db.session.add(bob); db.session.commit()
    workspaces.ensure_default_for(bob)

    client.post("/user/login/", data={"email": bob.email, "password": "password"})
    r = client.post(
        f"/file/{uploaded_file.id}/rename",
        data=json.dumps({"name": "evil.pdf"}),
        content_type="application/json",
    )
    assert r.status_code == 404


# ---------- Conversation pin / archive ----------------------------------


def _make_conv(db, user, workspace, title="t"):
    from filenergy.models import Conversation
    c = Conversation(user_id=user.id, workspace_id=workspace.id, title=title)
    db.session.add(c); db.session.commit()
    return c


def test_pin_conversation_toggles(auth_client, db, user, workspace):
    c = _make_conv(db, user, workspace)
    r = auth_client.post(f"/ask/c/{c.id}/pin")
    assert r.get_json()["pinned"] is True
    db.session.refresh(c)
    assert c.pinned_at is not None

    r = auth_client.post(f"/ask/c/{c.id}/pin")
    assert r.get_json()["pinned"] is False
    db.session.refresh(c)
    assert c.pinned_at is None


def test_archive_conversation_toggles(auth_client, db, user, workspace):
    c = _make_conv(db, user, workspace)
    r = auth_client.post(f"/ask/c/{c.id}/archive")
    assert r.get_json()["archived"] is True
    db.session.refresh(c)
    assert c.archived_at is not None


def test_archived_conversation_hidden_from_sidebar(auth_client, db, user, workspace):
    """The default conv list excludes archived threads."""
    from filenergy.services import conversations

    a = _make_conv(db, user, workspace, "active thread")
    b = _make_conv(db, user, workspace, "old thread")
    auth_client.post(f"/ask/c/{b.id}/archive")
    convs = conversations.list_for_user(user, workspace)
    titles = [c.title for c in convs]
    assert "active thread" in titles
    assert "old thread" not in titles


def test_pinned_conversations_float_to_top(auth_client, db, user, workspace):
    from filenergy.services import conversations

    a = _make_conv(db, user, workspace, "first")
    b = _make_conv(db, user, workspace, "second")
    c = _make_conv(db, user, workspace, "third")
    # Pin `a`, the oldest. Should jump to the top.
    auth_client.post(f"/ask/c/{a.id}/pin")
    convs = conversations.list_for_user(user, workspace)
    assert convs[0].id == a.id


# ---------- Collection share --------------------------------------------


def test_collection_share_mint_returns_token(auth_client, db, user, workspace):
    from filenergy.models import Collection, CollectionShareLink

    coll = Collection(workspace_id=workspace.id, name="Demo", slug="demo")
    db.session.add(coll); db.session.commit()
    r = auth_client.post(
        f"/collections/{coll.slug}/share",
        data="{}", content_type="application/json",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["token"]
    assert "/collections/share/" in body["url"]
    link = CollectionShareLink.query.filter_by(token=body["token"]).first()
    assert link is not None and link.collection_id == coll.id


def test_collection_share_landing_is_public(client, db, user, workspace, uploaded_file):
    from filenergy.models import Collection, CollectionShareLink, utcnow
    import secrets as sec

    coll = Collection(workspace_id=workspace.id, name="Demo", slug="demo")
    db.session.add(coll); db.session.commit()
    uploaded_file.collection_id = coll.id
    db.session.commit()
    link = CollectionShareLink(
        collection_id=coll.id, token=sec.token_urlsafe(16),
        created_by_id=user.id,
    )
    db.session.add(link); db.session.commit()

    # Anonymous client can hit the landing.
    r = client.get(f"/collections/share/{link.token}")
    assert r.status_code == 200
    assert b"Demo" in r.data
    assert uploaded_file.name.encode() in r.data


def test_collection_share_landing_404s_revoked(client, db, user, workspace):
    from filenergy.models import Collection, CollectionShareLink, utcnow

    coll = Collection(workspace_id=workspace.id, name="Demo", slug="demo2")
    db.session.add(coll); db.session.commit()
    link = CollectionShareLink(
        collection_id=coll.id, token="xyz", created_by_id=user.id,
        revoked_at=utcnow(),
    )
    db.session.add(link); db.session.commit()
    r = client.get("/collections/share/xyz")
    assert r.status_code == 404


# ---------- Reranker -----------------------------------------------------


def test_reranker_noop_passthrough(monkeypatch):
    from filenergy.services import reranker
    monkeypatch.setenv("FILENERGY_RERANKER", "noop")

    # A list of (chunk-like, embedding-score) tuples.
    class _C:
        def __init__(self, content): self.content = content
    cands = [(_C("a"), 0.9), (_C("b"), 0.8), (_C("c"), 0.7)]
    out = reranker.rerank("?", cands)
    # Top-K cap of 4 still applies.
    assert out == cands[:4]


def test_reranker_disabled_when_no_anthropic_key(monkeypatch):
    from filenergy.services import reranker
    from filenergy import settings

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("FILENERGY_RERANKER", "claude")
    assert reranker.is_enabled() is False


def test_reranker_claude_reorders_by_score(monkeypatch):
    """When the Claude backend returns scores, the reranker sorts by them."""
    from filenergy.services import reranker

    class _C:
        def __init__(self, content): self.content = content

    # Three candidates; the embedding picked them in this order, but the
    # rerank backend says #2 is best, then #0, then #1.
    candidates = [(_C("a"), 0.9), (_C("b"), 0.8), (_C("c"), 0.7)]

    class _Block:
        type = "text"
        text = '{"scores":[{"id":0,"score":3},{"id":1,"score":1},{"id":2,"score":9}]}'
    class _Msg:
        content = [_Block()]
    class _Messages:
        def create(self, **kwargs): return _Msg()
    class _FakeClient:
        messages = _Messages()

    monkeypatch.setattr(
        "filenergy.services.chat._client", lambda: _FakeClient()
    )
    monkeypatch.setenv("FILENERGY_RERANKER", "claude")
    monkeypatch.setenv("FILENERGY_RERANK_TOP_K", "3")

    out = reranker.rerank("?", candidates)
    # New order: c (id=2, score 9) → a (id=0, score 3) → b (id=1, score 1).
    assert [c.content for (c, _) in out] == ["c", "a", "b"]


def test_reranker_falls_back_on_bad_json(monkeypatch):
    from filenergy.services import reranker

    class _C:
        def __init__(self, content): self.content = content
    candidates = [(_C("a"), 0.9), (_C("b"), 0.8)]

    class _Block:
        type = "text"
        text = "not json"
    class _Msg:
        content = [_Block()]
    class _Messages:
        def create(self, **k): return _Msg()
    class _FakeClient:
        messages = _Messages()

    monkeypatch.setattr("filenergy.services.chat._client", lambda: _FakeClient())
    monkeypatch.setenv("FILENERGY_RERANKER", "claude")
    out = reranker.rerank("?", candidates)
    # Fallback returns the original embedding order, capped at top-K.
    assert [c.content for (c, _) in out] == ["a", "b"]


# ---------- Email-to-ingest ---------------------------------------------


def test_inbound_email_address_format(monkeypatch, db, workspace):
    from filenergy.services import inbound_email

    monkeypatch.setenv("FILENERGY_INBOUND_DOMAIN", "filenergy.app")
    monkeypatch.setenv("FILENERGY_INBOUND_SECRET", "test-secret")
    addr = inbound_email.address_for(workspace)
    assert addr.startswith(f"inbox-{workspace.slug}-")
    assert addr.endswith("@filenergy.app")


def test_inbound_email_disabled_when_unset(monkeypatch, workspace):
    from filenergy.services import inbound_email
    monkeypatch.delenv("FILENERGY_INBOUND_DOMAIN", raising=False)
    monkeypatch.delenv("FILENERGY_INBOUND_SECRET", raising=False)
    assert inbound_email.is_configured() is False
    assert inbound_email.address_for(workspace) == ""


def test_inbound_email_ingest_creates_files(monkeypatch, app, db, user, workspace):
    from filenergy.models import File
    from filenergy.services import inbound_email

    monkeypatch.setenv("FILENERGY_INBOUND_DOMAIN", "filenergy.app")
    monkeypatch.setenv("FILENERGY_INBOUND_SECRET", "test-secret")
    addr = inbound_email.address_for(workspace)

    payload = {
        "to": addr,
        "from": "alice@example.com",
        "subject": "Q4 review",
        "text": "Here's the latest draft.",
        "attachments": [{
            "filename": "draft.txt",
            "content": base64.b64encode(b"hello world").decode(),
            "content_type": "text/plain",
        }],
    }
    result = inbound_email.ingest_payload(payload)
    assert result["ok"] is True
    assert result["ingested"] == 2  # body + 1 attachment

    files = File.query.filter_by(workspace_id=workspace.id).all()
    names = {f.name for f in files}
    # `materialize_blob` slugifies whitespace → hyphens for filename safety.
    assert "Q4-review.md" in names
    assert "draft.txt" in names


def test_inbound_email_rejects_unknown_address(monkeypatch, app, db, user, workspace):
    from filenergy.services import inbound_email

    monkeypatch.setenv("FILENERGY_INBOUND_DOMAIN", "filenergy.app")
    monkeypatch.setenv("FILENERGY_INBOUND_SECRET", "test-secret")
    result = inbound_email.ingest_payload({
        "to": "inbox-bogus-deadbeef0000@filenergy.app",
        "subject": "x", "text": "y", "attachments": [],
    })
    assert result["ok"] is False


def test_inbound_email_rejects_forged_token(monkeypatch, app, db, user, workspace):
    """Use the right slug but a wrong HMAC token — should not match."""
    from filenergy.services import inbound_email

    monkeypatch.setenv("FILENERGY_INBOUND_DOMAIN", "filenergy.app")
    monkeypatch.setenv("FILENERGY_INBOUND_SECRET", "test-secret")
    bad = f"inbox-{workspace.slug}-000000000000@filenergy.app"
    result = inbound_email.ingest_payload({
        "to": bad, "subject": "x", "text": "y", "attachments": [],
    })
    assert result["ok"] is False


def test_inbound_email_endpoint_returns_503_when_disabled(client, monkeypatch):
    monkeypatch.delenv("FILENERGY_INBOUND_DOMAIN", raising=False)
    monkeypatch.delenv("FILENERGY_INBOUND_SECRET", raising=False)
    r = client.post("/inbound/email", data="{}", content_type="application/json")
    assert r.status_code == 503


def test_inbound_email_endpoint_checks_provider_secret(client, monkeypatch, workspace):
    from filenergy.services import inbound_email
    monkeypatch.setenv("FILENERGY_INBOUND_DOMAIN", "filenergy.app")
    monkeypatch.setenv("FILENERGY_INBOUND_SECRET", "test-secret")
    monkeypatch.setenv("FILENERGY_INBOUND_SHARED_SECRET", "provider-shared")

    r = client.post(
        "/inbound/email",
        data=json.dumps({"to": inbound_email.address_for(workspace)}),
        content_type="application/json",
    )
    assert r.status_code == 401  # missing X-Inbound-Secret header

    r = client.post(
        "/inbound/email",
        data=json.dumps({"to": inbound_email.address_for(workspace)}),
        content_type="application/json",
        headers={"X-Inbound-Secret": "provider-shared"},
    )
    assert r.status_code == 200


# ---------- SCIM 2.0 -----------------------------------------------------


def test_scim_disabled_without_token(client, monkeypatch):
    monkeypatch.delenv("FILENERGY_SCIM_TOKEN", raising=False)
    r = client.get("/scim/v2/Users", headers={"Authorization": "Bearer x"})
    assert r.status_code == 503


def test_scim_rejects_bad_bearer(client, monkeypatch, workspace):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.get(
        "/scim/v2/Users",
        headers={
            "Authorization": "Bearer wrong",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 401


def test_scim_lists_workspace_members(client, monkeypatch, workspace, user):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.get(
        "/scim/v2/Users",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["totalResults"] >= 1
    assert any(
        u["userName"] == user.email for u in body["Resources"]
    )


def test_scim_create_user_provisions(client, monkeypatch, db, workspace):
    from filenergy.models import User, WorkspaceMember

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.post(
        "/scim/v2/Users",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps({
            "userName": "scim@example.com",
            "emails": [{"value": "scim@example.com", "primary": True}],
            "active": True,
        }),
    )
    assert r.status_code == 201
    assert User.query.filter_by(email="scim@example.com").first() is not None
    assert WorkspaceMember.query.join(User).filter(
        User.email == "scim@example.com",
        WorkspaceMember.workspace_id == workspace.id,
    ).first() is not None


def test_scim_patch_active_false_removes_membership(client, monkeypatch, db, workspace):
    from filenergy.models import User, WorkspaceMember

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    bob = User(email="bob@scim.example", username="bob@scim.example")
    bob.set_password("pwd"); db.session.add(bob); db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=bob.id, role="member",
    ))
    db.session.commit()

    r = client.patch(
        f"/scim/v2/Users/{bob.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps({
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        }),
    )
    assert r.status_code == 200
    # Membership gone, user row preserved (audit + GDPR).
    assert WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=bob.id,
    ).first() is None
    assert User.query.get(bob.id) is not None


def test_scim_delete_removes_membership(client, monkeypatch, db, workspace):
    from filenergy.models import User, WorkspaceMember

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    eve = User(email="eve@scim.example", username="eve@scim.example")
    eve.set_password("pwd"); db.session.add(eve); db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=eve.id, role="member",
    ))
    db.session.commit()
    r = client.delete(
        f"/scim/v2/Users/{eve.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 204
    assert WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=eve.id,
    ).first() is None


def test_scim_service_provider_config_is_public(client, monkeypatch):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.get("/scim/v2/ServiceProviderConfig")
    assert r.status_code == 200
    body = r.get_json()
    assert body["patch"]["supported"] is True


def test_scim_schemas_endpoint(client):
    r = client.get("/scim/v2/Schemas")
    assert r.status_code == 200
    body = r.get_json()
    assert body["totalResults"] == 1
    assert "User" in body["Resources"][0]["name"]


def test_scim_resource_types_endpoint(client):
    r = client.get("/scim/v2/ResourceTypes")
    assert r.status_code == 200
    body = r.get_json()
    assert body["Resources"][0]["endpoint"] == "/Users"


def test_scim_missing_workspace_header(client, monkeypatch):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.get(
        "/scim/v2/Users",
        headers={"Authorization": "Bearer good-token"},
    )
    assert r.status_code == 400


def test_scim_unknown_workspace(client, monkeypatch):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.get(
        "/scim/v2/Users",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": "nonexistent",
        },
    )
    assert r.status_code == 404


def test_scim_get_user_returns_member(client, monkeypatch, workspace, user):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.get(
        f"/scim/v2/Users/{user.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["userName"] == user.email


def test_scim_get_user_404_when_not_in_workspace(client, monkeypatch, workspace, db):
    from filenergy.models import User

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    other = User(email="other@example.com", username="other@example.com")
    other.set_password("p"); db.session.add(other); db.session.commit()
    r = client.get(
        f"/scim/v2/Users/{other.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 404


def test_scim_get_user_404_when_unknown(client, monkeypatch, workspace):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.get(
        "/scim/v2/Users/999999",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 404


def test_scim_replace_user_changes_role(client, monkeypatch, db, workspace):
    from filenergy.models import User, WorkspaceMember

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    carol = User(email="carol@scim.example", username="carol@scim.example")
    carol.set_password("p"); db.session.add(carol); db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=carol.id, role="member",
    ))
    db.session.commit()

    r = client.put(
        f"/scim/v2/Users/{carol.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps({
            "userName": carol.email,
            "active": True,
            "urn:ietf:params:scim:schemas:extension:filenergy:2.0:User": {
                "workspaceRole": "admin",
            },
        }),
    )
    assert r.status_code == 200
    m = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=carol.id,
    ).first()
    assert m.role == "admin"


def test_scim_replace_user_inactive_removes_membership(client, monkeypatch, db, workspace):
    from filenergy.models import User, WorkspaceMember

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    dan = User(email="dan@scim.example", username="dan@scim.example")
    dan.set_password("p"); db.session.add(dan); db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=dan.id, role="member",
    ))
    db.session.commit()

    r = client.put(
        f"/scim/v2/Users/{dan.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps({"userName": dan.email, "active": False}),
    )
    assert r.status_code == 200
    assert WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=dan.id,
    ).first() is None


def test_scim_create_user_idempotent(client, monkeypatch, db, workspace):
    """POST /Users with an existing email should reuse the user row."""
    from filenergy.models import User

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    payload = {
        "userName": "shared@scim.example",
        "emails": [{"value": "shared@scim.example", "primary": True}],
        "active": True,
    }
    r1 = client.post(
        "/scim/v2/Users",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/scim/v2/Users",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
    )
    assert r2.status_code == 201
    assert User.query.filter_by(email="shared@scim.example").count() == 1


def test_scim_create_user_requires_email(client, monkeypatch, workspace):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.post(
        "/scim/v2/Users",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps({"active": True}),
    )
    assert r.status_code == 400


def test_scim_delete_user_404_when_unknown(client, monkeypatch, workspace):
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.delete(
        "/scim/v2/Users/999999",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 404


def test_scim_patch_no_op_returns_user(client, monkeypatch, db, workspace):
    """PATCH with a no-op operation list shouldn't crash; should return user."""
    from filenergy.models import User, WorkspaceMember

    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    f = User(email="f@scim.example", username="f@scim.example")
    f.set_password("p"); db.session.add(f); db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=f.id, role="member",
    ))
    db.session.commit()
    r = client.patch(
        f"/scim/v2/Users/{f.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
            "Content-Type": "application/json",
        },
        data=json.dumps({"Operations": []}),
    )
    assert r.status_code == 200


# ---------- Landing page renders -----------------------------------------


def test_landing_renders_hero_and_pricing(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.data
    # Hero copy.
    assert b"finally askable" in body
    # Pricing tiers.
    assert b"Free" in body and b"Pro" in body and b"Team" in body
    # Logo strip.
    assert b"Anthropic Claude" in body
    # Final CTA.
    assert b"Stop searching" in body
