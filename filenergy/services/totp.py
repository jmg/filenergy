"""TOTP-based 2FA + one-time recovery codes.

Storage:
- `User.totp_secret` holds the base32 secret (set by `start_setup`,
  confirmed by `enable`).
- `User.totp_enabled_at` is set when the user successfully verifies the
  first OTP — this is the "enabled" flip.
- `User.recovery_codes_json` is a JSON array of SHA-256 hashes of single-use
  recovery codes.

Login flow:
1. user/login/  → password challenge (existing).
2. If `totp_enabled`, redirect to /user/2fa with the user_id stashed in the
   session as `pending_2fa_user_id` (we don't call login_user yet).
3. /user/2fa accepts a 6-digit OTP or a recovery code; on success calls
   `login_user` and clears the pending key.
"""
from __future__ import annotations

import hashlib
import json
import secrets

from filenergy import db
from filenergy.models import User, utcnow


ISSUER = "Filenergy"


class TOTPUnavailable(RuntimeError):
    pass


def _pyotp():
    try:
        import pyotp
    except ImportError as exc:
        raise TOTPUnavailable("pyotp not installed") from exc
    return pyotp


def start_setup(user: User) -> str:
    """Provision a TOTP secret on the user and return the otpauth:// URI.

    Idempotent: if a secret is already set but TOTP is not enabled, return
    its URI. If TOTP is fully enabled, raise to force the user to disable
    first.
    """
    if user.totp_enabled:
        raise ValueError("TOTP is already enabled. Disable it first.")
    pyotp = _pyotp()
    if not user.totp_secret:
        user.totp_secret = pyotp.random_base32()
        db.session.commit()
    return pyotp.totp.TOTP(user.totp_secret).provisioning_uri(
        name=user.email or user.username or str(user.id),
        issuer_name=ISSUER,
    )


def verify_otp(user: User, code: str) -> bool:
    """Check a 6-digit code against the user's TOTP. Tolerates the previous
    and next 30-second windows (clock skew).
    """
    if not user.totp_secret or not code:
        return False
    code = code.strip().replace(" ", "")
    pyotp = _pyotp()
    return pyotp.totp.TOTP(user.totp_secret).verify(code, valid_window=1)


def enable(user: User, code: str) -> bool:
    """Verify the user's first OTP and flip TOTP on. Generates recovery codes."""
    if not verify_otp(user, code):
        return False
    user.totp_enabled_at = utcnow()
    _regenerate_recovery_codes(user)
    db.session.commit()
    return True


def disable(user: User) -> None:
    user.totp_secret = None
    user.totp_enabled_at = None
    user.recovery_codes_json = None
    db.session.commit()


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _regenerate_recovery_codes(user: User) -> list[str]:
    """Mint 8 fresh recovery codes. Plaintexts returned ONCE."""
    plaintexts = [secrets.token_hex(5) for _ in range(8)]  # 10 hex chars each
    user.recovery_codes_json = json.dumps([_hash_code(p) for p in plaintexts])
    db.session.commit()
    return plaintexts


def regenerate_recovery_codes(user: User) -> list[str]:
    if not user.totp_enabled:
        raise ValueError("Enable TOTP first")
    return _regenerate_recovery_codes(user)


def consume_recovery_code(user: User, code: str) -> bool:
    """Single-use: mark the code consumed by removing its hash from the set."""
    if not user.recovery_codes_json or not code:
        return False
    h = _hash_code(code.strip().replace(" ", ""))
    try:
        codes = json.loads(user.recovery_codes_json)
    except Exception:
        return False
    if h not in codes:
        return False
    codes.remove(h)
    user.recovery_codes_json = json.dumps(codes)
    db.session.commit()
    return True


def qr_svg(otpauth_uri: str) -> str:
    """Render the otpauth URI as an inline SVG QR code.

    Uses `qrcode` with SVG factory to avoid a Pillow dependency.
    """
    try:
        import qrcode
        import qrcode.image.svg as svg_factory
    except ImportError as exc:
        raise TOTPUnavailable("qrcode not installed") from exc
    img = qrcode.make(otpauth_uri, image_factory=svg_factory.SvgImage)
    import io
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")
