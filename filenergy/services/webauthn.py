"""WebAuthn / passkeys as a second factor.

Three layers:

1. **Stub** (`register_stub`, `verify_assertion_stub`) — synthetic enrol
   that records a `WebAuthnCredential` row without doing the FIDO2
   ceremony. Useful for tests, deployments without the `webauthn`
   package, and as a fallback at /user/2fa when the JS hasn't run.

2. **Full ceremony** (`begin_registration`, `complete_registration`,
   `begin_authentication`, `complete_authentication`) — drives the
   `py_webauthn` library. The challenge stays in the Flask session so
   the response that comes back from the browser can be verified
   against it. Public key + sign counter persist on the credential row.

3. **Helpers** (`is_supported`, `list_for_user`, `delete`,
   `has_credential`) — boring DB lookups consumed by settings views,
   2FA enforcement middleware, and account export.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from typing import Any

from flask import session

from filenergy import db
from filenergy.models import WebAuthnCredential, utcnow

log = logging.getLogger(__name__)


# Session keys for the (short-lived) challenge values.
_REG_CHALLENGE_KEY = "webauthn_reg_challenge"
_REG_USER_HANDLE_KEY = "webauthn_reg_user_handle"
_AUTH_CHALLENGE_KEY = "webauthn_auth_challenge"


class WebAuthnError(RuntimeError):
    pass


def _has_lib() -> bool:
    try:
        import webauthn  # noqa: F401
        return True
    except ImportError:
        return False


def is_supported() -> bool:
    """Whether the UI should expose the passkey enrolment form.

    Self-hosted operators can disable the experience entirely with
    `FILENERGY_WEBAUTHN_DISABLED=true`. The stub flow works without
    `py_webauthn` installed; only the full ceremony requires it.
    """
    return os.environ.get("FILENERGY_WEBAUTHN_DISABLED", "").lower() != "true"


def list_for_user(user) -> list[WebAuthnCredential]:
    return (
        WebAuthnCredential.query
        .filter_by(user_id=user.id)
        .order_by(WebAuthnCredential.id.asc())
        .all()
    )


def has_credential(user) -> bool:
    if user is None or not getattr(user, "id", None):
        return False
    return WebAuthnCredential.query.filter_by(
        user_id=user.id
    ).first() is not None


def delete(user, cred_id: int) -> bool:
    cred = WebAuthnCredential.query.filter_by(
        id=cred_id, user_id=user.id
    ).first()
    if cred is None:
        return False
    db.session.delete(cred)
    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# Stub flow — fast path used by tests and pre-JS deployments
# ---------------------------------------------------------------------------


def register_stub(user, *, label: str) -> WebAuthnCredential:
    """Synthetic enrolment used by the simple "register a key" form."""
    if not is_supported():
        raise WebAuthnError("WebAuthn is disabled on this instance.")
    cred_id = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=cred_id,
        public_key="stub",
        sign_count=0,
        label=label[:120],
    )
    db.session.add(cred)
    db.session.commit()
    return cred


def verify_assertion_stub(user, credential_id: str) -> bool:
    """Mark `credential_id` as used. Real verification is in
    `complete_authentication` — the stub is enough for tests / fallback.
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
# Full FIDO2 ceremony — wraps py_webauthn.
# ---------------------------------------------------------------------------


def _rp_id() -> str:
    """Relying-Party identifier. Browser ties credentials to this domain;
    must match `request.host` (sans port). Operators override per-deploy.
    """
    return os.environ.get("FILENERGY_RP_ID", "localhost")


def _rp_name() -> str:
    return os.environ.get("FILENERGY_RP_NAME", "Filenergy")


def _expected_origin() -> str:
    """Origin the browser sends in `clientDataJSON`. Defaults to https://<rp_id>
    for production; override with `FILENERGY_RP_ORIGIN` (e.g. for localhost
    development, set http://localhost:5000)."""
    explicit = os.environ.get("FILENERGY_RP_ORIGIN")
    if explicit:
        return explicit
    rp = _rp_id()
    if rp == "localhost":
        return "http://localhost:5000"
    return f"https://{rp}"


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - (len(s) % 4)
    if pad != 4:
        s = s + ("=" * pad)
    return base64.urlsafe_b64decode(s)


