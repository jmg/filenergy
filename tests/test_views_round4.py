"""HTTP-level tests for the round-4 features."""
import io
import json

import pytest

from filenergy.models import (
    Collection,
    Event,
    File,
    User,
    WebhookSubscription,
    WorkspaceMember,
)


# ---- Health ----


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_readyz_ok(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["db"]["ok"] is True
    assert "anthropic" in body
    assert "stripe" in body


def test_readyz_db_failure(client, monkeypatch):
    from filenergy import db

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db.session, "execute", boom)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.get_json()["db"]["ok"] is False


# ---- Collections ----


def test_collections_list_loads(auth_client):
    r = auth_client.get("/collections/")
    assert r.status_code == 200


def test_collections_create_and_view(auth_client, workspace):
    r = auth_client.post(
        "/collections/", data={"name": "Engineering"}, follow_redirects=False
    )
    assert r.status_code == 302
    coll = Collection.query.filter_by(workspace_id=workspace.id).one()
    assert coll.slug == "engineering"
    page = auth_client.get(f"/collections/{coll.slug}")
    assert page.status_code == 200
    assert b"Engineering" in page.data


def test_collections_view_404(auth_client):
    r = auth_client.get("/collections/nope")
    assert r.status_code == 404


def test_collections_rename(auth_client, workspace):
    auth_client.post("/collections/", data={"name": "Old"})
    coll = Collection.query.first()
    r = auth_client.post(
        f"/collections/{coll.slug}/rename", data={"name": "New"}
    )
    assert r.status_code == 302
    from filenergy import db
    db.session.refresh(coll)
    assert coll.name == "New"


def test_collections_rename_404(auth_client):
    assert auth_client.post(
        "/collections/garbage/rename", data={"name": "X"}
    ).status_code == 404


def test_collections_delete(auth_client, workspace):
    auth_client.post("/collections/", data={"name": "Trash"})
    coll = Collection.query.first()
    r = auth_client.post(f"/collections/{coll.slug}/delete")
    assert r.status_code == 302
    assert Collection.query.count() == 0


def test_collections_delete_404(auth_client):
    assert auth_client.post(
        "/collections/garbage/delete"
    ).status_code == 404


def test_assign_file_to_collection(auth_client, db, user, workspace):
    auth_client.post("/collections/", data={"name": "Eng"})
    coll = Collection.query.first()
    f = File(user_id=user.id, workspace_id=workspace.id, name="x", path="/x", url="u1")
    db.session.add(f)
    db.session.commit()
    r = auth_client.post(
        "/collections/assign", data={"file_id": f.id, "collection_id": coll.id}
    )
    assert r.status_code == 200
    db.session.refresh(f)
    assert f.collection_id == coll.id

    # Move out
    r = auth_client.post(
        "/collections/assign", data={"file_id": f.id, "collection_id": ""}
    )
    db.session.refresh(f)
    assert f.collection_id is None


def test_assign_unknown_file(auth_client):
    r = auth_client.post(
        "/collections/assign", data={"file_id": 9999, "collection_id": ""}
    )
    assert r.status_code == 404


def test_assign_unknown_collection(auth_client, db, user, workspace):
    f = File(user_id=user.id, workspace_id=workspace.id, name="x", path="/x", url="u1")
    db.session.add(f)
    db.session.commit()
    r = auth_client.post(
        "/collections/assign", data={"file_id": f.id, "collection_id": 9999}
    )
    assert r.status_code == 404


# ---- File detail ----


def test_file_detail_page(auth_client, uploaded_file):
    r = auth_client.get(f"/file/{uploaded_file.id}")
    assert r.status_code == 200
    assert b"fruits.txt" in r.data


def test_file_detail_404(auth_client):
    assert auth_client.get("/file/9999").status_code == 404


def test_file_detail_other_workspace_404(client, db, uploaded_file):
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    from filenergy.services import workspaces
    workspaces.ensure_default_for(other)
    client.get("/user/logout/")
    client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = client.get(f"/file/{uploaded_file.id}")
    assert r.status_code == 404


# ---- Scoped ask ----


def test_ask_with_collection_scope(auth_client, db, user, workspace, uploaded_file):
    """Send a question scoped to a collection."""
    from filenergy.services import collections as coll_service
    coll = coll_service.create(workspace, "scope")
    uploaded_file.collection_id = coll.id
    db.session.commit()
    r = auth_client.post("/ask/", json={
        "question": "What about apples?",
        "collection_id": coll.id,
    })
    assert r.status_code == 200
    e = Event.query.filter_by(type="ask.question").order_by(Event.id.desc()).first()
    meta = json.loads(e.metadata_json)
    assert meta["collection_id"] == coll.id


def test_ask_with_file_scope(auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={
        "question": "What about apples?",
        "file_id": uploaded_file.id,
    })
    assert r.status_code == 200


def test_ask_with_invalid_scope_returns_404(auth_client):
    r = auth_client.post("/ask/", json={
        "question": "x", "collection_id": 9999,
    })
    assert r.status_code == 404


def test_ask_with_other_workspace_file_404(auth_client, db, user, uploaded_file):
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    from filenergy.services import workspaces
    foreign_ws = workspaces.ensure_default_for(other)
    foreign_file = File(
        user_id=other.id, workspace_id=foreign_ws.id,
        name="theirs", path="/x", url="zz",
    )
    db.session.add(foreign_file)
    db.session.commit()
    r = auth_client.post("/ask/", json={
        "question": "x", "file_id": foreign_file.id,
    })
    assert r.status_code == 404


def test_ask_index_with_scope_args(auth_client, db, user, workspace, uploaded_file):
    from filenergy.services import collections as coll_service
    coll = coll_service.create(workspace, "scope")
    r = auth_client.get(f"/ask/?collection={coll.slug}&file_id={uploaded_file.id}")
    assert r.status_code == 200


