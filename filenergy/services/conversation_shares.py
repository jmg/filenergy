"""Public read-only share links for Conversations.

The thread's transcript renders at `/sc/<token>`. Optional TTL via
`expires_at`. Revocable. Downloads not counted (read-only HTML, not a
file), but `view_count` increments on each successful landing hit.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from filenergy import db
from filenergy.models import ConversationShareLink, utcnow


def create(conversation, *, created_by, ttl_hours: Optional[int] = None
           ) -> ConversationShareLink:
    expires_at = utcnow() + timedelta(hours=ttl_hours) if ttl_hours else None
    link = ConversationShareLink(
        conversation_id=conversation.id,
        token=secrets.token_urlsafe(24),
        expires_at=expires_at,
        created_by_id=created_by.id,
    )
    db.session.add(link)
    db.session.commit()
    return link


def find_active(token: str) -> Optional[ConversationShareLink]:
    link = ConversationShareLink.query.filter_by(token=token).first()
    if link is None:
        return None
    return link if link.is_active() else None


def record_view(link: ConversationShareLink) -> None:
    link.view_count = (link.view_count or 0) + 1
    db.session.commit()


def revoke(link: ConversationShareLink) -> None:
    link.revoked_at = utcnow()
    db.session.commit()


def list_for_conversation(conversation) -> list[ConversationShareLink]:
    return (
        ConversationShareLink.query.filter_by(conversation_id=conversation.id)
        .order_by(ConversationShareLink.id.desc())
        .all()
    )
