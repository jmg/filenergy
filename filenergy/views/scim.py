"""SCIM 2.0 — auto-provisioning from Okta, Workspace, Auth0, Azure AD.

Implements the slice of RFC 7644 that identity providers actually use:

  GET    /scim/v2/Users
  GET    /scim/v2/Users/<id>
  POST   /scim/v2/Users
  PUT    /scim/v2/Users/<id>
  PATCH  /scim/v2/Users/<id>
  DELETE /scim/v2/Users/<id>
  GET    /scim/v2/ServiceProviderConfig
  GET    /scim/v2/Schemas
  GET    /scim/v2/ResourceTypes

Auth: bearer token in `Authorization: Bearer <token>`, compared
constant-time against `FILENERGY_SCIM_TOKEN`. The endpoint is
workspace-scoped via a second header `X-Filenergy-Workspace-Slug`
(IdPs let you set arbitrary headers on the SCIM connection).

Provisioning a SCIM user creates (or revives) a `User` row + a
`WorkspaceMember` link. De-provisioning removes the membership but
preserves the User and audit trail. This matches what compliant SaaS
products do — disabling a user shouldn't nuke their data.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from typing import Any

from flask import Blueprint, jsonify, request

from filenergy import db
from filenergy.models import User, Workspace, WorkspaceMember, utcnow

scim_bp = Blueprint("scim", __name__)

log = logging.getLogger(__name__)


SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _scim_error(detail: str, status: int) -> tuple:
    return jsonify({
        "schemas": [SCIM_ERROR_SCHEMA],
        "detail": detail,
        "status": str(status),
    }), status


def _check_auth(workspace_slug: str | None = None) -> tuple[Workspace | None, tuple | None]:
    """Returns (workspace, error_response). On success the second slot is None."""
    expected = os.environ.get("FILENERGY_SCIM_TOKEN")
    if not expected:
        return None, _scim_error("SCIM disabled — set FILENERGY_SCIM_TOKEN", 503)

    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None, _scim_error("Missing bearer token", 401)
    presented = auth.split(" ", 1)[1].strip()
    if not hmac.compare_digest(expected, presented):
        return None, _scim_error("Invalid bearer token", 401)

    slug = workspace_slug or request.headers.get("X-Filenergy-Workspace-Slug", "")
    if not slug:
        return None, _scim_error("Missing X-Filenergy-Workspace-Slug header", 400)
    ws = Workspace.query.filter_by(slug=slug).first()
    if ws is None:
        return None, _scim_error(f"Workspace '{slug}' not found", 404)
    return ws, None


def _user_to_scim(user: User, workspace: Workspace, *, role: str = "member") -> dict[str, Any]:
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": str(user.id),
        "userName": user.email or user.username or str(user.id),
        "name": {"formatted": user.username or user.email or ""},
        "emails": [{"value": user.email or "", "primary": True}],
        "active": True,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else None,
            "location": f"/scim/v2/Users/{user.id}",
        },
        # Custom attribute: which role the user has in the workspace.
        "urn:ietf:params:scim:schemas:extension:filenergy:2.0:User": {
            "workspaceRole": role,
            "workspaceSlug": workspace.slug,
        },
    }


def _existing_membership(workspace: Workspace, user: User) -> WorkspaceMember | None:
    return WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=user.id,
    ).first()


# ---------------------------------------------------------------------------
# Discovery endpoints — Okta / Workspace probe these on connection setup.
# ---------------------------------------------------------------------------


@scim_bp.route("/v2/ServiceProviderConfig")
def service_provider_config():
    return jsonify({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "name": "OAuth Bearer Token",
            "description": "Authentication scheme using the OAuth Bearer Token Standard",
            "specUri": "http://www.rfc-editor.org/info/rfc6750",
            "type": "oauthbearertoken",
            "primary": True,
        }],
    })


@scim_bp.route("/v2/Schemas")
def schemas():
    return jsonify({
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": 1,
        "Resources": [{
            "id": SCIM_USER_SCHEMA,
            "name": "User",
            "description": "User account",
        }],
    })


@scim_bp.route("/v2/ResourceTypes")
def resource_types():
    return jsonify({
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": 1,
        "Resources": [{
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": SCIM_USER_SCHEMA,
        }],
    })


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@scim_bp.route("/v2/Users")
def list_users():
    ws, err = _check_auth()
    if err is not None:
        return err

    members = WorkspaceMember.query.filter_by(workspace_id=ws.id).all()
    resources = []
    for m in members:
        if m.user is None:
            continue
        resources.append(_user_to_scim(m.user, ws, role=m.role))

    return jsonify({
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": len(resources),
        "Resources": resources,
        "startIndex": 1,
        "itemsPerPage": len(resources),
    })


@scim_bp.route("/v2/Users/<int:user_id>")
def get_user(user_id):
    ws, err = _check_auth()
    if err is not None:
        return err
    user = User.query.get(user_id)
    if user is None:
        return _scim_error("User not found", 404)
    member = _existing_membership(ws, user)
    if member is None:
        return _scim_error("User not in workspace", 404)
    return jsonify(_user_to_scim(user, ws, role=member.role))


@scim_bp.route("/v2/Users", methods=["POST"])
def create_user():
    ws, err = _check_auth()
    if err is not None:
        return err

    payload = request.get_json(silent=True) or {}
    email = (
        (payload.get("emails") or [{}])[0].get("value")
        or payload.get("userName")
        or ""
    ).strip().lower()
    if not email:
        return _scim_error("emails or userName is required", 400)

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email, username=email)
        # Random password — IdP-provisioned users sign in via SAML/SSO,
        # not local passwords. Setting one keeps the column non-null.
        user.set_password(secrets.token_urlsafe(24))
        db.session.add(user)
        db.session.commit()

    if _existing_membership(ws, user) is None:
        ext = payload.get(
            "urn:ietf:params:scim:schemas:extension:filenergy:2.0:User", {}
        )
        role = (ext.get("workspaceRole") or "member").lower()
        if role not in ("owner", "admin", "member"):
            role = "member"
        db.session.add(WorkspaceMember(
            workspace_id=ws.id, user_id=user.id, role=role,
        ))
        db.session.commit()

    member = _existing_membership(ws, user)
    return jsonify(_user_to_scim(user, ws, role=member.role)), 201


@scim_bp.route("/v2/Users/<int:user_id>", methods=["PUT"])
def replace_user(user_id):
    ws, err = _check_auth()
    if err is not None:
        return err
    user = User.query.get(user_id)
    if user is None:
        return _scim_error("User not found", 404)
    member = _existing_membership(ws, user)
    if member is None:
        return _scim_error("User not in workspace", 404)

    payload = request.get_json(silent=True) or {}
    if payload.get("active") is False:
        # IdP marks the user inactive → remove the membership but keep
        # the user row (data, audit, GDPR).
        db.session.delete(member)
        db.session.commit()
        return jsonify(_user_to_scim(user, ws, role=member.role))

    ext = payload.get(
        "urn:ietf:params:scim:schemas:extension:filenergy:2.0:User", {}
    )
    role = (ext.get("workspaceRole") or member.role).lower()
    if role in ("owner", "admin", "member"):
        member.role = role
        db.session.commit()
    return jsonify(_user_to_scim(user, ws, role=member.role))


@scim_bp.route("/v2/Users/<int:user_id>", methods=["PATCH"])
def patch_user(user_id):
    """SCIM PATCH (RFC 7644 §3.5.2) — applies a list of operations."""
    ws, err = _check_auth()
    if err is not None:
        return err
    user = User.query.get(user_id)
    if user is None:
        return _scim_error("User not found", 404)
    member = _existing_membership(ws, user)
    if member is None:
        return _scim_error("User not in workspace", 404)

    payload = request.get_json(silent=True) or {}
    operations = payload.get("Operations", [])
    for op in operations:
        path = (op.get("path") or "").lower()
        value = op.get("value")
        verb = (op.get("op") or "").lower()
        if path == "active" and verb in ("replace", "add"):
            active = value if isinstance(value, bool) else (
                isinstance(value, dict) and value.get("active")
            )
            if active is False:
                db.session.delete(member)
                db.session.commit()
                return jsonify(_user_to_scim(user, ws, role=member.role))
    return jsonify(_user_to_scim(user, ws, role=member.role))


@scim_bp.route("/v2/Users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    ws, err = _check_auth()
    if err is not None:
        return err
    user = User.query.get(user_id)
    if user is None:
        return _scim_error("User not found", 404)
    member = _existing_membership(ws, user)
    if member is not None:
        db.session.delete(member)
        db.session.commit()
    return "", 204
