"""API key minting + verification.

Tokens look like `fk_<32-byte-base64url>`. Only the SHA-256 hash is stored.
Optional per-key scopes restrict what the bearer can do; empty scopes
means full access (back-compat).

A key acts as its owner, so it can never do more than the owner's
*current* role in the workspace allows: `verify` refuses keys whose owner
is no longer a member, and `allows` intersects the key's scopes with the
role (a plain member never gets `ADMIN_SCOPES`). Removing a member also
revokes their keys.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Iterable, Optional

from filenergy import db
from filenergy.models import ApiKey, WorkspaceMember, utcnow


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

ADMIN_ROLES = ("owner", "admin")

# What the web UI already keeps to owners and admins: inviting members and
# reading the audit log.
ADMIN_SCOPES = frozenset({"members:write", "events:read"})


class ScopeNotAllowed(ValueError):
    """The minting user's role can't hold one of the requested scopes."""


def scopes_for_role(role: str | None) -> frozenset[str]:
    if role in ADMIN_ROLES:
        return frozenset(KNOWN_SCOPES)
    if role == "member":
        return frozenset(KNOWN_SCOPES) - ADMIN_SCOPES
    return frozenset()


def _current_role(workspace_id: int, user_id: int) -> str | None:
    m = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=user_id,
    ).first()
    return m.role if m else None


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
    """Create a key. Returns (row, plaintext_token). Plaintext shown ONCE.

    Raises `ScopeNotAllowed` when `user` isn't a member of `workspace` or
    asks for a scope their role can't hold. A member's "full access" key
    is stored as the explicit member scope list, so it doesn't grow if
    they're promoted later.
    """
    role = _current_role(workspace.id, user.id)
    allowed = scopes_for_role(role)
    if not allowed:
        raise ScopeNotAllowed("Only workspace members can create API keys.")
    normalized = _normalize_scopes(scopes)
    denied = sorted(set(normalized) - allowed)
    if denied:
        raise ScopeNotAllowed(
            "Your role can't create keys with: " + ", ".join(denied)
        )
    if not normalized and role not in ADMIN_ROLES:
        normalized = sorted(allowed)
    raw = secrets.token_urlsafe(32)
    plaintext = f"{TOKEN_PREFIX}_{raw}"
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
    """Look up an active key by plaintext token. Bumps last_used_at.

    The key's owner must still be a member of the key's workspace; their
    current role is attached as `row.member_role` for `allows`.
    """
    if not token or not token.startswith(TOKEN_PREFIX + "_"):
        return None
    row = ApiKey.query.filter_by(token_hash=_hash(token)).first()
    if row is None or row.revoked_at is not None:
        return None
    role = _current_role(row.workspace_id, row.user_id)
    if role is None:
        return None
    row.member_role = role
    row.last_used_at = utcnow()
    db.session.commit()
    return row


def allows(record: ApiKey, scope: str) -> bool:
    """Whether a key returned by `verify` may perform `scope`: the key must
    carry it and its owner's current role must allow it."""
    role = getattr(record, "member_role", None)
    if scope not in scopes_for_role(role):
        return False
    return record.has_scope(scope)


def revoke(workspace, key_id: int, *, by=None) -> bool:
    """Revoke a key. With `by`, a plain member may only revoke their own."""
    row = ApiKey.query.filter_by(id=key_id, workspace_id=workspace.id).first()
    if row is None or row.revoked_at is not None:
        return False
    if by is not None and row.user_id != by.id and (
        _current_role(workspace.id, by.id) not in ADMIN_ROLES
    ):
        return False
    row.revoked_at = utcnow()
    db.session.commit()
    return True


def revoke_for_member(workspace_id: int, user_id: int) -> int:
    """Revoke every live key `user_id` holds in the workspace. Doesn't
    commit: callers do it together with the membership change."""
    return (
        ApiKey.query.filter_by(workspace_id=workspace_id, user_id=user_id)
        .filter(ApiKey.revoked_at.is_(None))
        .update({ApiKey.revoked_at: utcnow()}, synchronize_session=False)
    )


def list_for_workspace(workspace) -> list[ApiKey]:
    return (
        ApiKey.query.filter_by(workspace_id=workspace.id)
        .order_by(ApiKey.id.desc())
        .all()
    )
