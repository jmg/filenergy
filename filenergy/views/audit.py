"""Audit log UI: filter the Event table by type/date and export CSV."""
import csv
import io

from flask import Blueprint, Response, g, render_template, request
from flask_login import login_required

from filenergy.models import Event, User
from filenergy.services import workspaces

audit_bp = Blueprint("audit", __name__)


def _query_for_workspace():
    q = Event.query.filter_by(workspace_id=g.workspace.id)
    type_filter = (request.args.get("type") or "").strip()
    if type_filter:
        q = q.filter(Event.type.like(f"{type_filter}%"))
    user_filter = (request.args.get("user_id") or "").strip()
    if user_filter.isdigit():
        q = q.filter_by(user_id=int(user_filter))
    return q.order_by(Event.id.desc())


@audit_bp.route("/")
@login_required
def index():
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403

    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = 50
    q = _query_for_workspace()
    total = q.count()
    items = q.offset((page - 1) * per_page).limit(per_page).all()
    user_ids = sorted({e.user_id for e in items if e.user_id})
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    return render_template(
        "audit/index.html",
        events=items,
        users=users,
        page=page,
        per_page=per_page,
        total=total,
        has_next=(page * per_page) < total,
        type_filter=request.args.get("type", ""),
        user_filter=request.args.get("user_id", ""),
    )


@audit_bp.route("/export.csv")
@login_required
def export_csv():
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403
    rows = _query_for_workspace().limit(10_000).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "type", "user_id", "created_at", "metadata_json"])
    for e in rows:
        writer.writerow([
            e.id,
            e.type,
            e.user_id or "",
            e.created_at.isoformat() if e.created_at else "",
            e.metadata_json or "",
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="filenergy-audit-{g.workspace.slug}.csv"'
        },
    )
