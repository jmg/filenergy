"""Session lifecycle.

We layer this on top of Flask-Login's signed-cookie session: every login
mints a `session_id` (random) which is stored in the Flask session AND
on a `UserSession` row. The middleware validates the cookie's claim by
looking up the row and checking it isn't revoked. On every authenticated
request, `last_seen_at` is updated.

Revoking a session = setting `revoked_at` and removing the row's hash
match. The cookie still exists in the browser but no longer maps to an
active row, so the user is forced back to login.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from flask import request, session
from flask_login import current_user

from filenergy import db
from filenergy.models import User, UserSession, utcnow


SESSION_COOKIE_KEY = "fln_session_id"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(user: User) -> UserSession:
    """Mint a fresh `UserSession` row + remember the token in Flask's session."""
    raw = secrets.token_urlsafe(32)
    row = UserSession(
        user_id=user.id,
        session_token_hash=_hash(raw),
        user_agent=(request.headers.get("User-Agent") or "")[:512],
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or "")[:64],
        last_seen_at=utcnow(),
    )
    db.session.add(row)
    db.session.commit()
    session[SESSION_COOKIE_KEY] = raw
    return row


def current() -> Optional[UserSession]:
    """Resolve the active UserSession from the current Flask cookie."""
    if not getattr(current_user, "is_authenticated", False):
        return None
    raw = session.get(SESSION_COOKIE_KEY)
    if not raw:
        return None
    row = UserSession.query.filter_by(
        session_token_hash=_hash(raw), user_id=current_user.id,
    ).first()
    if row is None or row.revoked_at is not None:
        return None
    return row


def touch(row: UserSession) -> None:
    """Update last_seen_at; called by middleware on every auth'd request."""
    if row is None:
        return
    row.last_seen_at = utcnow()
    db.session.commit()


def list_active(user: User) -> list[UserSession]:
    return (
        UserSession.query.filter_by(user_id=user.id, revoked_at=None)
        .order_by(UserSession.last_seen_at.desc())
        .all()
    )


def revoke(user: User, session_id: int) -> bool:
    row = UserSession.query.filter_by(
        id=session_id, user_id=user.id, revoked_at=None,
    ).first()
    if row is None:
        return False
    row.revoked_at = utcnow()
    db.session.commit()
    return True


def revoke_all_others(user: User) -> int:
    """Revoke every active session for `user` except the one we're on."""
    keep = current()
    keep_id = keep.id if keep else None
    q = UserSession.query.filter_by(user_id=user.id, revoked_at=None)
    if keep_id is not None:
        q = q.filter(UserSession.id != keep_id)
    revoked = 0
    for row in q.all():
        row.revoked_at = utcnow()
        revoked += 1
    db.session.commit()
    return revoked


def revoke_on_logout() -> None:
    """Called from the logout view to retire the row + clear the cookie."""
    raw = session.pop(SESSION_COOKIE_KEY, None)
    if not raw or not getattr(current_user, "is_authenticated", False):
        return
    row = UserSession.query.filter_by(
        session_token_hash=_hash(raw), user_id=current_user.id,
    ).first()
    if row is not None and row.revoked_at is None:
        row.revoked_at = utcnow()
        db.session.commit()


def is_session_alive() -> bool:
    """Cheap check used by middleware: the cookie still maps to an active row.

    Returns True when there's no cookie at all (anonymous request) so the
    middleware doesn't churn on every public hit.
    """
    if not getattr(current_user, "is_authenticated", False):
        return True
    raw = session.get(SESSION_COOKIE_KEY)
    if not raw:
        # Logged in via Flask-Login but no UserSession token (e.g.
        # legacy cookie predating this feature). Allow once and let
        # the next login mint a token.
        return True
    return current() is not None
