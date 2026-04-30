"""SAML login + ACS endpoints."""
from flask import Blueprint, flash, jsonify, redirect, request, url_for

from filenergy.services import events, saml_sso

saml_bp = Blueprint("saml", __name__)


@saml_bp.route("/status")
def status():
    return jsonify(saml_sso.status())


@saml_bp.route("/login")
def login():
    if not saml_sso.is_configured():
        return jsonify(error="SAML SSO is not configured"), 503
    try:
        target = saml_sso.init_request(
            redirect_uri=url_for("index.index", _external=True)
        )
    except saml_sso.SAMLError as exc:
        return jsonify(error=str(exc)), 503
    return redirect(target)


@saml_bp.route("/acs", methods=["POST"])
def acs():
    if not saml_sso.is_configured():
        return jsonify(error="SAML SSO is not configured"), 503
    try:
        user = saml_sso.process_response(request.form.to_dict())
    except saml_sso.SAMLError as exc:
        flash(f"SAML login failed: {exc}", "error")
        return redirect(url_for("user.login"))
    events.log_event(events.USER_LOGGED_IN, user=user, via="saml")
    return redirect(url_for("index.index"))
