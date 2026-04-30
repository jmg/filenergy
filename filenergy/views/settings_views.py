"""User-facing settings: profile, API keys, workspace, billing."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import login_required

from filenergy import settings as cfg
from filenergy.services import api_keys, billing, events, webhooks, workspaces

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/")
@login_required
def index():
    return redirect(url_for("settings.profile"))


@settings_bp.route("/profile")
@login_required
def profile():
    return render_template("settings/profile.html")


@settings_bp.route("/profile", methods=["POST"])
@login_required
def update_profile():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not g.user.check_password(current):
        flash("Current password is wrong", "error")
        return redirect(url_for("settings.profile"))
    if new != confirm:
        flash("New passwords don't match", "error")
        return redirect(url_for("settings.profile"))
    g.user.set_password(new)
    from filenergy import db
    db.session.commit()
    flash("Password updated", "success")
    return redirect(url_for("settings.profile"))


@settings_bp.route("/keys")
@login_required
def keys():
    return render_template(
        "settings/api_keys.html",
        keys=api_keys.list_for_workspace(g.workspace),
    )


@settings_bp.route("/keys", methods=["POST"])
@login_required
def create_key():
    name = request.form.get("name", "").strip() or "Untitled"
    record, plaintext = api_keys.mint(g.workspace, g.user, name)
    events.log_event(
        events.API_KEY_CREATED,
        user=g.user, workspace_id=g.workspace.id, key_id=record.id, name=name,
    )
    flash(
        f"Key created. Save it now — it won't be shown again: {plaintext}",
        "success",
    )
    return redirect(url_for("settings.keys"))


@settings_bp.route("/keys/<int:key_id>/revoke", methods=["POST"])
@login_required
def revoke_key(key_id):
    if api_keys.revoke(g.workspace, key_id):
        events.log_event(
            events.API_KEY_REVOKED,
            user=g.user, workspace_id=g.workspace.id, key_id=key_id,
        )
        flash("Key revoked", "success")
    return redirect(url_for("settings.keys"))


@settings_bp.route("/workspace")
@login_required
def workspace():
    return render_template(
        "settings/workspace.html",
        members=workspaces.members(g.workspace),
        invitations=workspaces.pending_invitations(g.workspace),
        my_role=workspaces.role_of(g.workspace, g.user),
        my_workspaces=workspaces.list_for_user(g.user),
    )


@settings_bp.route("/billing")
@login_required
def billing_page():
    return render_template(
        "settings/billing.html",
        usage=billing.usage_summary(g.workspace),
        plans=cfg.PLAN_LIMITS,
        stripe_configured=billing.is_configured(),
        current_plan=g.workspace.plan or "free",
    )


@settings_bp.route("/webhooks")
@login_required
def webhooks_page():
    return render_template(
        "settings/webhooks.html",
        subscriptions=webhooks.list_for_workspace(g.workspace),
    )


@settings_bp.route("/webhooks", methods=["POST"])
@login_required
def create_webhook():
    url = (request.form.get("url") or "").strip()
    selected_events = request.form.getlist("events")
    if not url or not url.startswith(("http://", "https://")):
        flash("URL must be http(s)://...", "error")
        return redirect(url_for("settings.webhooks_page"))
    if not selected_events:
        flash("Pick at least one event type", "error")
        return redirect(url_for("settings.webhooks_page"))
    sub, secret = webhooks.create(g.workspace, url, selected_events)
    flash(
        f"Webhook created. Save its signing secret now — it won't be shown again: {secret}",
        "success",
    )
    return redirect(url_for("settings.webhooks_page"))


@settings_bp.route("/webhooks/<int:sub_id>/delete", methods=["POST"])
@login_required
def delete_webhook(sub_id):
    sub = webhooks.get(g.workspace, sub_id)
    if sub is None:
        return "Not found", 404
    webhooks.delete(sub)
    return redirect(url_for("settings.webhooks_page"))


@settings_bp.route("/webhooks/<int:sub_id>/toggle", methods=["POST"])
@login_required
def toggle_webhook(sub_id):
    sub = webhooks.get(g.workspace, sub_id)
    if sub is None:
        return "Not found", 404
    webhooks.set_enabled(sub, not sub.enabled)
    return redirect(url_for("settings.webhooks_page"))


@settings_bp.route("/billing/checkout", methods=["POST"])
@login_required
def billing_checkout():
    if not workspaces.require_role(g.workspace, g.user, "owner"):
        return "Forbidden", 403
    plan = request.form.get("plan", "")
    try:
        url = billing.create_checkout_session(g.workspace, plan)
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings.billing_page"))
    events.log_event(
        events.BILLING_CHECKOUT_STARTED,
        user=g.user, workspace_id=g.workspace.id, plan=plan,
    )
    return redirect(url)
