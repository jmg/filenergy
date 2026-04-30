"""Tests for at-rest encryption (services/crypto.py + EncryptedText)."""
import os

import pytest

from filenergy.services import crypto


def test_is_configured_false_default(monkeypatch):
    monkeypatch.delenv("FILENERGY_ENCRYPTION_KEY", raising=False)
    assert crypto.is_configured() is False


def test_is_configured_true_with_env(monkeypatch):
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    assert crypto.is_configured() is True


def test_encrypt_passthrough_when_unconfigured(monkeypatch):
    monkeypatch.delenv("FILENERGY_ENCRYPTION_KEY", raising=False)
    assert crypto.encrypt("hello") == "hello"
    assert crypto.encrypt(None) is None


def test_decrypt_passthrough_when_unconfigured(monkeypatch):
    monkeypatch.delenv("FILENERGY_ENCRYPTION_KEY", raising=False)
    assert crypto.decrypt("plaintext") == "plaintext"
    assert crypto.decrypt(None) is None


def test_roundtrip_with_key(monkeypatch):
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    enc = crypto.encrypt("a secret")
    assert enc.startswith("enc:")
    assert enc != "a secret"
    assert crypto.decrypt(enc) == "a secret"


def test_idempotent_encrypt(monkeypatch):
    """Encrypting an already-encrypted value is a no-op."""
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    enc = crypto.encrypt("secret")
    assert crypto.encrypt(enc) == enc


def test_decrypt_passthrough_for_plaintext_when_keyed(monkeypatch):
    """Mixed-encryption tables: plaintext rows still round-trip."""
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    assert crypto.decrypt("not-encrypted") == "not-encrypted"


def test_decrypt_with_bad_key_returns_value(monkeypatch, caplog):
    """If the key is rotated and we have a stale token, surface it instead
    of raising — operators can spot the failure in logs."""
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    encrypted = crypto.encrypt("hi")
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    with caplog.at_level("ERROR", logger="filenergy.services.crypto"):
        out = crypto.decrypt(encrypted)
    assert out == encrypted  # unchanged; garbled output not silently emitted


def test_decrypt_when_key_disappeared_logs_error(monkeypatch, caplog):
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    encrypted = crypto.encrypt("hi")
    monkeypatch.delenv("FILENERGY_ENCRYPTION_KEY", raising=False)
    with caplog.at_level("ERROR", logger="filenergy.services.crypto"):
        out = crypto.decrypt(encrypted)
    assert out == encrypted
    assert any("FILENERGY_ENCRYPTION_KEY" in r.message for r in caplog.records)


def test_generate_key_format():
    key = crypto.generate_key()
    assert isinstance(key, str)
    # Fernet keys are 32 url-safe base64 bytes (44 chars including '=').
    assert len(key) == 44


# ---- EncryptedText through SQLAlchemy ----


def test_encrypted_column_roundtrip_with_key(db, user, workspace, app, monkeypatch):
    """File.text_content writes encrypted, reads back plaintext."""
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    from filenergy.models import File

    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="x.txt", path="/x", url="hh",
        text_content="my secret notes",
    )
    db.session.add(f)
    db.session.commit()
    db.session.expire_all()

    # Read back through the ORM — auto-decrypted.
    fresh = File.query.first()
    assert fresh.text_content == "my secret notes"

    # Read raw row to confirm the on-disk value really is enc:...
    row = db.session.execute(
        db.text("SELECT text_content FROM file WHERE id=:id"), {"id": f.id}
    ).first()
    assert row[0].startswith("enc:")


def test_encrypted_column_passthrough_without_key(db, user, workspace, app, monkeypatch):
    monkeypatch.delenv("FILENERGY_ENCRYPTION_KEY", raising=False)
    from filenergy.models import File

    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="x.txt", path="/x", url="hh",
        text_content="plain",
    )
    db.session.add(f)
    db.session.commit()
    row = db.session.execute(
        db.text("SELECT text_content FROM file WHERE id=:id"), {"id": f.id}
    ).first()
    assert row[0] == "plain"


def test_reencrypt_all_walks_columns(db, user, workspace, app, monkeypatch):
    """Pre-existing plaintext rows get re-written through the encrypted type."""
    monkeypatch.delenv("FILENERGY_ENCRYPTION_KEY", raising=False)
    from filenergy.models import ConnectorAccount, File

    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="x.txt", path="/x", url="hh",
        text_content="plain notes",
    )
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="plain-token", refresh_token="plain-refresh",
    )
    db.session.add_all([f, a])
    db.session.commit()

    # Now flip encryption on and back-fill.
    monkeypatch.setenv("FILENERGY_ENCRYPTION_KEY", crypto.generate_key())
    counts = crypto.reencrypt_all()
    assert counts["file"] >= 1
    assert counts["connector_account"] >= 1

    raw = db.session.execute(
        db.text("SELECT text_content FROM file WHERE id=:id"), {"id": f.id}
    ).first()
    assert raw[0].startswith("enc:")
    assert File.query.first().text_content == "plain notes"


def test_reencrypt_all_noop_without_key(db, monkeypatch):
    monkeypatch.delenv("FILENERGY_ENCRYPTION_KEY", raising=False)
    counts = crypto.reencrypt_all()
    assert counts == {"file": 0, "chunk": 0, "connector_account": 0, "user": 0}
