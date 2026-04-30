"""Federated identity via Google OAuth (OpenID Connect).

Plug and play: flips on when GOOGLE_OAUTH_CLIENT_ID + ..._SECRET are set
in the environment. Until then `is_configured()` is False and the views
hide the "Sign in with Google" button.
"""
from __future__ import annotations

import os

from flask_login import login_user

from filenergy import db
from filenergy.models import User
from filenergy.services import workspaces


GOOGLE_DISCOVERY_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)


def is_configured() -> bool:
    return bool(_client_id() and _client_secret())


def _client_id() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")


_oauth_singleton = None


def _oauth(app=None):
    """Return the Authlib `OAuth` registry. Lazy-import + lazy-register."""
    global _oauth_singleton
    if _oauth_singleton is not None:
        return _oauth_singleton
    try:
        from authlib.integrations.flask_client import OAuth  # type: ignore
    except ImportError as exc:
        raise RuntimeError("authlib not installed") from exc

    from filenergy import app as flask_app
    oauth = OAuth(app or flask_app)
    oauth.register(
        name="google",
        client_id=_client_id(),
        client_secret=_client_secret(),
        server_metadata_url=GOOGLE_DISCOVERY_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    _oauth_singleton = oauth
    return oauth


def login_redirect(redirect_uri: str):
    """Issue the redirect to Google's OAuth consent screen."""
    return _oauth().google.authorize_redirect(redirect_uri)


def consume_callback() -> User:
    """Complete the OAuth dance and return the resulting User row.

    Creates the user + a default workspace if this is their first login.
    Logs the user into Flask-Login.
    """
    token = _oauth().google.authorize_access_token()
    info = token.get("userinfo") or {}
    sub = info.get("sub")
    email = (info.get("email") or "").lower()
    if not sub or not email:
        raise ValueError("Google response missing sub/email")

    user = (
        User.query.filter_by(google_id=sub).first()
        or User.query.filter_by(email=email).first()
    )
    if user is None:
        user = User(email=email, username=email, google_id=sub)
        db.session.add(user)
        db.session.commit()
    elif not user.google_id:
        # Link Google to an existing email-only account.
        user.google_id = sub
        db.session.commit()

    workspaces.ensure_default_for(user)
    login_user(user)
    return user
