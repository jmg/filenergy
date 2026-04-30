"""Workspace switcher, member admin, and invitation flow."""
from flask import Blueprint, abort, flash, g, redirect, request, url_for
from flask_login import login_required

from filenergy.services import email as email_service
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
    addr = request.form.get("email", "").strip()
    role = request.form.get("role", "member")
    if not addr:
        flash("Email is required", "error")
        return redirect(url_for("settings.workspace"))
    inv = workspaces.invite(g.workspace, g.user, addr, role)
    accept_url = url_for("workspace.accept", token=inv.token, _external=True)
    email_service.send(
        to=addr,
        subject=f"You're invited to {g.workspace.name} on Filenergy",
        body=(
            f"{g.user.email} invited you to join {g.workspace.name}.\n\n"
            f"Click here to accept:\n{accept_url}\n\n"
            f"This link expires in 14 days."
        ),
    )
    events.log_event(
        events.WORKSPACE_INVITED,
        user=g.user, workspace_id=g.workspace.id,
        email=addr, invitation_id=inv.id,
    )
    flash(f"Invitation sent to {addr}.", "success")
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
