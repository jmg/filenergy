"""Tests for TOTP 2FA + recovery codes."""
import json

import pytest

from filenergy.services import totp


def test_start_setup_assigns_secret(db, user, app):
    with app.test_request_context():
        uri = totp.start_setup(user)
    assert user.totp_secret
    assert uri.startswith("otpauth://totp/")
    assert "Filenergy" in uri


def test_start_setup_idempotent_until_enabled(db, user, app):
    with app.test_request_context():
        uri1 = totp.start_setup(user)
        uri2 = totp.start_setup(user)
    assert uri1 == uri2


def test_start_setup_blocks_if_already_enabled(db, user, app):
    with app.test_request_context():
        totp.start_setup(user)
        from filenergy.models import utcnow
        user.totp_enabled_at = utcnow()
        from filenergy import db as real_db
        real_db.session.commit()
        with pytest.raises(ValueError):
            totp.start_setup(user)


def test_verify_otp_accepts_current_code(db, user, app):
    import pyotp

    with app.test_request_context():
        totp.start_setup(user)
        code = pyotp.TOTP(user.totp_secret).now()
        assert totp.verify_otp(user, code) is True


def test_verify_otp_strips_whitespace(db, user, app):
    import pyotp

    with app.test_request_context():
        totp.start_setup(user)
        code = pyotp.TOTP(user.totp_secret).now()
        assert totp.verify_otp(user, "  " + code + " ") is True


def test_verify_otp_rejects_wrong_code(db, user, app):
    with app.test_request_context():
        totp.start_setup(user)
        assert totp.verify_otp(user, "000000") is False


def test_verify_otp_rejects_when_no_secret(db, user):
    assert totp.verify_otp(user, "123456") is False
    assert totp.verify_otp(user, "") is False


def test_enable_flips_state_and_creates_recovery_codes(db, user, app):
    import pyotp

    with app.test_request_context():
        totp.start_setup(user)
        code = pyotp.TOTP(user.totp_secret).now()
        ok = totp.enable(user, code)
    assert ok
    assert user.totp_enabled is True
    assert json.loads(user.recovery_codes_json)


def test_enable_rejects_invalid_code(db, user, app):
    with app.test_request_context():
        totp.start_setup(user)
        assert totp.enable(user, "000000") is False
        assert user.totp_enabled is False


def test_disable_clears_secret_and_codes(db, user, app):
    import pyotp

    with app.test_request_context():
        totp.start_setup(user)
        totp.enable(user, pyotp.TOTP(user.totp_secret).now())
        totp.disable(user)
    assert user.totp_secret is None
    assert user.totp_enabled_at is None
    assert user.recovery_codes_json is None


def test_consume_recovery_code_works_once(db, user, app):
    import pyotp

    with app.test_request_context():
        totp.start_setup(user)
        totp.enable(user, pyotp.TOTP(user.totp_secret).now())
        codes = totp.regenerate_recovery_codes(user)
        first = codes[0]
        assert totp.consume_recovery_code(user, first) is True
        assert totp.consume_recovery_code(user, first) is False


def test_consume_recovery_code_rejects_unknown(db, user, app):
    import pyotp

    with app.test_request_context():
        totp.start_setup(user)
        totp.enable(user, pyotp.TOTP(user.totp_secret).now())
        assert totp.consume_recovery_code(user, "garbage") is False


def test_consume_recovery_code_rejects_when_empty(db, user):
    assert totp.consume_recovery_code(user, "") is False
    assert totp.consume_recovery_code(user, "any") is False


def test_consume_recovery_code_handles_invalid_json(db, user):
    user.recovery_codes_json = "not json"
    assert totp.consume_recovery_code(user, "abc") is False


def test_regenerate_recovery_codes_requires_enabled(db, user, app):
    with app.test_request_context():
        totp.start_setup(user)
        with pytest.raises(ValueError):
            totp.regenerate_recovery_codes(user)


def test_qr_svg_returns_svg(db, user, app):
    with app.test_request_context():
        uri = totp.start_setup(user)
        svg = totp.qr_svg(uri)
    assert "<svg" in svg


def test_pyotp_unavailable_raises(monkeypatch, db, user, app):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyotp":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with app.test_request_context(), pytest.raises(totp.TOTPUnavailable):
        totp.start_setup(user)


def test_qrcode_unavailable_raises(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("qrcode", "qrcode.image.svg"):
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(totp.TOTPUnavailable):
        totp.qr_svg("otpauth://totp/x")
