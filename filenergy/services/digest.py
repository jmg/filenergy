"""Weekly activity digests.

`build_digest(user, workspace)` returns a (subject, body) tuple summarising
the past week's activity in one workspace from the user's perspective. The
caller — typically a cron / scheduler / RQ job — picks up the eligible users
via `users_due()` and sends them via `email.send`.

Eligibility:
- `user.weekly_digest is True` (default) or NULL → opt-in by default
- `user.last_digest_sent_at is NULL` or older than `DIGEST_INTERVAL`
- The user must have at least one workspace and that workspace must have
  had any activity in the window — otherwise we skip the send rather than
  push an empty email.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from filenergy import db
from filenergy.models import (
    Conversation, Event, File, User, WorkspaceMember, utcnow,
)
from filenergy.services import email as email_service
from filenergy.services import events as events_log

log = logging.getLogger(__name__)


DIGEST_INTERVAL = timedelta(days=7)
WINDOW = timedelta(days=7)


def _stats_for(workspace, since):
    files_uploaded = (
        File.query.filter(
            File.workspace_id == workspace.id,
            File.created_at >= since,
        ).count()
    )
    asks = Event.query.filter(
        Event.workspace_id == workspace.id,
        Event.type == "ask.answered",
        Event.created_at >= since,
    ).count()
    new_conversations = Conversation.query.filter(
        Conversation.workspace_id == workspace.id,
        Conversation.created_at >= since,
    ).count()
    new_members = Event.query.filter(
        Event.workspace_id == workspace.id,
        Event.type == "workspace.member_joined",
        Event.created_at >= since,
    ).count()
    return {
        "files_uploaded": files_uploaded,
        "asks": asks,
        "conversations": new_conversations,
        "new_members": new_members,
    }


def build_digest(user, workspace) -> tuple[str, str] | None:
    """Render a digest for one (user, workspace) pair, or None if empty."""
    since = utcnow() - WINDOW
    stats = _stats_for(workspace, since)
    if not any(stats.values()):
        return None

    subject = f"Filenergy weekly: {workspace.name}"
    lines = [
        f"Hi {user.email or user.username},",
        "",
        f"Here's what happened in {workspace.name} this past week:",
        "",
        f"  • {stats['files_uploaded']} new file"
        f"{'' if stats['files_uploaded'] == 1 else 's'} uploaded",
        f"  • {stats['asks']} question"
        f"{'' if stats['asks'] == 1 else 's'} answered",
        f"  • {stats['conversations']} new conversation"
        f"{'' if stats['conversations'] == 1 else 's'} started",
    ]
    if stats["new_members"]:
        lines.append(
            f"  • {stats['new_members']} new member"
            f"{'' if stats['new_members'] == 1 else 's'} joined"
        )
    lines += [
        "",
        "Sign in: /",
        "",
        "Don't want these? Disable weekly digests in your settings.",
    ]
    return subject, "\n".join(lines)


def users_due():
    """Yield users eligible for a digest send right now."""
    cutoff = utcnow() - DIGEST_INTERVAL
    q = User.query.filter(
        # opt-in: True or NULL counts (NULL = column added in migration,
        # not yet defaulted on existing rows)
        (User.weekly_digest.is_(True) | User.weekly_digest.is_(None))
    ).filter(
        (User.last_digest_sent_at.is_(None))
        | (User.last_digest_sent_at < cutoff)
    )
    return q.all()


def send_pending() -> int:
    """Send digests to every eligible user. Returns count of sends."""
    sent = 0
    for user in users_due():
        # Use the user's first workspace as the digest scope. We could
        # batch all workspaces in one email, but per-workspace makes the
        # subject line meaningful and keeps the body short.
        m = (
            WorkspaceMember.query.filter_by(user_id=user.id)
            .order_by(WorkspaceMember.id.asc())
            .first()
        )
        if m is None or m.workspace is None:
            continue
        rendered = build_digest(user, m.workspace)
        if rendered is None:
            continue
        subject, body = rendered
        if not email_service.send(
            to=user.email or "",
            subject=subject,
            body=body,
        ):
            log.warning("Digest send failed for user %s", user.id)
            continue
        user.last_digest_sent_at = utcnow()
        db.session.commit()
        events_log.log_event(
            events_log.USER_DIGEST_SENT,
            user=user, workspace_id=m.workspace.id,
        )
        sent += 1
    return sent
