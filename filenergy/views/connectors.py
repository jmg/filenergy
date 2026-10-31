"""Connector OAuth + sync routes."""
import hmac
import secrets
import time

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, session, url_for,
)
from flask_login import login_required

from filenergy.models import ConnectorAccount, Workspace
from filenergy.services import connectors, events, workspaces

connectors_bp = Blueprint("connectors", __name__)

# Pending OAuth flows of this browser session: {state: {kind, workspace_id,
# user_id, exp}}. The state sent to the provider is random and single-use;
# the callback takes the workspace from here, never from the provider, and
# checks the user can still manage it.
OAUTH_STATES_KEY = "connector_oauth_states"
OAUTH_STATE_TTL = 600
_MAX_PENDING = 5


def _remember_state(kind, workspace_id, user_id) -> str:
    now = int(time.time())
    pending = {
        k: v for k, v in (session.get(OAUTH_STATES_KEY) or {}).items()
        if isinstance(v, dict) and v.get("exp", 0) > now
    }
    while len(pending) >= _MAX_PENDING:
        pending.pop(min(pending, key=lambda k: pending[k]["exp"]))
    state = secrets.token_urlsafe(32)
    pending[state] = {
        "kind": kind, "workspace_id": workspace_id,
        "user_id": user_id, "exp": now + OAUTH_STATE_TTL,
    }
    session[OAUTH_STATES_KEY] = pending
    return state


def _take_state(state):
    """Pop and return the pending flow for `state` (None if unknown/expired)."""
    pending = dict(session.get(OAUTH_STATES_KEY) or {})
    match = None
    for key in list(pending):
        if hmac.compare_digest(key.encode(), (state or "").encode()):
            match = pending.pop(key)
    session[OAUTH_STATES_KEY] = pending
    if not isinstance(match, dict) or match.get("exp", 0) <= time.time():
        return None
    return match


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
    state = _remember_state(kind, g.workspace.id, g.user.id)
    try:
        return redirect(conn.authorize_url(redirect_uri, state))
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
    pending = _take_state(state)
    if (
        pending is None
        or pending.get("kind") != kind
        or pending.get("user_id") != g.user.id
    ):
        flash("This connection link is invalid or expired. Start again.", "error")
        return redirect(url_for("connectors.index"))
    workspace = Workspace.query.get(pending.get("workspace_id"))
    if workspace is None or not workspaces.require_role(
        workspace, g.user, "owner", "admin"
    ):
        return "Forbidden", 403
    redirect_uri = url_for("connectors.callback", kind=kind, _external=True)
    try:
        conn.complete_oauth(
            code=code, workspace_id=workspace.id, redirect_uri=redirect_uri,
        )
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
