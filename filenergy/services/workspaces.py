"""Workspace lifecycle: create, slugify, invite, accept, switch.

A `Workspace` is the tenant boundary. Files, conversations, events, API
keys all live under one. Users are members via `WorkspaceMember` rows; one
user can belong to many workspaces and switches between them via
`set_current(...)`.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Optional

from flask import session

from filenergy import db
from filenergy.models import (
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    utcnow,
)


SESSION_KEY = "current_workspace_id"
INVITE_TTL_DAYS = 14


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workspace"
    return base[:48]


def _unique_slug(name: str) -> str:
    base = slugify(name)
    candidate = base
    counter = 2
    while Workspace.query.filter_by(slug=candidate).first() is not None:
        suffix = f"-{counter}"
        candidate = base[: 64 - len(suffix)] + suffix
        counter += 1
    return candidate


def create(owner: User, name: Optional[str] = None) -> Workspace:
    name = (name or "").strip() or f"{owner.email}'s workspace"
    ws = Workspace(name=name, slug=_unique_slug(name), owner_id=owner.id)
    db.session.add(ws)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=ws.id, user_id=owner.id, role="owner"
    ))
    db.session.commit()
    return ws


def ensure_default_for(user: User) -> Workspace:
    """Every user gets a personal workspace on first sight."""
    membership = WorkspaceMember.query.filter_by(user_id=user.id).first()
    if membership is not None:
        return membership.workspace
    return create(user, name=f"{user.email}'s workspace")


def list_for_user(user: User) -> list[Workspace]:
    rows = (
        WorkspaceMember.query.filter_by(user_id=user.id)
        .order_by(WorkspaceMember.id.asc())
        .all()
    )
    return [m.workspace for m in rows]


def set_current(user: User, workspace_id: int) -> Optional[Workspace]:
    """Activate a workspace if the user is a member; returns it or None."""
    ws = Workspace.query.get(workspace_id)
    if ws is None or not ws.has_member(user):
        return None
    session[SESSION_KEY] = ws.id
    return ws


def get_current(user: User) -> Optional[Workspace]:
    """Resolve the active workspace for the request.

    Order: session value (if member) → user's first membership → None.
    """
    if user is None or not getattr(user, "id", None):
        return None
    wid = session.get(SESSION_KEY)
    if wid:
        ws = Workspace.query.get(wid)
        if ws is not None and ws.has_member(user):
            return ws
    membership = (
        WorkspaceMember.query.filter_by(user_id=user.id)
        .order_by(WorkspaceMember.id.asc())
        .first()
    )
    if membership is None:
        return None
    session[SESSION_KEY] = membership.workspace_id
    return membership.workspace


def role_of(workspace: Workspace, user: User) -> Optional[str]:
    return workspace.role_of(user)


def require_role(workspace: Workspace, user: User, *roles: str) -> bool:
    return role_of(workspace, user) in roles


# ---- Invitations ----


def invite(workspace: Workspace, inviter: User, email: str, role: str = "member") -> WorkspaceInvitation:
    if role not in ("member", "admin"):
        role = "member"
    inv = WorkspaceInvitation(
        workspace_id=workspace.id,
        email=email.strip().lower(),
        role=role,
        token=secrets.token_urlsafe(32),
        invited_by_id=inviter.id,
        expires_at=utcnow() + timedelta(days=INVITE_TTL_DAYS),
    )
    db.session.add(inv)
    db.session.commit()
    return inv


def find_invitation(token: str) -> Optional[WorkspaceInvitation]:
    inv = WorkspaceInvitation.query.filter_by(token=token).first()
    if inv is None:
        return None
    if inv.accepted_at is not None:
        return None
    if inv.expires_at and utcnow() >= inv.expires_at:
        return None
    return inv


def accept_invitation(token: str, user: User) -> Optional[Workspace]:
    inv = find_invitation(token)
    if inv is None:
        return None
    # Idempotent membership add.
    existing = WorkspaceMember.query.filter_by(
        workspace_id=inv.workspace_id, user_id=user.id
    ).first()
    if existing is None:
        db.session.add(WorkspaceMember(
            workspace_id=inv.workspace_id, user_id=user.id, role=inv.role
        ))
    inv.accepted_at = utcnow()
    db.session.commit()
    return inv.workspace


def remove_member(workspace: Workspace, user_id: int) -> bool:
    """Remove a member. Cannot remove the owner."""
    if workspace.owner_id == user_id:
        return False
    m = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=user_id
    ).first()
    if m is None:
        return False
    db.session.delete(m)
    db.session.commit()
    return True


def members(workspace: Workspace):
    return (
        WorkspaceMember.query.filter_by(workspace_id=workspace.id)
        .order_by(WorkspaceMember.id.asc())
        .all()
    )


def pending_invitations(workspace: Workspace):
    return (
        WorkspaceInvitation.query.filter_by(workspace_id=workspace.id)
        .filter(WorkspaceInvitation.accepted_at.is_(None))
        .order_by(WorkspaceInvitation.id.desc())
        .all()
    )
