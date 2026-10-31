import secrets

from flask import (
    Blueprint, flash, g, redirect, render_template, request, session, url_for,
)
from flask_login import login_user

from filenergy.models import User
from filenergy.services import (
    events, oauth, sessions as session_service, totp, webauthn,
)
from filenergy.services.user import UserService

user_bp = Blueprint("user", __name__)


PENDING_2FA_KEY = "pending_2fa_user_id"
PENDING_2FA_NEXT = "pending_2fa_next"
# Random id of this pending login. Wrong codes are counted server-side
# (events table) against it: the session is a cookie the client could
# replay, so a counter kept in it would reset at will.
PENDING_2FA_ATTEMPT = "pending_2fa_attempt"


def _clear_pending_2fa():
    for key in (PENDING_2FA_KEY, PENDING_2FA_NEXT, PENDING_2FA_ATTEMPT):
        session.pop(key, None)


def _two_factor_locked(user, attempt_id) -> bool:
    """Too many wrong codes for this pending login, or for the account."""
    from filenergy import settings as cfg

    per_login = events.count_recent_with_metadata(
        events.USER_2FA_FAILED, 24 * 3600, attempt=attempt_id,
    )
    if per_login >= cfg.TWO_FACTOR_MAX_ATTEMPTS:
        return True
    per_account = events.count_recent(
        user, events.USER_2FA_FAILED, cfg.TWO_FACTOR_ACCOUNT_WINDOW_SECONDS,
    )
    return per_account >= cfg.TWO_FACTOR_ACCOUNT_LIMIT


@user_bp.route("/login/")
def login():
    return render_template(
        "user/login.html",
        next=request.args.get("next"),
        google_enabled=oauth.is_configured(),
    )


@user_bp.route("/login/", methods=["POST"])
def login_post():
    from filenergy import settings as cfg

    email = request.form["email"].strip().lower()
    password = request.form["password"].strip()
    next_ = request.form.get("next") or url_for("index.index")

    # Bucket failed-login attempts per email so a single attacker can't
    # iterate through password lists. The bucket is the email being
    # attempted, not the actual user — keeps anonymous attackers out.
    recent_failures = events.count_recent_with_metadata(
        events.USER_LOGIN_FAILED,
        cfg.LOGIN_RATE_WINDOW_SECONDS,
        email=email,
    )
    if recent_failures >= cfg.LOGIN_RATE_LIMIT:
        events.log_event(events.USER_LOGIN_RATE_LIMITED, email=email)
        flash(
            "Too many failed attempts. Try again in a few minutes.",
            "error",
        )
        return redirect(url_for("user.login"))

    user = User.query.filter_by(email=email).first()
    if user is None or not user.check_password(password):
        events.log_event(events.USER_LOGIN_FAILED, email=email)
        flash("Email or password incorrect.", "error")
        return redirect(url_for("user.login"))

    if user.totp_enabled or webauthn.has_credential(user):
        # Defer flask-login until the second factor succeeds.
        session[PENDING_2FA_KEY] = user.id
        session[PENDING_2FA_NEXT] = next_
        session[PENDING_2FA_ATTEMPT] = secrets.token_urlsafe(16)
        return redirect(url_for("user.two_factor"))

    # No 2FA: log in directly.
    UserService().login(email, password)
    session_service.issue(user)
    events.log_event(events.USER_LOGGED_IN, user=user)
    return redirect(next_)


@user_bp.route("/2fa")
def two_factor():
    if not session.get(PENDING_2FA_KEY):
        return redirect(url_for("user.login"))
    user_id = session.get(PENDING_2FA_KEY)
    user = User.query.get(user_id) if user_id else None
    has_passkey = bool(user and webauthn.has_credential(user))
    return render_template(
        "user/two_factor.html",
        webauthn_enabled=has_passkey,
    )


@user_bp.route("/2fa/webauthn/begin", methods=["POST"])
def two_factor_webauthn_begin():
    from flask import jsonify
    user_id = session.get(PENDING_2FA_KEY)
    if not user_id:
        return jsonify({"error": "no pending login"}), 400
    user = User.query.get(user_id)
    if user is None:
        return jsonify({"error": "no pending login"}), 400
    try:
        options = webauthn.begin_authentication(user)
    except webauthn.WebAuthnError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(options)


