"""Tests for the Google OAuth helper."""
import pytest

from filenergy.services import oauth


def test_is_configured_false_without_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert oauth.is_configured() is False


def test_is_configured_true_with_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    assert oauth.is_configured() is True


def test_consume_callback_creates_user(db, app, monkeypatch):
    """Drive consume_callback against a stubbed OAuth registry."""

    class _GoogleClient:
        def authorize_access_token(self):
            return {"userinfo": {"sub": "google-123", "email": "alice@gmail.com"}}

    class _Registry:
        google = _GoogleClient()

    monkeypatch.setattr(oauth, "_oauth", lambda app=None: _Registry())
    with app.test_request_context():
        with app.test_client() as c:
            with c.session_transaction():
                pass
            user = oauth.consume_callback()
    assert user.google_id == "google-123"
    assert user.email == "alice@gmail.com"
    # ensure_default_for ran.
    assert any(m.role == "owner" for m in user.memberships.all())


def test_consume_callback_links_existing_email(db, user, app, monkeypatch):
    class _GoogleClient:
        def authorize_access_token(self):
            return {"userinfo": {"sub": "google-456", "email": user.email}}

    class _Registry:
        google = _GoogleClient()

    monkeypatch.setattr(oauth, "_oauth", lambda app=None: _Registry())
    with app.test_request_context():
        with app.test_client() as c:
            with c.session_transaction():
                pass
            returned = oauth.consume_callback()
    assert returned.id == user.id
    assert returned.google_id == "google-456"


def test_consume_callback_returns_existing_google_user(db, user, app, monkeypatch):
    user.google_id = "google-789"
    db.session.commit()

    class _GoogleClient:
        def authorize_access_token(self):
            return {"userinfo": {"sub": "google-789", "email": "different@gmail.com"}}

    class _Registry:
        google = _GoogleClient()

    monkeypatch.setattr(oauth, "_oauth", lambda app=None: _Registry())
    with app.test_request_context(), app.test_client() as c:
        with c.session_transaction():
            pass
        returned = oauth.consume_callback()
    assert returned.id == user.id


def test_consume_callback_rejects_missing_fields(db, app, monkeypatch):
    class _GoogleClient:
        def authorize_access_token(self):
            return {"userinfo": {}}

    class _Registry:
        google = _GoogleClient()

    monkeypatch.setattr(oauth, "_oauth", lambda app=None: _Registry())
    with app.test_request_context(), app.test_client() as c:
        with c.session_transaction():
            pass
        with pytest.raises(ValueError):
            oauth.consume_callback()


def test_login_redirect_invokes_authorize_redirect(monkeypatch):
    captured = {}

    class _GoogleClient:
        def authorize_redirect(self, redirect_uri):
            captured["uri"] = redirect_uri
            return "redirect-response"

    class _Registry:
        google = _GoogleClient()

    monkeypatch.setattr(oauth, "_oauth", lambda app=None: _Registry())
    out = oauth.login_redirect("http://x/cb")
    assert out == "redirect-response"
    assert captured["uri"] == "http://x/cb"


def test_oauth_factory_raises_without_authlib(monkeypatch):
    """If authlib isn't importable, _oauth() should raise."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("authlib"):
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    # Reset the singleton so the next call re-imports.
    monkeypatch.setattr(oauth, "_oauth_singleton", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError):
        oauth._oauth()
