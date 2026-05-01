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
    MessageFeedback,
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


@dashboard_bp.route("/evals")
@login_required
def evals():
    """Quality dashboard: how often users thumbs-up assistant answers,
    which low-rated answers regressed lately, who's giving feedback.
    """
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403

    ws_id = g.workspace.id

    # All feedback rows for this workspace (join through Message → Conversation).
    fb_q = (
        db.session.query(MessageFeedback)
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(Conversation.workspace_id == ws_id)
    )
    total = fb_q.count()
    ups = fb_q.filter(MessageFeedback.rating == "up").count()
    downs = fb_q.filter(MessageFeedback.rating == "down").count()
    ratio = (ups / total) if total else 0.0

    # Daily up/down counts for the last 30 days.
    today = utcnow().date()
    start = today - timedelta(days=29)
    daily_rows = (
        db.session.query(
            func.date(MessageFeedback.created_at),
            MessageFeedback.rating,
            func.count(MessageFeedback.id),
        )
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Conversation.workspace_id == ws_id,
            MessageFeedback.created_at >= start,
        )
        .group_by(func.date(MessageFeedback.created_at), MessageFeedback.rating)
        .all()
    )
    daily: dict[str, dict[str, int]] = {}
    for d, rating, n in daily_rows:
        daily.setdefault(str(d), {"up": 0, "down": 0})[rating] = n
    timeseries = []
    for i in range(30):
        d = str(start + timedelta(days=i))
        row = daily.get(d, {"up": 0, "down": 0})
        timeseries.append({"date": d, "up": row.get("up", 0), "down": row.get("down", 0)})

    # 25 most-recent thumbs-down answers — the queue an owner triages.
    recent_downs = (
        db.session.query(MessageFeedback, Message, Conversation)
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Conversation.workspace_id == ws_id,
            MessageFeedback.rating == "down",
        )
        .order_by(MessageFeedback.id.desc())
        .limit(25)
        .all()
    )

    return render_template(
        "dashboard/evals.html",
        total=total, ups=ups, downs=downs, ratio=ratio,
        timeseries=timeseries,
        recent_downs=recent_downs,
    )