@user_bp.route("/2fa/webauthn/complete", methods=["POST"])
def two_factor_webauthn_complete():
    from flask import jsonify
    user_id = session.get(PENDING_2FA_KEY)
    if not user_id:
        return jsonify({"error": "no pending login"}), 400
    user = User.query.get(user_id)
    if user is None:
        return jsonify({"error": "no pending login"}), 400
    payload = request.get_json(silent=True) or {}
    response = payload.get("response") or {}
    try:
        ok = webauthn.complete_authentication(user, response=response)
    except webauthn.WebAuthnError as exc:
        return jsonify({"error": str(exc)}), 400
    if not ok:
        return jsonify({"error": "verification failed"}), 400

    next_ = session.pop(PENDING_2FA_NEXT, None) or url_for("index.index")
    _clear_pending_2fa()
    login_user(user)
    from filenergy.services import workspaces
    workspaces.ensure_default_for(user)
    session_service.issue(user)
    events.log_event(events.USER_LOGGED_IN, user=user, via="webauthn")
    events.log_event(events.WEBAUTHN_VERIFIED, user=user)
    return jsonify({"ok": True, "next": next_})


@user_bp.route("/2fa", methods=["POST"])
def two_factor_post():
    user_id = session.get(PENDING_2FA_KEY)
    attempt_id = session.get(PENDING_2FA_ATTEMPT)
    if not user_id or not attempt_id:
        _clear_pending_2fa()
        return redirect(url_for("user.login"))
    user = User.query.get(user_id)
    if user is None:
        _clear_pending_2fa()
        return redirect(url_for("user.login"))

    if _two_factor_locked(user, attempt_id):
        _clear_pending_2fa()
        flash("Too many invalid codes. Sign in again in a few minutes.", "error")
        return redirect(url_for("user.login"))

    # TOTP or a recovery code only. Passkeys go through
    # /2fa/webauthn/complete, which checks the signed assertion; a bare
    # `webauthn_credential_id` here used to be accepted on sight, and
    # /2fa/webauthn/begin gives that id to whoever has the password.
    code = (request.form.get("code") or "").strip()
    ok = bool(code) and (
        totp.verify_otp(user, code)
        or totp.consume_recovery_code(user, code)
    )
    if not ok:
        events.log_event(events.USER_2FA_FAILED, user=user, attempt=attempt_id)
        if _two_factor_locked(user, attempt_id):
            # The pending login is spent: the password has to be typed again.
            _clear_pending_2fa()
            events.log_event(events.USER_2FA_LOCKED, user=user)
            flash("Too many invalid codes. Sign in again in a few minutes.", "error")
            return redirect(url_for("user.login"))
        flash("Invalid code", "error")
        return redirect(url_for("user.two_factor"))

    next_ = session.pop(PENDING_2FA_NEXT, None) or url_for("index.index")
    _clear_pending_2fa()
    login_user(user)
    from filenergy.services import workspaces
    workspaces.ensure_default_for(user)
    session_service.issue(user)
    events.log_event(events.USER_LOGGED_IN, user=user)
    return redirect(next_)


@user_bp.route("/register/")
def register():
    return render_template(
        "user/register.html", google_enabled=oauth.is_configured()
    )


@user_bp.route("/register/", methods=["POST"])
def register_post():
    email = request.form["email"].strip()
    password = request.form["password"].strip()
    password_again = request.form["password_again"].strip()

    user_svc = UserService()
    error = user_svc.register(email, password, password_again)
    if error:
        flash(error, "error")
        return redirect(url_for("user.register"))

    user = user_svc.get_one(email=email)
    events.log_event(events.USER_REGISTERED, user=user)

    return redirect(url_for("onboarding.index"))


@user_bp.route("/logout/")
def logout():
    user = g.user if g.user.is_authenticated else None
    session_service.revoke_on_logout()
    UserService().logout()
    if user is not None:
        events.log_event(events.USER_LOGGED_OUT, user=user)
    return redirect(url_for("index.index"))


# ---- Google OAuth ----


@user_bp.route("/oauth/google/login")
def oauth_google_login():
    if not oauth.is_configured():
        flash("Google sign-in is not configured.", "error")
        return redirect(url_for("user.login"))
    redirect_uri = url_for("user.oauth_google_callback", _external=True)
    return oauth.login_redirect(redirect_uri)


@user_bp.route("/oauth/google/callback")
def oauth_google_callback():
    if not oauth.is_configured():
        return redirect(url_for("user.login"))
    try:
        user = oauth.consume_callback()
    except Exception as exc:
        flash(f"Google sign-in failed: {exc}", "error")
        return redirect(url_for("user.login"))
    events.log_event(events.USER_LOGGED_IN, user=user, via="google_oauth")
    return redirect(url_for("index.index"))
