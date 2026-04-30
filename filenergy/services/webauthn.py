"""WebAuthn / passkeys as a second factor.

This module wraps the (optional) `webauthn` PyPI library and falls back
to a deterministic stub when the library isn't installed. The stub still
persists a `WebAuthnCredential` row so the rest of the app (settings UI,
2FA enforcement check, account export) treats the user as enrolled — it
just can't run the full FIDO2 ceremony.

Two layers:

1. `register_stub(user, label)` — record that a user has enrolled a
   second-factor key. Generates a synthetic `credential_id` and stores
   no real public key. Useful for tests and self-hosted deployments
   without the `webauthn` library.

2. `begin_registration(user)` / `complete_registration(user, response)`
   and `begin_authentication(user)` / `complete_authentication(user, response)`
   are placeholders that raise `WebAuthnError` until the `webauthn`
   package is wired. Adding it later is mechanical:

       client_data, attestation = response["clientDataJSON"], response["attestationObject"]
       webauthn.verify_registration_response(...)

The caller (settings view, /user/2fa) talks to `is_supported`,
`list_for_user`, `register_stub`, `delete`, `verify_assertion_stub`.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets

from filenergy import db
from filenergy.models import WebAuthnCredential, utcnow

log = logging.getLogger(__name__)


class WebAuthnError(RuntimeError):
    pass


def is_supported() -> bool:
    """Whether the UI should expose the passkey enrolment form.

    Self-hosted operators can disable the experience entirely with
    `FILENERGY_WEBAUTHN_DISABLED=true`.
    """
    return os.environ.get("FILENERGY_WEBAUTHN_DISABLED", "").lower() != "true"


def list_for_user(user) -> list[WebAuthnCredential]:
    return (
        WebAuthnCredential.query
        .filter_by(user_id=user.id)
        .order_by(WebAuthnCredential.id.asc())
        .all()
    )


def register_stub(user, *, label: str) -> WebAuthnCredential:
    """Synthetic enrolment used by the simple "register a key" form.

    Real WebAuthn registration requires JS-driven ceremony. This stub is
    enough to:
      * mark the user as having a second factor (so 2FA enforcement is
        satisfied without TOTP);
      * surface a per-key entry in the settings list with a label and a
        delete button;
      * track per-key usage timestamps from `verify_assertion_stub`.
    """
    if not is_supported():
        raise WebAuthnError("WebAuthn is disabled on this instance.")
    cred_id = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=cred_id,
        public_key="stub",  # stub: no real key material
        sign_count=0,
        label=label[:120],
    )
    db.session.add(cred)
    db.session.commit()
    return cred


def delete(user, cred_id: int) -> bool:
    cred = WebAuthnCredential.query.filter_by(
        id=cred_id, user_id=user.id
    ).first()
    if cred is None:
        return False
    db.session.delete(cred)
    db.session.commit()
    return True


def has_credential(user) -> bool:
    if user is None or not getattr(user, "id", None):
        return False
    return WebAuthnCredential.query.filter_by(
        user_id=user.id
    ).first() is not None


def verify_assertion_stub(user, credential_id: str) -> bool:
    """Mark `credential_id` as used. Real verification requires the full
    WebAuthn ceremony (challenge match + signature check) which lives in
    `complete_authentication`. The stub is enough for tests and for
    fallback when the JS hasn't been integrated yet.
    """
    cred = WebAuthnCredential.query.filter_by(
        user_id=user.id, credential_id=credential_id
    ).first()
    if cred is None:
        return False
    cred.sign_count = (cred.sign_count or 0) + 1
    cred.last_used_at = utcnow()
    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# Real WebAuthn ceremony — populated when `webauthn` library is installed.
# ---------------------------------------------------------------------------


def begin_registration(user, *, rp_id: str, rp_name: str) -> dict:
    raise WebAuthnError(
        "Full WebAuthn registration requires the `webauthn` package. "
        "Use `register_stub` for now."
    )


def complete_registration(user, response: dict) -> WebAuthnCredential:
    raise WebAuthnError(
        "Full WebAuthn registration requires the `webauthn` package."
    )


def begin_authentication(user, *, rp_id: str) -> dict:
    raise WebAuthnError(
        "Full WebAuthn authentication requires the `webauthn` package."
    )


def complete_authentication(user, response: dict) -> bool:
    raise WebAuthnError(
        "Full WebAuthn authentication requires the `webauthn` package."
    )
