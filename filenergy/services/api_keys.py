"""API key minting + verification.

Tokens look like `fk_<32-byte-base64url>`. Only the SHA-256 hash is stored.
Optional per-key scopes restrict what the bearer can do; empty scopes
means full access (back-compat).
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Iterable, Optional

from filenergy import db
from filenergy.models import ApiKey, utcnow


TOKEN_PREFIX = "fk"

# The full set of recognised scopes. Validated at mint time so a typo
# can't lock an integration out forever.
KNOWN_SCOPES = {
    "files:read", "files:write",
    "ask:read", "ask:write",
    "collections:read", "collections:write",
    "conversations:read", "conversations:write",
    "webhooks:read", "webhooks:write",
    "members:read", "members:write",
    "share_links:read", "share_links:write",
    "events:read",
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_scopes(scopes: Iterable[str] | None) -> list[str]:
    if not scopes:
        return []
    out = []
    for s in scopes:
        s = (s or "").strip().lower()
        if s in KNOWN_SCOPES and s not in out:
            out.append(s)
    return sorted(out)


def mint(workspace, user, name: str,
         *, scopes: Iterable[str] | None = None) -> tuple[ApiKey, str]:
    """Create a key. Returns (row, plaintext_token). Plaintext shown ONCE."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{TOKEN_PREFIX}_{raw}"
    normalized = _normalize_scopes(scopes)
    record = ApiKey(
        workspace_id=workspace.id,
        user_id=user.id,
        name=name.strip()[:120] or "API key",
        prefix=plaintext[:12],
        token_hash=_hash(plaintext),
        scopes_json=json.dumps(normalized) if normalized else None,
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
