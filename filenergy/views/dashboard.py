"""Owner-only activity dashboard.

Renders simple stat cards + a 30-day timeseries of uploads and questions.
The timeseries is built as a JSON blob and rendered as an inline SVG —
no external chart libraries.
"""
from datetime import timedelta

from flask import Blueprint, g, render_template
from flask_login import login_required
from sqlalchemy import func

from filenergy import db
from filenergy.models import (
    ApiKey,
    Chunk,
    Collection,
    Conversation,
    Event,
    File,
    Message,
    MessageCitation,
    WorkspaceMember,
    utcnow,
)
from filenergy.services import billing, workspaces

dashboard_bp = Blueprint("dashboard", __name__)


def _daily_counts(event_type: str, workspace_id: int, days: int = 30) -> list[dict]:
    """Return [{date, count}] for the last `days` days, dense (zeros included)."""
    today = utcnow().date()
    start = today - timedelta(days=days - 1)

    rows = (
        db.session.query(func.date(Event.created_at), func.count(Event.id))
        .filter(
            Event.workspace_id == workspace_id,
            Event.type == event_type,
            Event.created_at >= start,
        )
        .group_by(func.date(Event.created_at))
        .all()
    )
    counts = {str(d): n for d, n in rows}
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        out.append({"date": str(d), "count": counts.get(str(d), 0)})
    return out


@dashboard_bp.route("/")
@login_required
def index():
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403

    ws_id = g.workspace.id
    stats = {
        "files": File.query.filter_by(workspace_id=ws_id).count(),
        "indexed_files": File.query.filter(
            File.workspace_id == ws_id, File.indexed_at.isnot(None)
        ).count(),
        "collections": Collection.query.filter_by(workspace_id=ws_id).count(),
        "conversations": Conversation.query.filter_by(workspace_id=ws_id).count(),
        "messages": Message.query.join(
            Conversation, Message.conversation_id == Conversation.id
        ).filter(Conversation.workspace_id == ws_id).count(),
        "members": WorkspaceMember.query.filter_by(workspace_id=ws_id).count(),
        "api_keys_active": ApiKey.query.filter_by(
            workspace_id=ws_id, revoked_at=None
        ).count(),
    }

    uploads = _daily_counts("file.uploaded", ws_id)
    asks = _daily_counts("ask.question", ws_id)

    # Most-cited files (real chunk-level provenance from MessageCitation).
    top_files = (
        db.session.query(
            File.id, File.name, File.url,
            func.count(MessageCitation.id).label("hits"),
        )
        .join(Chunk, Chunk.file_id == File.id)
        .join(MessageCitation, MessageCitation.chunk_id == Chunk.id)
        .filter(File.workspace_id == ws_id)
        .group_by(File.id)
        .order_by(func.count(MessageCitation.id).desc())
        .limit(5)
        .all()
    )

    # Most-cited chunks across the workspace (with snippet for the dashboard).
    top_chunks = (
        db.session.query(
            Chunk.id, Chunk.position, Chunk.content,
            File.name, File.url,
            func.count(MessageCitation.id).label("hits"),
        )
        .join(File, File.id == Chunk.file_id)
        .join(MessageCitation, MessageCitation.chunk_id == Chunk.id)
        .filter(File.workspace_id == ws_id)
        .group_by(Chunk.id)
        .order_by(func.count(MessageCitation.id).desc())
        .limit(5)
        .all()
    )

    return render_template(
        "dashboard/index.html",
        stats=stats,
        uploads=uploads,
        asks=asks,
        top_files=top_files,
        top_chunks=top_chunks,
        usage=billing.usage_summary(g.workspace),
    )
