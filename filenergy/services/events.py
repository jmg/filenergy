"""Lightweight analytics: log every meaningful user action.

Use `log()` from request handlers and services. Aggregations live in queries
against the Event table — keep this module write-only.
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

FILE_UPLOADED = "file.uploaded"
FILE_DELETED = "file.deleted"
FILE_DOWNLOADED = "file.downloaded"
FILE_INDEXED = "file.indexed"
FILE_INDEX_FAILED = "file.index_failed"
FILE_REINDEXED = "file.reindexed"
FILE_MADE_PUBLIC = "file.made_public"
FILE_MADE_PRIVATE = "file.made_private"

ASK_QUESTION = "ask.question"
ASK_ANSWERED = "ask.answered"
ASK_RATE_LIMITED = "ask.rate_limited"
ASK_FAILED = "ask.failed"

CONVERSATION_CREATED = "conversation.created"
CONVERSATION_DELETED = "conversation.deleted"


def log_event(type_: str, user=None, **metadata: Any) -> Event:
    """Persist one event. Never raises — logging failures shouldn't break flows."""
    try:
        event = Event(
            user_id=user.id if user is not None and getattr(user, "id", None) else None,
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


def count_recent(user, type_: str, since_seconds: int) -> int:
    """How many events of `type_` did `user` emit in the last `since_seconds`?"""
    from datetime import timedelta

    from filenergy.models import utcnow

    cutoff = utcnow() - timedelta(seconds=since_seconds)
    return (
        Event.query.filter_by(user_id=user.id, type=type_)
        .filter(Event.created_at >= cutoff)
        .count()
    )
