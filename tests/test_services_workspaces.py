import pytest

from filenergy.models import Workspace, WorkspaceInvitation, WorkspaceMember
from filenergy.services import workspaces


def test_slugify_lowercases_and_dehyphens():
    assert workspaces.slugify("Acme Corp!") == "acme-corp"
    assert workspaces.slugify("   ") == "workspace"


def test_create_assigns_slug_and_owner_member(db, user, app):
    with app.test_request_context():
        ws = workspaces.create(user, name="Acme")
    assert ws.slug == "acme"
    assert ws.owner_id == user.id
    members = WorkspaceMember.query.filter_by(workspace_id=ws.id).all()
    assert len(members) == 1
    assert members[0].role == "owner"


def test_unique_slug_disambiguates(db, user, app):
    with app.test_request_context():
        a = workspaces.create(user, name="Acme")
        b = workspaces.create(user, name="Acme")
    assert a.slug != b.slug
    assert a.slug == "acme"
    assert b.slug.startswith("acme-")


def test_ensure_default_for_is_idempotent(db, user, app):
    with app.test_request_context():
        a = workspaces.ensure_default_for(user)
        b = workspaces.ensure_default_for(user)
    assert a.id == b.id


def test_set_current_requires_membership(db, user, workspace, app, client):
    other = Workspace(name="Other", slug="other", owner_id=999)
    db.session.add(other)
    db.session.commit()
    with client:
        client.get("/")  # set up session
        assert workspaces.set_current(user, other.id) is None
        assert workspaces.set_current(user, workspace.id).id == workspace.id


def test_get_current_falls_back_to_first_membership(db, user, workspace, client):
    with client:
        client.get("/")
        ws = workspaces.get_current(user)
    assert ws.id == workspace.id


def test_get_current_returns_none_for_anonymous(client):
    with client:
        client.get("/")
        assert workspaces.get_current(None) is None


def test_invite_and_accept_flow(db, user, workspace, app):
    other_user = _make_user(db, "joiner@x")
    with app.test_request_context():
        inv = workspaces.invite(workspace, user, "joiner@x", "admin")
    assert inv.token
    assert inv.role == "admin"
    with app.test_request_context():
        ws = workspaces.accept_invitation(inv.token, other_user)
    assert ws.id == workspace.id
    member = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=other_user.id
    ).one()
    assert member.role == "admin"


def test_invite_role_falls_back_to_member(db, user, workspace, app):
    with app.test_request_context():
        inv = workspaces.invite(workspace, user, "x@x", "evil_admin_god")
    assert inv.role == "member"


def test_accept_invitation_unknown_token_returns_none(db, user, app):
    with app.test_request_context():
        assert workspaces.accept_invitation("not-a-real-token", user) is None


def test_accept_invitation_expired(db, user, workspace, app):
    from datetime import datetime

    other = _make_user(db, "joiner@x")
    inv = WorkspaceInvitation(
        workspace_id=workspace.id, email="x", role="member",
        token="expired", invited_by_id=user.id,
        expires_at=datetime(1970, 1, 1),  # naive — matches utcnow()
    )
    db.session.add(inv)
    db.session.commit()
    with app.test_request_context():
        assert workspaces.accept_invitation("expired", other) is None


def test_accept_invitation_idempotent(db, user, workspace, app):
    other = _make_user(db, "joiner@x")
    with app.test_request_context():
        inv = workspaces.invite(workspace, user, "joiner@x")
        workspaces.accept_invitation(inv.token, other)
        # Second time the invitation is consumed.
        assert workspaces.accept_invitation(inv.token, other) is None


def test_remove_member(db, user, workspace, app):
    other = _make_user(db, "joiner@x")
    with app.test_request_context():
        inv = workspaces.invite(workspace, user, "joiner@x")
        workspaces.accept_invitation(inv.token, other)
        assert workspaces.remove_member(workspace, other.id) is True
        # Owner cannot be removed.
        assert workspaces.remove_member(workspace, user.id) is False
        # Removing twice is a no-op.
        assert workspaces.remove_member(workspace, other.id) is False


def test_role_helpers(db, user, workspace):
    assert workspace.role_of(user) == "owner"
    assert workspaces.require_role(workspace, user, "owner")
    assert not workspaces.require_role(workspace, user, "member")


def test_role_of_for_anonymous(workspace):
    assert workspace.role_of(None) is None
    assert workspace.has_member(None) is False


def test_list_for_user_returns_only_membered(db, user, app):
    other = _make_user(db, "x@x")
    with app.test_request_context():
        a = workspaces.ensure_default_for(user)
        workspaces.create(other, name="theirs")
    listed = workspaces.list_for_user(user)
    assert {w.id for w in listed} == {a.id}


def _make_user(db, email):
    from filenergy.models import User

    u = User(email=email, username=email)
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u
