"""Tier 1 security: CSRF, login rate limit, security headers, sessions."""
import pytest

from filenergy.models import Event, UserSession


# ---- security headers ----


def test_security_headers_present(client):
    r = client.get("/")
    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Content-Security-Policy",
    ):
        assert header in r.headers, f"missing header: {header}"


def test_x_frame_options_is_deny(client):
    r = client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"


def test_hsts_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("FILENERGY_DISABLE_HSTS", "true")
    r = client.get("/")
    assert "Strict-Transport-Security" not in r.headers


# ---- login rate limit ----


def test_login_failures_log_events(client, user, db):
    client.post("/user/login/", data={
        "email": user.email, "password": "wrong",
    })
    assert Event.query.filter_by(type="user.login_failed").count() == 1


def test_login_rate_limit_blocks_after_threshold(client, user, monkeypatch):
    from filenergy import settings as cfg
    monkeypatch.setattr(cfg, "LOGIN_RATE_LIMIT", 3)
    monkeypatch.setattr(cfg, "LOGIN_RATE_WINDOW_SECONDS", 60)

    for _ in range(3):
        client.post("/user/login/", data={
            "email": user.email, "password": "wrong",
        })
    # 4th attempt — even with the right password — gets rate-limited.
    r = client.post("/user/login/", data={
        "email": user.email, "password": "password",
    })
    assert r.status_code == 302
    assert Event.query.filter_by(type="user.login_rate_limited").count() == 1


def test_login_rate_limit_isolated_per_email(client, user, db, monkeypatch):
    """Failed attempts on attacker@x don't lock out the real user."""
    from filenergy import settings as cfg
    monkeypatch.setattr(cfg, "LOGIN_RATE_LIMIT", 2)
    for _ in range(5):
        client.post("/user/login/", data={
            "email": "attacker@x", "password": "wrong",
        })
    # Real user can still log in.
    r = client.post("/user/login/", data={
        "email": user.email, "password": "password",
    })
    assert r.status_code == 302


# ---- session management ----


def test_login_creates_user_session_row(client, user, db):
    client.post("/user/login/", data={
        "email": user.email, "password": "password",
    })
    rows = UserSession.query.filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].revoked_at is None
    assert rows[0].session_token_hash  # not the raw token


def test_logout_revokes_session(client, user, db):
    client.post("/user/login/", data={
        "email": user.email, "password": "password",
    })
    client.get("/user/logout/")
    row = UserSession.query.filter_by(user_id=user.id).first()
    assert row.revoked_at is not None


def test_security_page_lists_active_sessions(auth_client):
    r = auth_client.get("/settings/security")
    assert r.status_code == 200
    assert b"Active sessions" in r.data
    assert b"this session" in r.data


def test_revoke_session_endpoint(auth_client, user, db, app):
    # Mint a second session manually to revoke.
    from filenergy.services import sessions as session_service
    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        sess = session_service.issue(user)
    r = auth_client.post(
        f"/settings/security/sessions/{sess.id}/revoke",
        follow_redirects=False,
    )
    assert r.status_code == 302
    db.session.refresh(sess)
    assert sess.revoked_at is not None


def test_revoke_other_sessions_endpoint(auth_client, user, db, app):
    from filenergy.services import sessions as session_service
    # Add 2 extra rows beside the live one.
    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        session_service.issue(user)
        session_service.issue(user)
    before = UserSession.query.filter_by(user_id=user.id, revoked_at=None).count()
    assert before >= 3
    r = auth_client.post("/settings/security/sessions/revoke-others", follow_redirects=False)
    assert r.status_code == 302
    after = UserSession.query.filter_by(user_id=user.id, revoked_at=None).count()
    # The "current" session in the test client survives.
    assert after == 1


def test_revoked_session_cookie_logs_user_out(client, user, db, app):
    from filenergy.services import sessions as session_service

    client.post("/user/login/", data={
        "email": user.email, "password": "password",
    })
    sess = UserSession.query.filter_by(user_id=user.id, revoked_at=None).first()
    assert sess is not None

    # Revoke from the side, simulating "log out everywhere from another device".
    from filenergy.models import utcnow
    sess.revoked_at = utcnow()
    db.session.commit()

    # The same browser hits a protected page; middleware should kick them out.
    r = client.get("/settings/security", follow_redirects=False)
    assert r.status_code == 302  # redirected to login


# ---- CSRF protection (positive: with the test client, CSRF is disabled
#       via WTF_CSRF_ENABLED=False; we exercise the wiring directly) ----


def test_csrf_protection_object_registered():
    from filenergy import csrf
    from flask_wtf.csrf import CSRFProtect
    assert isinstance(csrf, CSRFProtect)


def _exempt_names(app):
    return {bp.name for bp in app.extensions["csrf"]._exempt_blueprints}


def test_api_v1_blueprint_is_csrf_exempt(app):
    """API key endpoints don't get CSRF — they auth with Bearer tokens."""
    assert "api_v1" in _exempt_names(app)


def test_stripe_webhook_blueprint_is_csrf_exempt(app):
    assert "billing" in _exempt_names(app)


def test_saml_blueprint_is_csrf_exempt(app):
    assert "saml" in _exempt_names(app)


def test_login_form_includes_csrf_token(client):
    r = client.get("/user/login/")
    assert b"csrf_token" in r.data
