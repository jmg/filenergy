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
        return event
    except Exception:
        log.exception("Failed to log event %s", type_)
        db.session.rollback()
        return None  # type: ignore[return-value]


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
