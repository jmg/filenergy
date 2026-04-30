"""CRUD around chat conversations and messages, scoped to a workspace."""
from __future__ import annotations

import json
from dataclasses import asdict

from filenergy import db
from filenergy.models import Conversation, Message
from filenergy.services import events


def get_or_create(user, workspace, conversation_id: int | None) -> Conversation:
    if conversation_id:
        conv = Conversation.query.filter_by(
            id=conversation_id, user_id=user.id, workspace_id=workspace.id
        ).first()
        if conv is not None:
            return conv

    conv = Conversation(
        user_id=user.id, workspace_id=workspace.id, title="New conversation"
    )
    db.session.add(conv)
    db.session.commit()
    events.log_event(
        events.CONVERSATION_CREATED,
        user=user,
        workspace_id=workspace.id,
        conversation_id=conv.id,
    )
    return conv


def list_for_user(user, workspace) -> list[Conversation]:
    return (
        Conversation.query.filter_by(user_id=user.id, workspace_id=workspace.id)
        .order_by(Conversation.id.desc())
        .limit(50)
        .all()
    )


def add_user_message(conversation: Conversation, text: str) -> Message:
    msg = Message(
        conversation_id=conversation.id,
        role="user",
        content=text,
    )
    db.session.add(msg)
    if conversation.title in (None, "", "New conversation"):
        conversation.title = (text[:60] + "...") if len(text) > 60 else text
    db.session.commit()
    return msg


def add_assistant_message(
    conversation: Conversation, text: str, sources
) -> Message:
    if hasattr(sources[0] if sources else None, "__dataclass_fields__"):
        sources_payload = [asdict(s) for s in sources]
    else:
        sources_payload = list(sources)

    msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=text,
        sources_json=json.dumps(sources_payload) if sources_payload else None,
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def history(conversation: Conversation, limit: int = 12) -> list[Message]:
    msgs = (
        Message.query.filter_by(conversation_id=conversation.id)
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(msgs))


def delete(user, workspace, conversation_id: int) -> bool:
    conv = Conversation.query.filter_by(
        id=conversation_id, user_id=user.id, workspace_id=workspace.id
    ).first()
    if conv is None:
        return False
    db.session.delete(conv)
    db.session.commit()
    events.log_event(
        events.CONVERSATION_DELETED,
        user=user,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
    )
    return True
