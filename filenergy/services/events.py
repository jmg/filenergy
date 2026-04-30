"""Lightweight analytics: log every meaningful user action.

Events are scoped to (user, workspace). Queries against the Event table
back the rate limiter, billing usage gauges, and admin dashboards — keep
this module write-only.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from filenergy import db
from filenergy.models import Event

log = logging.getLogger(__name__)


# Canonical event types — keep in sync with admin filters and dashboards.
USER_REGISTERED = "user.registered"
USER_LOGGED_IN = "user.logged_in"
USER_LOGGED_OUT = "user.logged_out"
USER_LOGIN_FAILED = "user.login_failed"
USER_LOGIN_RATE_LIMITED = "user.login_rate_limited"
USER_SESSION_REVOKED = "user.session_revoked"

WORKSPACE_CREATED = "workspace.created"
WORKSPACE_SWITCHED = "workspace.switched"
WORKSPACE_INVITED = "workspace.invited"
WORKSPACE_MEMBER_JOINED = "workspace.member_joined"
WORKSPACE_MEMBER_REMOVED = "workspace.member_removed"

FILE_UPLOADED = "file.uploaded"
FILE_DELETED = "file.deleted"
FILE_DOWNLOADED = "file.downloaded"
FILE_INDEXED = "file.indexed"
FILE_INDEX_FAILED = "file.index_failed"
FILE_REINDEXED = "file.reindexed"
FILE_MADE_PUBLIC = "file.made_public"
FILE_MADE_PRIVATE = "file.made_private"
FILE_SHARED = "file.shared"
FILE_SHARE_DOWNLOADED = "file.share_downloaded"

ASK_QUESTION = "ask.question"
ASK_ANSWERED = "ask.answered"
ASK_RATE_LIMITED = "ask.rate_limited"
ASK_FAILED = "ask.failed"
ASK_QUOTA_EXCEEDED = "ask.quota_exceeded"
UPLOAD_QUOTA_EXCEEDED = "upload.quota_exceeded"

CONVERSATION_CREATED = "conversation.created"
CONVERSATION_DELETED = "conversation.deleted"

API_KEY_CREATED = "api_key.created"
API_KEY_REVOKED = "api_key.revoked"
API_KEY_USED = "api_key.used"

BILLING_CHECKOUT_STARTED = "billing.checkout_started"
BILLING_SUBSCRIPTION_UPDATED = "billing.subscription_updated"


# Subset of events that fire outbound webhooks. Internal/noisy types
# (e.g. ASK_QUESTION which fires once per keystroke-batch) are excluded.
WEBHOOK_EVENT_TYPES = {
    "file.uploaded",
    "file.indexed",
    "file.index_failed",
    "file.deleted",
    "file.shared",
    "ask.answered",
    "workspace.member_joined",
    "billing.subscription_updated",
}


def log_event(type_: str, user=None, workspace_id: int | None = None,
              **metadata: Any) -> Event:
    """Persist one event. Never raises."""
    try:
        event = Event(
            user_id=user.id if user is not None and getattr(user, "id", None) else None,
            workspace_id=workspace_id,
            type=type_,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        db.session.add(event)
        db.session.commit()
    except Exception:
        log.exception("Failed to log event %s", type_)
        db.session.rollback()
        return None  # type: ignore[return-value]

    if workspace_id is not None and type_ in WEBHOOK_EVENT_TYPES:
        try:
            from filenergy.services import webhooks  # local — avoid cycle
            webhooks.dispatch(workspace_id, type_, {
                "event_id": event.id,
                "type": type_,
                "user_id": event.user_id,
                "metadata": metadata,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            })
        except Exception:
            log.exception("Webhook dispatch failed for %s", type_)
    return event


def count_recent(user, type_: str, since_seconds: int,
                 workspace_id: int | None = None) -> int:
    from datetime import timedelta

    from filenergy.models import utcnow

    cutoff = utcnow() - timedelta(seconds=since_seconds)
    q = Event.query.filter_by(user_id=user.id, type=type_).filter(
        Event.created_at >= cutoff
    )
    if workspace_id is not None:
        q = q.filter_by(workspace_id=workspace_id)
    return q.count()


def count_recent_with_metadata(type_: str, since_seconds: int,
                                **needles: str) -> int:
    """Count events of `type_` in the last `since_seconds` whose
    `metadata_json` contains every key=value pair in `needles`.

    Used by anonymous-actor rate limits (failed logins keyed on email,
    IP allow-list checks, etc.) where there's no user_id to scope by.
    SQLite + Postgres both honour LIKE on the JSON text.
    """
    from datetime import timedelta

    from filenergy.models import utcnow

    cutoff = utcnow() - timedelta(seconds=since_seconds)
    q = Event.query.filter_by(type=type_).filter(Event.created_at >= cutoff)
    for key, value in needles.items():
        # Match the JSON-encoded form so we don't false-match substrings.
        needle = json.dumps({key: value})[1:-1]  # drop the wrapping {}
        q = q.filter(Event.metadata_json.like(f"%{needle}%"))
    return q.count()
