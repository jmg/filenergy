"""User-facing settings: profile, API keys, workspace, billing."""
from flask import (
    Blueprint, Response, flash, g, redirect, render_template, request, url_for,
)
from flask_login import login_required

from filenergy import db, settings as cfg
from filenergy.services import (
    api_keys, billing, events, exporting, sessions as session_service,
    totp, webauthn, webhooks, workspaces,
)

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
    scopes = request.form.getlist("scopes")
    record, plaintext = api_keys.mint(
        g.workspace, g.user, name, scopes=scopes,
    )
    events.log_event(
        events.API_KEY_CREATED,
        user=g.user, workspace_id=g.workspace.id, key_id=record.id, name=name,
        scopes=record.scopes,
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
    from filenergy.services import inbound_email
    return render_template(
        "settings/workspace.html",
        members=workspaces.members(g.workspace),
        invitations=workspaces.pending_invitations(g.workspace),
        my_role=workspaces.role_of(g.workspace, g.user),
        my_workspaces=workspaces.list_for_user(g.user),
        inbound_address=inbound_email.address_for(g.workspace),
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


@settings_bp.route("/security")
@login_required
def security():
    current_session = session_service.current()
    return render_template(
        "settings/security.html",
        totp_enabled=g.user.totp_enabled,
        has_pending_setup=bool(g.user.totp_secret) and not g.user.totp_enabled,
        active_sessions=session_service.list_active(g.user),
        current_session_id=current_session.id if current_session else None,
        webauthn_supported=webauthn.is_supported(),
        webauthn_credentials=webauthn.list_for_user(g.user),
    )


@settings_bp.route("/security/webauthn", methods=["POST"])
@login_required
def webauthn_register():
    label = (request.form.get("label") or "").strip() or "Security key"
    try:
        cred = webauthn.register_stub(g.user, label=label)
    except webauthn.WebAuthnError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings.security"))
    events.log_event(
        events.WEBAUTHN_REGISTERED, user=g.user, credential_id=cred.id,
    )
    flash("Passkey registered.", "success")
    return redirect(url_for("settings.security"))


@settings_bp.route("/security/webauthn/begin", methods=["POST"])
@login_required
def webauthn_begin():
    """Start a real FIDO2 registration ceremony — returns the
    PublicKeyCredentialCreationOptions dict for `navigator.credentials.create`."""
    from flask import jsonify
    try:
        options = webauthn.begin_registration(g.user)
    except webauthn.WebAuthnError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(options)


@settings_bp.route("/security/webauthn/complete", methods=["POST"])
@login_required
def webauthn_complete():
    """Finish a real FIDO2 registration ceremony."""
    from flask import jsonify
    payload = request.get_json(silent=True) or {}
    label = (payload.get("label") or "Security key").strip()[:120]
    response = payload.get("response") or {}
    try:
        cred = webauthn.complete_registration(
            g.user, response=response, label=label,
        )
    except webauthn.WebAuthnError as exc:
        return jsonify({"error": str(exc)}), 400
    events.log_event(
        events.WEBAUTHN_REGISTERED, user=g.user, credential_id=cred.id,
    )
    return jsonify({"id": cred.id, "label": cred.label})


@settings_bp.route("/security/webauthn/<int:cred_id>/delete", methods=["POST"])
@login_required
def webauthn_delete(cred_id):
    if webauthn.delete(g.user, cred_id):
        events.log_event(
            events.WEBAUTHN_REMOVED, user=g.user, credential_id=cred_id,
        )
        flash("Passkey removed.", "success")
    return redirect(url_for("settings.security"))


@settings_bp.route("/notifications", methods=["POST"])
@login_required
def update_notifications():
    g.user.weekly_digest = bool(request.form.get("weekly_digest"))
    db.session.commit()
    flash("Notification preferences saved.", "success")
    return redirect(url_for("settings.workspace"))


@settings_bp.route("/account/export")
@login_required
def export_account():
    payload = exporting.user_zip(g.user)
    events.log_event(
        events.USER_EXPORTED, user=g.user, bytes=len(payload),
    )
    return Response(
        payload,
        mimetype="application/zip",
        headers={
            "Content-Disposition":
                f'attachment; filename="filenergy-{g.user.id}.zip"',
        },
    )


@settings_bp.route("/security/sessions/<int:session_id>/revoke", methods=["POST"])
@login_required
def revoke_session(session_id):
    if session_service.revoke(g.user, session_id):
        events.log_event(
            events.USER_SESSION_REVOKED, user=g.user, session_id=session_id,
        )
        flash("Session revoked.", "success")
    return redirect(url_for("settings.security"))


@settings_bp.route("/security/sessions/revoke-others", methods=["POST"])
@login_required
def revoke_other_sessions():
    n = session_service.revoke_all_others(g.user)
    events.log_event(
        events.USER_SESSION_REVOKED, user=g.user, count=n, scope="others",
    )
    flash(f"Logged out {n} other session{'s' if n != 1 else ''}.", "success")
    return redirect(url_for("settings.security"))


@settings_bp.route("/security/totp/start", methods=["POST"])
@login_required
def totp_start():
    try:
        uri = totp.start_setup(g.user)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings.security"))
    qr = totp.qr_svg(uri)
    return render_template(
        "settings/security_setup.html", otpauth_uri=uri, qr_svg=qr
    )


@settings_bp.route("/security/totp/enable", methods=["POST"])
@login_required
def totp_enable():
    code = (request.form.get("code") or "").strip()
    if not totp.enable(g.user, code):
        flash("Invalid code. Try again.", "error")
        return redirect(url_for("settings.totp_start"))
    codes = totp.regenerate_recovery_codes(g.user)
    flash(
        "2FA is on. Save these recovery codes — each is single-use:\n"
        + " ".join(codes),
        "success",
    )
    return redirect(url_for("settings.security"))


@settings_bp.route("/security/totp/disable", methods=["POST"])
@login_required
def totp_disable():
    totp.disable(g.user)
    flash("2FA disabled.", "success")
    return redirect(url_for("settings.security"))


@settings_bp.route("/security/totp/recover", methods=["POST"])
@login_required
def totp_regenerate():
    try:
        codes = totp.regenerate_recovery_codes(g.user)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings.security"))
    flash("New codes: " + " ".join(codes), "success")
    return redirect(url_for("settings.security"))


@settings_bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    """GDPR self-serve account deletion.

    Wipes the user, every workspace they own (and all its files), and any
    membership rows. This is irreversible.
    """
    if (request.form.get("confirm") or "").strip() != g.user.email:
        flash("Type your email to confirm.", "error")
        return redirect(url_for("settings.security"))

    from filenergy.models import (
        ApiKey, Conversation, Event, User, Workspace, WorkspaceMember,
    )
    from flask_login import logout_user

    user_id = g.user.id
    user_row = User.query.get(user_id)

    # Workspaces the user owns are wiped (cascades to files, conversations, etc.).
    for w in Workspace.query.filter_by(owner_id=user_id).all():
        db.session.delete(w)
    # Membership rows in other workspaces.
    WorkspaceMember.query.filter_by(user_id=user_id).delete()
    # Personal artifacts not under any workspace.
    Conversation.query.filter_by(user_id=user_id).delete()
    ApiKey.query.filter_by(user_id=user_id).delete()
    # Anonymize events instead of deleting (audit trail).
    Event.query.filter_by(user_id=user_id).update({Event.user_id: None})
    db.session.commit()

    logout_user()
    db.session.delete(user_row)
    db.session.commit()
    events.log_event(
        "user.deleted", workspace_id=None, deleted_user_id=user_id,
    )
    flash("Account deleted.", "success")
    return redirect(url_for("index.index"))


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