def test_ask_stream_with_invalid_scope(auth_client):
    r = auth_client.post(
        "/ask/stream", json={"question": "x", "collection_id": 9999}
    )
    assert r.status_code == 404


def test_ask_stream_with_collection_scope(auth_client, workspace, uploaded_file):
    from filenergy.services import collections as coll_service
    coll = coll_service.create(workspace, "x")
    r = auth_client.post(
        "/ask/stream",
        json={"question": "?", "collection_id": coll.id},
    )
    assert r.status_code == 200


# ---- Audit log ----


def test_audit_requires_admin(client, db, user, workspace):
    other = User(email="m@m", username="m")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    client.post("/user/login/", data={"email": "m@m", "password": "pw"})
    client.post(f"/w/switch/{workspace.id}")
    r = client.get("/audit/")
    assert r.status_code == 403


def test_audit_loads_for_owner(auth_client, uploaded_file):
    r = auth_client.get("/audit/")
    assert r.status_code == 200
    # uploaded_file fixture emits some events; they should appear
    assert b"file.uploaded" in r.data


def test_audit_filter_by_type(auth_client, uploaded_file):
    r = auth_client.get("/audit/?type=file.")
    assert r.status_code == 200


def test_audit_filter_by_user(auth_client, user, uploaded_file):
    r = auth_client.get(f"/audit/?user_id={user.id}")
    assert r.status_code == 200


def test_audit_pagination(auth_client, user, workspace, app):
    """Many events to force a second page."""
    from filenergy.services import events
    with app.test_request_context():
        for _ in range(60):
            events.log_event("test.event", user=user, workspace_id=workspace.id)
    r = auth_client.get("/audit/?page=2")
    assert r.status_code == 200


def test_audit_export_csv(auth_client, uploaded_file):
    r = auth_client.get("/audit/export.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    # First line is the header.
    body = r.get_data(as_text=True)
    assert body.startswith("id,type,user_id,created_at,metadata_json")


def test_audit_export_csv_requires_admin(client, db, user, workspace):
    other = User(email="m@m", username="m")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    client.post("/user/login/", data={"email": "m@m", "password": "pw"})
    client.post(f"/w/switch/{workspace.id}")
    assert client.get("/audit/export.csv").status_code == 403


# ---- Webhooks settings ----


def test_webhooks_page_loads(auth_client):
    r = auth_client.get("/settings/webhooks")
    assert r.status_code == 200


def test_create_webhook_validates_url(auth_client):
    r = auth_client.post(
        "/settings/webhooks", data={"url": "not-a-url", "events": ["file.uploaded"]}
    )
    assert r.status_code == 302  # flash + redirect
    assert WebhookSubscription.query.count() == 0


def test_create_webhook_requires_events(auth_client):
    r = auth_client.post(
        "/settings/webhooks", data={"url": "https://x/", "events": []}
    )
    assert r.status_code == 302
    assert WebhookSubscription.query.count() == 0


def test_create_and_toggle_and_delete_webhook(auth_client, workspace):
    r = auth_client.post(
        "/settings/webhooks",
        data={"url": "https://x.test/h", "events": ["file.uploaded"]},
    )
    assert r.status_code == 302
    sub = WebhookSubscription.query.one()

    r = auth_client.post(f"/settings/webhooks/{sub.id}/toggle")
    assert r.status_code == 302
    from filenergy import db
    db.session.refresh(sub)
    assert sub.enabled is False

    r = auth_client.post(f"/settings/webhooks/{sub.id}/delete")
    assert r.status_code == 302
    assert WebhookSubscription.query.count() == 0


def test_toggle_unknown_webhook_404(auth_client):
    assert auth_client.post(
        "/settings/webhooks/9999/toggle"
    ).status_code == 404


def test_delete_unknown_webhook_404(auth_client):
    assert auth_client.post(
        "/settings/webhooks/9999/delete"
    ).status_code == 404


# ---- Webhooks fire on events ----


def test_event_log_dispatches_webhook(auth_client, db, user, workspace, app, monkeypatch):
    """Wiring smoke test: log_event triggers dispatch."""
    captured = []

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return b""

    def fake_urlopen(req, timeout=None):
        captured.append({"url": req.full_url, "body": req.data})
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    from filenergy.services import webhooks, events as ev

    with app.test_request_context():
        webhooks.create(workspace, "https://x.test/", ["file.uploaded"])
        ev.log_event(
            ev.FILE_UPLOADED, user=user, workspace_id=workspace.id, file_id=1
        )
    # urllib.request was called once.
    assert len(captured) == 1


def test_event_log_doesnt_dispatch_for_internal_events(
    db, user, workspace, app, monkeypatch
):
    captured = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: captured.append(1),
    )
    from filenergy.services import webhooks, events as ev

    with app.test_request_context():
        webhooks.create(workspace, "https://x.test/", ["file.uploaded"])
        # ASK_QUESTION is not in WEBHOOK_EVENT_TYPES.
        ev.log_event(
            ev.ASK_QUESTION, user=user, workspace_id=workspace.id
        )
    assert captured == []


# ---- Email wired into invitations ----


def test_invite_sends_email(auth_client, monkeypatch, workspace):
    sent = {}

    def fake_send(to, subject, body):
        sent["to"] = to
        sent["subject"] = subject
        sent["body"] = body
        return True

    monkeypatch.setattr("filenergy.services.email.send", fake_send)
    r = auth_client.post(
        "/w/invite", data={"email": "joiner@x.co", "role": "member"}
    )
    assert r.status_code == 302
    assert sent["to"] == "joiner@x.co"
    assert "invited" in sent["body"]
