"""SAML login + ACS endpoints. Stub for now."""
from flask import Blueprint, jsonify

from filenergy.services import saml_sso

saml_bp = Blueprint("saml", __name__)


@saml_bp.route("/status")
def status():
    return jsonify(saml_sso.status())


@saml_bp.route("/login")
def login():
    if not saml_sso.is_configured():
        return jsonify(error="SAML SSO is not configured"), 503
    try:
        target = saml_sso.init_request(redirect_uri="")
    except NotImplementedError as exc:
        return jsonify(error=str(exc)), 501
    from flask import redirect
    return redirect(target)


@saml_bp.route("/acs", methods=["POST"])
def acs():
    """SAML Assertion Consumer Service."""
    if not saml_sso.is_configured():
        return jsonify(error="SAML SSO is not configured"), 503
    return jsonify(error="SAML processing is a stub on this build"), 501
