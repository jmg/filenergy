"""Connector OAuth + sync routes."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import login_required

from filenergy.models import ConnectorAccount
from filenergy.services import connectors, events, workspaces

connectors_bp = Blueprint("connectors", __name__)


@connectors_bp.route("/")
@login_required
def index():
    return render_template(
        "connectors/index.html",
        all_connectors=connectors.all_connectors(),
        accounts=connectors.list_accounts(g.workspace),
    )


@connectors_bp.route("/<kind>/connect")
@login_required
def connect(kind):
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403
    conn = connectors.get(kind)
    if conn is None:
        abort(404)
    if not conn.is_configured():
        flash(f"{conn.label} OAuth client is not configured on this server.", "error")
        return redirect(url_for("connectors.index"))
    redirect_uri = url_for("connectors.callback", kind=kind, _external=True)
    try:
        return redirect(conn.authorize_url(redirect_uri, g.workspace.id))
    except connectors.ConnectorError as exc:
        flash(str(exc), "error")
        return redirect(url_for("connectors.index"))


@connectors_bp.route("/<kind>/callback")
@login_required
def callback(kind):
    conn = connectors.get(kind)
    if conn is None:
        abort(404)
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code or not state:
        flash("Missing code/state from OAuth callback.", "error")
        return redirect(url_for("connectors.index"))
    redirect_uri = url_for("connectors.callback", kind=kind, _external=True)
    try:
        conn.complete_oauth(code=code, state=state, redirect_uri=redirect_uri)
    except connectors.ConnectorError as exc:
        flash(f"Connection failed: {exc}", "error")
        return redirect(url_for("connectors.index"))
    flash(f"{conn.label} connected.", "success")
    return redirect(url_for("connectors.index"))


@connectors_bp.route("/accounts/<int:account_id>/sync", methods=["POST"])
@login_required
def sync(account_id):
    account = ConnectorAccount.query.filter_by(
        id=account_id, workspace_id=g.workspace.id
    ).first()
    if account is None:
        abort(404)
    conn = connectors.get(account.kind)
    if conn is None:
        abort(404)
    try:
        result = conn.sync(account, user=g.user, workspace=g.workspace)
    except connectors.ConnectorError as exc:
        account.last_error = str(exc)[:500]
        from filenergy import db
        db.session.commit()
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("connectors.index"))
    events.log_event(
        "connector.synced",
        user=g.user, workspace_id=g.workspace.id,
        kind=account.kind, **result,
    )
    flash(f"Synced — {result['created']} new, {result['skipped']} skipped.", "success")
    return redirect(url_for("connectors.index"))


@connectors_bp.route("/accounts/<int:account_id>/disconnect", methods=["POST"])
@login_required
def disconnect(account_id):
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403
    account = ConnectorAccount.query.filter_by(
        id=account_id, workspace_id=g.workspace.id
    ).first()
    if account is None:
        abort(404)
    connectors.disconnect(account)
    return redirect(url_for("connectors.index"))
