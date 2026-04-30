"""Workspace switcher, member admin, and invitation flow."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import login_required

from filenergy.services import events, workspaces

workspace_bp = Blueprint("workspace", __name__)


@workspace_bp.route("/switch/<int:workspace_id>", methods=["POST"])
@login_required
def switch(workspace_id):
    ws = workspaces.set_current(g.user, workspace_id)
    if ws is None:
        return "Forbidden", 403
    events.log_event(events.WORKSPACE_SWITCHED, user=g.user, workspace_id=ws.id)
    return redirect(request.form.get("next") or url_for("index.index"))


@workspace_bp.route("/new", methods=["POST"])
@login_required
def create():
    name = request.form.get("name", "").strip()
    ws = workspaces.create(g.user, name=name)
    workspaces.set_current(g.user, ws.id)
    events.log_event(events.WORKSPACE_CREATED, user=g.user, workspace_id=ws.id)
    return redirect(url_for("settings.workspace"))


@workspace_bp.route("/invite", methods=["POST"])
@login_required
def invite():
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403
    email = request.form.get("email", "").strip()
    role = request.form.get("role", "member")
    if not email:
        flash("Email is required", "error")
        return redirect(url_for("settings.workspace"))
    inv = workspaces.invite(g.workspace, g.user, email, role)
    events.log_event(
        events.WORKSPACE_INVITED,
        user=g.user, workspace_id=g.workspace.id,
        email=email, invitation_id=inv.id,
    )
    flash(f"Invitation link: {url_for('workspace.accept', token=inv.token, _external=True)}", "success")
    return redirect(url_for("settings.workspace"))


@workspace_bp.route("/accept/<token>")
@login_required
def accept(token):
    ws = workspaces.accept_invitation(token, g.user)
    if ws is None:
        abort(404)
    workspaces.set_current(g.user, ws.id)
    events.log_event(
        events.WORKSPACE_MEMBER_JOINED, user=g.user, workspace_id=ws.id
    )
    flash(f"You joined {ws.name}", "success")
    return redirect(url_for("index.index"))


@workspace_bp.route("/members/<int:user_id>/remove", methods=["POST"])
@login_required
def remove_member(user_id):
    if not workspaces.require_role(g.workspace, g.user, "owner", "admin"):
        return "Forbidden", 403
    ok = workspaces.remove_member(g.workspace, user_id)
    if ok:
        events.log_event(
            events.WORKSPACE_MEMBER_REMOVED,
            user=g.user, workspace_id=g.workspace.id, removed_user_id=user_id,
        )
    return redirect(url_for("settings.workspace"))
