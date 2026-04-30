"""HTTP-level tests for the round-5 features.

Covers: 2FA login flow, /settings/security, account deletion, conversation
export, bulk delete, /metrics, OpenAPI, error pages, OAuth views.
"""
import io
import json

import pytest


# ---- 404 / 500 handlers ----


def test_404_renders_template(client):
    r = client.get("/this-does-not-exist")
    assert r.status_code == 404
    assert b"404" in r.data
    assert b"Back home" in r.data


def test_500_handler_renders(app):
    """The 500 handler is registered and renders the template."""
    from flask import g

    from filenergy.views import _internal_error
    with app.test_request_context():
        # base.html reads g.user; provide a stand-in.
        class _Anon:
            is_authenticated = False
        g.user = _Anon()
        g.workspace = None
        body, status = _internal_error(RuntimeError("x"))
        assert status == 500
        text = body if isinstance(body, str) else body.decode()
        assert "500" in text


def test_500_handler_in_url_map():
    """Cheaper sanity check: the handler is registered against the app."""
    from filenergy import app as flask_app
    # Werkzeug stores error handlers under app.error_handler_spec.
    spec = flask_app.error_handler_spec[None]
    statuses = {code for code in spec.keys()}
    assert 500 in statuses
    assert 404 in statuses


# ---- /metrics + /healthz ----


def test_metrics_endpoint_renders(client):
    client.get("/")
    client.get("/")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "filenergy_http_requests_total" in r.get_data(as_text=True)
    assert "filenergy_http_request_duration_seconds" in r.get_data(as_text=True)


def test_after_request_emits_log(client, caplog):
    with caplog.at_level("INFO", logger="filenergy.request"):
        client.get("/")
    assert any(r.message == "request" for r in caplog.records)


# ---- OpenAPI + Swagger UI ----


def test_openapi_spec_served(client):
    r = client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    spec = r.get_json()
    assert spec["openapi"].startswith("3.")
    assert "/files" in spec["paths"]
    assert "/ask" in spec["paths"]


def test_docs_page_loads(client):
    r = client.get("/api/v1/docs")
    assert r.status_code == 200
    assert b"swagger-ui" in r.data


# ---- 2FA login flow ----


def _enable_totp(user, app):
    import pyotp

    from filenergy.services import totp
    with app.test_request_context():
        totp.start_setup(user)
        ok = totp.enable(user, pyotp.TOTP(user.totp_secret).now())
    assert ok
    return user.totp_secret


def test_login_without_2fa_works(client, user):
    r = client.post(
        "/user/login/", data={"email": user.email, "password": "password"}
    )
    assert r.status_code == 302


def test_login_with_2fa_requires_otp(client, user, app):
    _enable_totp(user, app)
    r = client.post(
        "/user/login/", data={"email": user.email, "password": "password"}
    )
    assert r.status_code == 302
    assert "/user/2fa" in r.headers["Location"]


def test_2fa_page_redirects_when_no_pending(client):
    r = client.get("/user/2fa", follow_redirects=False)
    assert r.status_code == 302


def test_2fa_post_without_pending_redirects(client):
    r = client.post("/user/2fa", data={"code": "x"}, follow_redirects=False)
    assert r.status_code == 302
    assert "/user/login/" in r.headers["Location"]


