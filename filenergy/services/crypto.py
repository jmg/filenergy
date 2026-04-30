"""Envelope encryption for sensitive columns.

`FILENERGY_ENCRYPTION_KEY` (a Fernet key, 32 url-safe base64 bytes)
controls whether at-rest encryption is on. Without it, columns wrapped
with `EncryptedText` round-trip as plaintext — handy in dev and for
backwards compatibility with rows written before this feature shipped.

When the key is set, new writes get prefixed with `enc:`; reads detect
the prefix and decrypt. Mixed rows (some encrypted, some not) coexist
peacefully so an upgrade can re-encrypt lazily without downtime.

Run `python manage.py reencrypt` after enabling the key to back-fill
existing rows.
"""
from __future__ import annotations

import base64
import logging
import os

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

log = logging.getLogger(__name__)


_PREFIX = "enc:"


def is_configured() -> bool:
    return bool(os.environ.get("FILENERGY_ENCRYPTION_KEY"))


def _fernet():
    """Lazy-import + lazy-construct so the Flask app boots without `cryptography`
    in the import path until somebody actually encrypts something."""
    key = os.environ.get("FILENERGY_ENCRYPTION_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover — cryptography pulled in by pypdf
        raise RuntimeError("cryptography is required for at-rest encryption") from exc
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def generate_key() -> str:
    """Convenience for `python manage.py generate-encryption-key`."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode("ascii")


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    if not is_configured():
        return plaintext
    fernet = _fernet()
    if fernet is None:
        return plaintext
    if isinstance(plaintext, str) and plaintext.startswith(_PREFIX):
        # Already encrypted — don't double-wrap (idempotent).
        return plaintext
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        # Either empty / numeric / already-plaintext — pass through.
        return value
    fernet = _fernet()
    if fernet is None:
        # The row is encrypted but we lost the key. Surface the prefixed
        # string so the failure mode is visible rather than silent garbage.
        log.error("Decryption requested but FILENERGY_ENCRYPTION_KEY is unset")
        return value
    try:
        return fernet.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        log.exception("Failed to decrypt column value")
        return value


class EncryptedText(TypeDecorator):
    """Text column that auto-encrypts on write and decrypts on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)


def reencrypt_all(*, batch: int = 500) -> dict:
    """Walk every encrypted column and re-write each row through the type.

    A no-op when the key isn't set. Useful after rotating the key (rotate
    by setting both old and new keys via MultiFernet — out of scope here)
    or after enabling encryption on existing data.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from filenergy import db
    from filenergy.models import Chunk, ConnectorAccount, File, User

    counts = {"file": 0, "chunk": 0, "connector_account": 0, "user": 0}
    if not is_configured():
        return counts

    def _touch(obj, attr):
        """Force the column to round-trip through the type decorator."""
        value = getattr(obj, attr)
        if value is None:
            return False
        # Re-assign + mark dirty so SQLAlchemy emits an UPDATE even when
        # the post-decoded plaintext is unchanged.
        setattr(obj, attr, value)
        flag_modified(obj, attr)
        return True

    for f in File.query.all():
        if _touch(f, "text_content"):
            counts["file"] += 1
    for c in Chunk.query.yield_per(batch):
        if _touch(c, "embedding"):
            counts["chunk"] += 1
    for a in ConnectorAccount.query.all():
        if _touch(a, "access_token"):
            counts["connector_account"] += 1
        _touch(a, "refresh_token")
    for u in User.query.all():
        if _touch(u, "totp_secret"):
            counts["user"] += 1
    db.session.commit()
    return counts