def begin_registration(user) -> dict[str, Any]:
    """Start a registration ceremony. Returns the
    PublicKeyCredentialCreationOptions dict the browser passes to
    `navigator.credentials.create({ publicKey })`.

    Raises `WebAuthnError` if `py_webauthn` is missing.
    """
    if not _has_lib():
        raise WebAuthnError(
            "py_webauthn is not installed. Use register_stub instead."
        )
    if not is_supported():
        raise WebAuthnError("WebAuthn is disabled on this instance.")

    import webauthn as wa

    user_handle = secrets.token_bytes(32)
    challenge = secrets.token_bytes(32)
    options = wa.generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_rp_name(),
        user_id=user_handle,
        user_name=user.email or user.username or str(user.id),
        user_display_name=user.email or user.username or "",
        challenge=challenge,
    )
    session[_REG_CHALLENGE_KEY] = _b64url_encode(challenge)
    session[_REG_USER_HANDLE_KEY] = _b64url_encode(user_handle)

    return json.loads(wa.options_to_json(options))


def complete_registration(
    user, *, response: dict[str, Any], label: str = "Security key",
) -> WebAuthnCredential:
    """Finish a registration ceremony.

    `response` is the JSON body from
    `navigator.credentials.create(...).response`. We verify it against the
    challenge we stashed in `begin_registration`, then persist the
    credential id + public key + sign counter.
    """
    if not _has_lib():
        raise WebAuthnError("py_webauthn is not installed.")
    challenge_b64 = session.pop(_REG_CHALLENGE_KEY, None)
    session.pop(_REG_USER_HANDLE_KEY, None)
    if not challenge_b64:
        raise WebAuthnError("No registration challenge in session.")

    import webauthn as wa
    try:
        verified = wa.verify_registration_response(
            credential=response,
            expected_challenge=_b64url_decode(challenge_b64),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
        )
    except Exception as exc:
        raise WebAuthnError(f"Registration verification failed: {exc}") from exc

    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=_b64url_encode(verified.credential_id),
        public_key=_b64url_encode(verified.credential_public_key),
        sign_count=verified.sign_count,
        label=label[:120],
    )
    db.session.add(cred)
    db.session.commit()
    return cred


def begin_authentication(user) -> dict[str, Any]:
    """Start an authentication ceremony for `user`. Returns the
    PublicKeyCredentialRequestOptions dict for `navigator.credentials.get`.
    """
    if not _has_lib():
        raise WebAuthnError("py_webauthn is not installed.")

    import webauthn as wa
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor

    creds = list_for_user(user)
    if not creds:
        raise WebAuthnError("User has no registered credentials.")

    challenge = secrets.token_bytes(32)
    allow_credentials = [
        PublicKeyCredentialDescriptor(id=_b64url_decode(c.credential_id))
        for c in creds
    ]
    options = wa.generate_authentication_options(
        rp_id=_rp_id(),
        challenge=challenge,
        allow_credentials=allow_credentials,
    )
    session[_AUTH_CHALLENGE_KEY] = _b64url_encode(challenge)
    return json.loads(wa.options_to_json(options))


def complete_authentication(user, *, response: dict[str, Any]) -> bool:
    """Finish an authentication ceremony.

    Returns True when the assertion verifies against the stored public key
    and the sign counter is monotonically increasing. Updates the row's
    sign counter and `last_used_at` on success.
    """
    if not _has_lib():
        raise WebAuthnError("py_webauthn is not installed.")
    challenge_b64 = session.pop(_AUTH_CHALLENGE_KEY, None)
    if not challenge_b64:
        raise WebAuthnError("No authentication challenge in session.")

    raw_id_b64 = response.get("id") or response.get("rawId")
    if not raw_id_b64:
        return False
    cred = WebAuthnCredential.query.filter_by(
        user_id=user.id, credential_id=raw_id_b64,
    ).first()
    if cred is None or cred.public_key == "stub":
        return False

    import webauthn as wa
    try:
        verified = wa.verify_authentication_response(
            credential=response,
            expected_challenge=_b64url_decode(challenge_b64),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origin(),
            credential_public_key=_b64url_decode(cred.public_key),
            credential_current_sign_count=cred.sign_count or 0,
        )
    except Exception as exc:
        log.info("WebAuthn authentication failed: %s", exc)
        return False

    cred.sign_count = verified.new_sign_count
    cred.last_used_at = utcnow()
    db.session.commit()
    return True
