"""SAML SSO scaffold.

Real SAML libraries (`python3-saml`, `pysaml2`) need libxml2/xmlsec C
libraries that are awkward in CI and Docker slim images. We expose the
SP-side hooks here so a deploy can plug in the library of its choice
without changing the views.

To enable in production, set:
    SAML_ENABLED=true
    SAML_IDP_METADATA_URL=https://idp.example.com/metadata
    SAML_SP_ENTITY_ID=https://your-domain.com/saml/metadata

then implement `init_request` / `process_response` in your fork using
your preferred library.

This stub is intentionally minimal — it returns a clear "not configured"
flag so the views render a "configure SAML" placeholder rather than 500.
"""
from __future__ import annotations

import os


def is_configured() -> bool:
    return os.environ.get("SAML_ENABLED", "").lower() == "true" and bool(
        os.environ.get("SAML_IDP_METADATA_URL")
    )


def status() -> dict:
    return {
        "enabled": is_configured(),
        "idp_metadata_url": os.environ.get("SAML_IDP_METADATA_URL", ""),
        "sp_entity_id": os.environ.get("SAML_SP_ENTITY_ID", ""),
        "implementation": (
            "Stub. Plug in python3-saml or pysaml2 and replace "
            "init_request/process_response."
        ),
    }


def init_request(*, redirect_uri: str) -> str:
    """Return the URL to redirect the browser to (the IdP's SSO endpoint)."""
    raise NotImplementedError(
        "SAML init_request is a stub. See filenergy/services/saml_sso.py."
    )


def process_response(form_data: dict) -> dict:
    """Validate the SAMLResponse POST. Return {'email': ..., 'sub': ...}."""
    raise NotImplementedError(
        "SAML process_response is a stub. See filenergy/services/saml_sso.py."
    )
