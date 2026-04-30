"""API key minting + verification.

Tokens look like `fk_<32-byte-base64url>`. Only the SHA-256 hash is stored.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from filenergy import db
from filenergy.models import ApiKey, utcnow


TOKEN_PREFIX = "fk"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint(workspace, user, name: str) -> tuple[ApiKey, str]:
    """Create a key. Returns (row, plaintext_token). Plaintext shown ONCE."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{TOKEN_PREFIX}_{raw}"
    record = ApiKey(
        workspace_id=workspace.id,
        user_id=user.id,
        name=name.strip()[:120] or "API key",
        prefix=plaintext[:12],
        token_hash=_hash(plaintext),
    )
    db.session.add(record)
    db.session.commit()
    return record, plaintext


def verify(token: str) -> Optional[ApiKey]:
    """Look up an active key by plaintext token. Bumps last_used_at."""
    if not token or not token.startswith(TOKEN_PREFIX + "_"):
        return None
    row = ApiKey.query.filter_by(token_hash=_hash(token)).first()
    if row is None or row.revoked_at is not None:
        return None
    row.last_used_at = utcnow()
    db.session.commit()
    return row


def revoke(workspace, key_id: int) -> bool:
    row = ApiKey.query.filter_by(id=key_id, workspace_id=workspace.id).first()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    db.session.commit()
    return True


def list_for_workspace(workspace) -> list[ApiKey]:
    return (
        ApiKey.query.filter_by(workspace_id=workspace.id)
        .order_by(ApiKey.id.desc())
        .all()
    )