def test_2fa_post_invalid_code_flashes(client, user, app):
    _enable_totp(user, app)
    client.post("/user/login/", data={"email": user.email, "password": "password"})
    r = client.post("/user/2fa", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 302
    assert "/user/2fa" in r.headers["Location"]


def test_2fa_post_with_valid_otp_completes_login(client, user, app):
    import pyotp

    secret = _enable_totp(user, app)
    client.post("/user/login/", data={"email": user.email, "password": "password"})
    code = pyotp.TOTP(secret).now()
    r = client.post("/user/2fa", data={"code": code}, follow_redirects=False)
    assert r.status_code == 302


def test_2fa_post_with_recovery_code(client, user, app):
    import pyotp
    from filenergy.services import totp

    with app.test_request_context():
        totp.start_setup(user)
        totp.enable(user, pyotp.TOTP(user.totp_secret).now())
        codes = totp.regenerate_recovery_codes(user)

    client.post("/user/login/", data={"email": user.email, "password": "password"})
    r = client.post("/user/2fa", data={"code": codes[0]}, follow_redirects=False)
    assert r.status_code == 302


def test_login_wrong_password_flashes(client, user):
    r = client.post(
        "/user/login/", data={"email": user.email, "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/user/login/" in r.headers["Location"]


# ---- /settings/security ----


def test_security_page_loads(auth_client):
    r = auth_client.get("/settings/security")
    assert r.status_code == 200


def test_totp_start_renders_qr(auth_client):
    r = auth_client.post("/settings/security/totp/start")
    assert r.status_code == 200
    assert b"<svg" in r.data
    assert b"otpauth://" in r.data


def test_totp_start_when_already_enabled_redirects(auth_client, user, app):
    _enable_totp(user, app)
    r = auth_client.post("/settings/security/totp/start", follow_redirects=False)
    assert r.status_code == 302


def test_totp_enable_invalid_code_redirects(auth_client):
    auth_client.post("/settings/security/totp/start")
    r = auth_client.post(
        "/settings/security/totp/enable", data={"code": "000000"},
        follow_redirects=False,
    )
    assert r.status_code == 302


def test_totp_enable_valid_code(auth_client, user, app):
    auth_client.post("/settings/security/totp/start")
    import pyotp
    from filenergy import db
    db.session.refresh(user)
    code = pyotp.TOTP(user.totp_secret).now()
    r = auth_client.post(
        "/settings/security/totp/enable", data={"code": code},
        follow_redirects=False,
    )
    assert r.status_code == 302
    db.session.refresh(user)
    assert user.totp_enabled


def test_totp_disable(auth_client, user, app):
    _enable_totp(user, app)
    r = auth_client.post("/settings/security/totp/disable", follow_redirects=False)
    assert r.status_code == 302
    from filenergy import db
    db.session.refresh(user)
    assert user.totp_enabled is False


def test_totp_regenerate_recovery_codes(auth_client, user, app):
    _enable_totp(user, app)
    r = auth_client.post("/settings/security/totp/recover", follow_redirects=False)
    assert r.status_code == 302


def test_totp_regenerate_when_not_enabled_redirects_with_error(auth_client):
    r = auth_client.post("/settings/security/totp/recover", follow_redirects=False)
    assert r.status_code == 302


# ---- Account deletion ----


def test_delete_account_requires_email_confirmation(auth_client):
    r = auth_client.post(
        "/settings/account/delete", data={"confirm": "wrong@email"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    from filenergy.models import User
    assert User.query.count() == 1


def test_delete_account_succeeds(auth_client, user, db, workspace):
    """Owned workspace, files, conversations all wiped."""
    import io as io_module
    auth_client.post(
        "/file/upload/",
        data={"files[]": (io_module.BytesIO(b"hi"), "x.txt")},
        content_type="multipart/form-data",
    )
    auth_client.post("/ask/", json={"question": "?"})

    r = auth_client.post(
        "/settings/account/delete", data={"confirm": user.email},
        follow_redirects=False,
    )
    assert r.status_code == 302

    from filenergy.models import (
        Conversation, File, User, Workspace, WorkspaceMember,
    )
    assert User.query.filter_by(id=user.id).first() is None
    assert Workspace.query.count() == 0
    assert File.query.count() == 0
    assert Conversation.query.count() == 0
    assert WorkspaceMember.query.count() == 0


# ---- Conversation export ----


def test_export_markdown(auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={"question": "What about apples?"})
    cid = r.get_json()["conversation_id"]
    md = auth_client.get(f"/ask/c/{cid}/export.md")
    assert md.status_code == 200
    assert md.mimetype == "text/markdown"
    body = md.get_data(as_text=True)
    assert "**You**" in body
    assert "**Assistant**" in body
    assert "Sources:" in body


def test_export_markdown_404_for_other_workspace(client, db, auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]

    from filenergy.models import User
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    auth_client.get("/user/logout/")
    auth_client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = auth_client.get(f"/ask/c/{cid}/export.md")
    assert r.status_code == 404


def test_export_markdown_handles_corrupt_sources(auth_client, db, user, workspace):
    """An assistant message with malformed sources_json shouldn't crash export."""
    from filenergy.models import Conversation, Message
    c = Conversation(user_id=user.id, workspace_id=workspace.id, title="t")
    db.session.add(c)
    db.session.commit()
    db.session.add(Message(
        conversation_id=c.id, role="user", content="q",
    ))
    db.session.add(Message(
        conversation_id=c.id, role="assistant", content="a",
        sources_json="not json",
    ))
    db.session.commit()
    r = auth_client.get(f"/ask/c/{c.id}/export.md")
    assert r.status_code == 200


# ---- Bulk delete ----


def test_bulk_delete(auth_client, db, user, workspace):
    """Upload three files, bulk delete two of them."""
    import io as io_module

    for i, name in enumerate(["a.txt", "b.txt", "c.txt"]):
        auth_client.post(
            "/file/upload/",
            data={"files[]": (io_module.BytesIO(f"hi {i}".encode()), name)},
            content_type="multipart/form-data",
        )
    from filenergy.models import File
    files = File.query.filter_by(workspace_id=workspace.id).all()
    ids = [f.id for f in files[:2]]
    r = auth_client.post(
        "/file/bulk_delete/", data={"ids[]": ids}
    )
    assert r.status_code == 200
    assert r.get_json()["deleted"] == 2
    assert File.query.filter_by(workspace_id=workspace.id).count() == 1


def test_bulk_delete_empty_request(auth_client):
    r = auth_client.post("/file/bulk_delete/")
    assert r.status_code == 200
    assert r.get_json()["deleted"] == 0


def test_bulk_delete_silently_ignores_other_workspaces(
    auth_client, db, user, workspace
):
    from filenergy.models import File, User
    from filenergy.services import workspaces as ws_service

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    other_ws = ws_service.ensure_default_for(other)
    foreign = File(
        user_id=other.id, workspace_id=other_ws.id, name="x", path="/x", url="zz",
    )
    db.session.add(foreign)
    db.session.commit()
    r = auth_client.post(
        "/file/bulk_delete/", data={"ids[]": [foreign.id, "garbage"]}
    )
    assert r.status_code == 200
    # Foreign file untouched.
    assert File.query.filter_by(id=foreign.id).first() is not None


# ---- OAuth views ----


def test_oauth_login_redirects_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    r = client.get("/user/oauth/google/login", follow_redirects=False)
    assert r.status_code == 302
    assert "/user/login/" in r.headers["Location"]


def test_oauth_login_redirects_to_google_when_configured(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")

    captured = {}

    class _G:
        def authorize_redirect(self, redirect_uri):
            captured["uri"] = redirect_uri
            from flask import redirect
            return redirect("https://accounts.google.com/test")

    class _R:
        google = _G()

    from filenergy.services import oauth as oauth_module
    monkeypatch.setattr(oauth_module, "_oauth", lambda app=None: _R())

    r = client.get("/user/oauth/google/login", follow_redirects=False)
    assert r.status_code == 302
    assert "google.com" in r.headers["Location"]
    assert "uri" in captured


def test_oauth_callback_handles_error(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")

    class _G:
        def authorize_access_token(self):
            raise RuntimeError("user denied")

    class _R:
        google = _G()

    from filenergy.services import oauth as oauth_module
    monkeypatch.setattr(oauth_module, "_oauth", lambda app=None: _R())

    r = client.get("/user/oauth/google/callback", follow_redirects=False)
    assert r.status_code == 302
    assert "/user/login/" in r.headers["Location"]


def test_oauth_callback_creates_user(client, db, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")

    class _G:
        def authorize_access_token(self):
            return {"userinfo": {"sub": "g-1", "email": "newby@gmail.com"}}

    class _R:
        google = _G()

    from filenergy.services import oauth as oauth_module
    monkeypatch.setattr(oauth_module, "_oauth", lambda app=None: _R())

    r = client.get("/user/oauth/google/callback", follow_redirects=False)
    assert r.status_code == 302
    from filenergy.models import User
    assert User.query.filter_by(google_id="g-1").one()


def test_oauth_callback_redirects_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    r = client.get("/user/oauth/google/callback", follow_redirects=False)
    assert r.status_code == 302
    assert "/user/login/" in r.headers["Location"]


# ---- User model password check on null ----


def test_check_password_returns_false_when_no_password(db):
    """OAuth-only users have no password set."""
    from filenergy.models import User
    u = User(email="oauth@x", username="oauth@x", google_id="g-1")
    db.session.add(u)
    db.session.commit()
    assert u.check_password("anything") is False
