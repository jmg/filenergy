"""SAML SSO via python3-saml.

Activated by env vars:
    SAML_ENABLED=true
    SAML_IDP_METADATA_URL=https://idp.example.com/metadata.xml
    SAML_SP_ENTITY_ID=https://your-domain.com/saml/metadata
    SAML_SP_ACS_URL=https://your-domain.com/saml/acs
    SAML_SP_X509_CERT=...   (optional, base64-encoded)
    SAML_SP_PRIVATE_KEY=... (optional, base64-encoded)

Provisioning: every successful SAML login creates a User + default
workspace if the email is new; otherwise links by email. python3-saml
needs `libxml2` and `libxmlsec1` system libraries — the Filenergy
Dockerfile installs them.
"""
from __future__ import annotations

import logging
import os
import urllib.request
from xml.etree import ElementTree as ET

from flask import request as flask_request
from flask_login import login_user

from filenergy import db
from filenergy.models import User
from filenergy.services import workspaces

log = logging.getLogger(__name__)


class SAMLError(RuntimeError):
    pass


def is_configured() -> bool:
    return os.environ.get("SAML_ENABLED", "").lower() == "true" and bool(
        os.environ.get("SAML_IDP_METADATA_URL")
    )


def status() -> dict:
    return {
        "enabled": is_configured(),
        "idp_metadata_url": os.environ.get("SAML_IDP_METADATA_URL", ""),
        "sp_entity_id": os.environ.get("SAML_SP_ENTITY_ID", ""),
        "library_available": _has_lib(),
    }


def _has_lib() -> bool:
    try:
        import onelogin.saml2.auth  # noqa: F401
        return True
    except ImportError:
        return False


def _build_settings() -> dict:
    """Compose a python3-saml settings dict from env + IdP metadata fetch."""
    metadata_url = os.environ["SAML_IDP_METADATA_URL"]
    idp = _fetch_idp_metadata(metadata_url)
    sp = {
        "entityId": os.environ.get("SAML_SP_ENTITY_ID", ""),
        "assertionConsumerService": {
            "url": os.environ.get("SAML_SP_ACS_URL", ""),
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
        },
        "x509cert": os.environ.get("SAML_SP_X509_CERT", ""),
        "privateKey": os.environ.get("SAML_SP_PRIVATE_KEY", ""),
        "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    }
    return {"strict": True, "debug": False, "sp": sp, "idp": idp}


def _fetch_idp_metadata(url: str) -> dict:
    """Best-effort IdP metadata parse — entityID, SSO URL, signing cert.

    We do this with stdlib XML so a restart isn't required when the
    metadata changes; python3-saml's built-in parser would also work
    but requires the full SAMLToolkit context.
    """
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            xml_bytes = resp.read()
    except Exception as exc:
        raise SAMLError(f"Couldn't fetch IdP metadata: {exc}") from exc
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise SAMLError(f"IdP metadata is not valid XML: {exc}") from exc
    ns = {
        "md": "urn:oasis:names:tc:SAML:2.0:metadata",
        "ds": "http://www.w3.org/2000/09/xmldsig#",
    }
    entity_id = root.attrib.get("entityID", "")
    sso = root.find(".//md:SingleSignOnService", ns)
    sso_url = sso.attrib.get("Location", "") if sso is not None else ""
    cert_node = root.find(".//md:KeyDescriptor//ds:X509Certificate", ns)
    cert = (cert_node.text or "").strip() if cert_node is not None else ""
    return {
        "entityId": entity_id,
        "singleSignOnService": {
            "url": sso_url,
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
        },
        "x509cert": cert,
    }


def _new_auth(req: dict):
    """Construct the OneLogin auth helper. Raises SAMLError if missing."""
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError as exc:
        raise SAMLError(
            "python3-saml not installed (libxml2 + xmlsec required)"
        ) from exc
    return OneLogin_Saml2_Auth(req, _build_settings())


def _flask_request_dict() -> dict:
    """Map Flask's request into python3-saml's expected dict shape."""
    return {
        "https": "on" if flask_request.is_secure else "off",
        "http_host": flask_request.host,
        "server_port": str(flask_request.environ.get("SERVER_PORT", "")),
        "script_name": flask_request.path,
        "get_data": flask_request.args.to_dict(),
        "post_data": flask_request.form.to_dict(),
    }


# -- Public entry points (called by views/saml.py) --


def init_request(*, redirect_uri: str = "") -> str:
    """Build the IdP redirect URL for browser login."""
    if not is_configured():
        raise SAMLError("SAML SSO is not configured")
    auth = _new_auth(_flask_request_dict())
    return auth.login(return_to=redirect_uri or None)


def process_response(form_data: dict | None = None) -> User:
    """Validate the SAMLResponse POST, create/link the user, log them in."""
    if not is_configured():
        raise SAMLError("SAML SSO is not configured")
    auth = _new_auth(_flask_request_dict())
    auth.process_response()
    errors = auth.get_errors() or []
    if errors:
        raise SAMLError("SAML response invalid: " + ", ".join(errors))
    if not auth.is_authenticated():
        raise SAMLError("SAML response did not authenticate")

    name_id = (auth.get_nameid() or "").strip().lower()
    attrs = auth.get_attributes() or {}
    email = name_id
    if not email:
        # Fallback to mail / emailAddress claims.
        email = (
            (attrs.get("email") or attrs.get("emailaddress") or [""])[0]
        ).strip().lower()
    if not email:
        raise SAMLError("SAML response is missing an email identifier")

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email, username=email)
        db.session.add(user)
        db.session.commit()
    workspaces.ensure_default_for(user)
    login_user(user)
    return user
