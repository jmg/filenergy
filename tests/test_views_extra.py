"""Tests for the new blueprints: workspace, settings, share, api/v1, webhooks."""
from __future__ import annotations

import io
import sys
import types

import pytest

from filenergy import settings as cfg
from filenergy.models import (
    ApiKey,
    Conversation,
    Event,
    File,
    ShareLink,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from filenergy.services import api_keys


# ---- workspace blueprint ----


def test_register_creates_default_workspace(client):
    client.post(
        "/user/register/",
        data={"email": "x@x.co", "password": "pw", "password_again": "pw"},
    )
    u = User.query.filter_by(email="x@x.co").one()
    members = WorkspaceMember.query.filter_by(user_id=u.id).all()
    assert len(members) == 1
    assert members[0].role == "owner"


def test_workspace_switch_to_member_workspace(auth_client, db, user, workspace):
    other_owner = User(email="o@o", username="o")
    other_owner.set_password("pw")
    db.session.add(other_owner)
    db.session.commit()
    from filenergy.services import workspaces

    other_ws = workspaces.create(other_owner, name="theirs")
    db.session.add(WorkspaceMember(
        workspace_id=other_ws.id, user_id=user.id, role="member"
    ))
    db.session.commit()

    r = auth_client.post(f"/w/switch/{other_ws.id}", data={"next": "/file/list/"})
    assert r.status_code == 302
    assert "/file/list/" in r.headers["Location"]
    assert Event.query.filter_by(type="workspace.switched").count() == 1


def test_workspace_switch_blocks_non_members(auth_client, db):
    foreign = Workspace(name="foreign", slug="foreign", owner_id=999)
    db.session.add(foreign)
    db.session.commit()
    r = auth_client.post(f"/w/switch/{foreign.id}")
    assert r.status_code == 403


def test_workspace_create_route(auth_client, user):
    r = auth_client.post("/w/new", data={"name": "new ws"})
    assert r.status_code == 302
    assert Workspace.query.filter_by(name="new ws").one()


def test_invite_only_admins(client, db, user, workspace):
    """A regular member can't invite."""
    other = User(email="m@m", username="m")
    other.set_password("pw")
    db.session.add(other)
    from filenergy.services import workspaces

    workspaces.ensure_default_for(other)
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    client.post("/user/login/", data={"email": "m@m", "password": "pw"})
    # Switch to user's workspace.
    client.post(f"/w/switch/{workspace.id}")
    r = client.post("/w/invite", data={"email": "z@z", "role": "member"})
    assert r.status_code == 403


def test_invite_creates_invitation(auth_client, workspace):
    r = auth_client.post(
        "/w/invite", data={"email": "joiner@x", "role": "admin"}
    )
    assert r.status_code == 302
    inv = WorkspaceInvitation.query.filter_by(email="joiner@x").one()
    assert inv.role == "admin"


def test_invite_empty_email_flashes(auth_client):
    r = auth_client.post("/w/invite", data={"email": ""}, follow_redirects=False)
    assert r.status_code == 302


def test_accept_invitation_route(client, db, auth_client, user, workspace):
    from filenergy.services import workspaces

    other = User(email="joiner@x", username="joiner@x")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    workspaces.ensure_default_for(other)
    inv = workspaces.invite(workspace, user, "joiner@x", "member")

    auth_client.get("/user/logout/")
    auth_client.post(
        "/user/login/", data={"email": "joiner@x", "password": "pw"}
    )
    r = auth_client.get(f"/w/accept/{inv.token}")
    assert r.status_code == 302
    assert WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=other.id
    ).count() == 1


def test_accept_invitation_unknown_token_404(auth_client):
    r = auth_client.get("/w/accept/garbage")
    assert r.status_code == 404


def test_remove_member(auth_client, db, user, workspace):
    other = User(email="rm@x", username="rm@x")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    r = auth_client.post(f"/w/members/{other.id}/remove")
    assert r.status_code == 302
    assert WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=other.id
    ).first() is None


def test_remove_member_requires_admin(client, db, user, workspace):
    other = User(email="m@m", username="m")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    client.post("/user/login/", data={"email": "m@m", "password": "pw"})
    client.post(f"/w/switch/{workspace.id}")
    r = client.post(f"/w/members/{user.id}/remove")
    assert r.status_code == 403


# ---- settings blueprint ----


def test_settings_index_redirects(auth_client):
    r = auth_client.get("/settings/")
    assert r.status_code == 302


def test_settings_profile_loads(auth_client, user):
    r = auth_client.get("/settings/profile")
    assert r.status_code == 200
    assert user.email.encode() in r.data


def test_update_password_success(auth_client, user, db):
    r = auth_client.post("/settings/profile", data={
        "current_password": "password",
        "new_password": "newpw",
        "confirm_password": "newpw",
    })
    assert r.status_code == 302
    db.session.refresh(user)
    assert user.check_password("newpw")


def test_update_password_wrong_current(auth_client):
    r = auth_client.post("/settings/profile", data={
        "current_password": "wrong",
        "new_password": "x",
        "confirm_password": "x",
    })
    assert r.status_code == 302  # redirect with flash


def test_update_password_mismatch(auth_client):
    r = auth_client.post("/settings/profile", data={
        "current_password": "password",
        "new_password": "a",
        "confirm_password": "b",
    })
    assert r.status_code == 302


def test_settings_keys_page(auth_client):
    r = auth_client.get("/settings/keys")
    assert r.status_code == 200


def test_create_api_key(auth_client, workspace):
    r = auth_client.post("/settings/keys", data={"name": "ci"})
    assert r.status_code == 302
    assert ApiKey.query.filter_by(name="ci", workspace_id=workspace.id).one()


def test_revoke_api_key(auth_client, workspace, user, app):
    with app.test_request_context():
        record, _ = api_keys.mint(workspace, user, "x")
    r = auth_client.post(f"/settings/keys/{record.id}/revoke")
    assert r.status_code == 302
    assert not api_keys.list_for_workspace(workspace)[0].is_active


def test_settings_workspace_page(auth_client):
    r = auth_client.get("/settings/workspace")
    assert r.status_code == 200


def test_settings_billing_page(auth_client):
    r = auth_client.get("/settings/billing")
    assert r.status_code == 200
    assert b"Free" in r.data


def test_billing_checkout_requires_owner(client, db, user, workspace):
    other = User(email="m@m", username="m")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    client.post("/user/login/", data={"email": "m@m", "password": "pw"})
    client.post(f"/w/switch/{workspace.id}")
    r = client.post("/settings/billing/checkout", data={"plan": "pro"})
    assert r.status_code == 403


def test_billing_checkout_unconfigured(auth_client, monkeypatch):
    monkeypatch.setattr(cfg, "STRIPE_SECRET_KEY", "")
    r = auth_client.post(
        "/settings/billing/checkout", data={"plan": "pro"}, follow_redirects=False
    )
    assert r.status_code == 302  # redirected to billing page with flash


def test_billing_checkout_redirects(auth_client, monkeypatch):
    fake = types.ModuleType("stripe")
    fake.api_key = None
    fake.Customer = type("C", (), {"create": staticmethod(lambda **kw: {"id": "cus_1"})})
    fake.checkout = types.SimpleNamespace(
        Session=type("S", (), {"create": staticmethod(
            lambda **kw: {"url": "https://stripe.test/checkout/abc"}
        )})
    )
    fake.Webhook = type("W", (), {"construct_event": staticmethod(lambda *a, **k: {})})
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setattr(cfg, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(cfg, "STRIPE_PRICE_PRO", "price_x")
    r = auth_client.post(
        "/settings/billing/checkout",
        data={"plan": "pro"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "stripe.test" in r.headers["Location"]


# ---- share blueprint ----


def test_share_landing_404_on_unknown(client):
    assert client.get("/s/nope").status_code == 404


def test_share_create_and_landing_and_download(auth_client, uploaded_file):
    r = auth_client.post(
        "/file/share/",
        data={"id": uploaded_file.id, "ttl_hours": "24", "max_downloads": "2"},
    )
    assert r.status_code == 200
    token = r.get_json()["token"]
    landing = auth_client.get(f"/s/{token}")
    assert landing.status_code == 200
    assert b"fruits.txt" in landing.data
    dl = auth_client.get(f"/s/{token}/download")
    assert dl.status_code == 200
    assert dl.data.startswith(b"Apples")
    assert "attachment" in dl.headers["Content-Disposition"]
    link = ShareLink.query.first()
    assert link.download_count == 1


def test_share_max_downloads_blocks(auth_client, uploaded_file):
    r = auth_client.post(
        "/file/share/",
        data={"id": uploaded_file.id, "max_downloads": "1"},
    )
    token = r.get_json()["token"]
    auth_client.get(f"/s/{token}/download")
    blocked = auth_client.get(f"/s/{token}/download")
    assert blocked.status_code == 404


def test_share_revoke(auth_client, uploaded_file):
    r = auth_client.post(
        "/file/share/", data={"id": uploaded_file.id}
    )
    link_id = ShareLink.query.first().id
    r = auth_client.post(f"/file/share/{link_id}/revoke")
    assert r.status_code == 200
    assert ShareLink.query.first().revoked_at is not None


def test_share_revoke_404_for_other_workspace(auth_client, uploaded_file, db):
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    from filenergy.services import workspaces

    other_ws = workspaces.ensure_default_for(other)
    foreign_file = File(
        user_id=other.id, workspace_id=other_ws.id,
        name="x", path="/x", url="zz",
    )
    db.session.add(foreign_file)
    db.session.commit()
    foreign_link = ShareLink(
        file_id=foreign_file.id, token="ft", created_by_id=other.id,
    )
    db.session.add(foreign_link)
    db.session.commit()
    r = auth_client.post(f"/file/share/{foreign_link.id}/revoke")
    assert r.status_code == 404


def test_share_create_unknown_file(auth_client):
    r = auth_client.post("/file/share/", data={"id": 9999})
    assert r.status_code == 404


# ---- /api/v1 ----


@pytest.fixture
def api_token(workspace, user, app):
    with app.test_request_context():
        _, token = api_keys.mint(workspace, user, "test")
    return token


def test_api_health(client):
    assert client.get("/api/v1/health").status_code == 200


def test_api_requires_token(client):
    r = client.post("/api/v1/ask", json={"question": "x"})
    assert r.status_code == 401
    r = client.get("/api/v1/files")
    assert r.status_code == 401


def test_api_invalid_token(client):
    r = client.post(
        "/api/v1/ask",
        json={"question": "x"},
        headers={"Authorization": "Bearer fk_garbage"},
    )
    assert r.status_code == 401


def test_api_list_files(client, api_token, uploaded_file):
    r = client.get(
        "/api/v1/files",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert r.get_json()["files"][0]["name"] == "fruits.txt"


def test_api_upload(client, api_token, workspace):
    r = client.post(
        "/api/v1/files",
        data={"files[]": (io.BytesIO(b"hi from api"), "api.txt")},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert b"api.txt" in r.data
    f = File.query.filter_by(workspace_id=workspace.id).one()
    assert f.indexed_at is not None


def test_api_ask(client, api_token, uploaded_file, workspace):
    r = client.post(
        "/api/v1/ask",
        json={"question": "What about apples?"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["answer"]
    assert payload["conversation_id"]


def test_api_ask_empty_question_400(client, api_token):
    r = client.post(
        "/api/v1/ask",
        json={"question": ""},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 400


def test_api_ask_unconfigured_503(client, api_token, monkeypatch):
    from filenergy.services import chat as chat_mod

    monkeypatch.setattr(chat_mod, "is_configured", lambda: False)
    r = client.post(
        "/api/v1/ask",
        json={"question": "x"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 503


def test_api_ask_quota_exceeded(client, api_token, monkeypatch):
    monkeypatch.setitem(cfg.PLAN_LIMITS["free"], "asks_per_month", 0)
    r = client.post(
        "/api/v1/ask",
        json={"question": "x"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 402


def test_api_upload_quota_exceeded(client, api_token, monkeypatch):
    monkeypatch.setitem(cfg.PLAN_LIMITS["free"], "files_max", 0)
    r = client.post(
        "/api/v1/files",
        data={"files[]": (io.BytesIO(b"x"), "x.txt")},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 402


def test_api_token_via_x_api_key_header(client, api_token, uploaded_file):
    r = client.get(
        "/api/v1/files", headers={"X-API-Key": api_token}
    )
    assert r.status_code == 200


def test_api_ask_invalid_conversation_id(client, api_token, uploaded_file):
    r = client.post(
        "/api/v1/ask",
        json={"question": "x", "conversation_id": "abc"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200


def test_api_ask_failure_returns_500(client, api_token, uploaded_file, monkeypatch):
    from filenergy.services import chat as chat_mod

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(chat_mod, "answer_question", boom)
    r = client.post(
        "/api/v1/ask",
        json={"question": "x"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 500


def test_api_ask_chat_unavailable_returns_503(client, api_token, uploaded_file, monkeypatch):
    from filenergy.services import chat as chat_mod

    def boom(*a, **k):
        raise chat_mod.ChatUnavailable("api key missing")

    monkeypatch.setattr(chat_mod, "answer_question", boom)
    r = client.post(
        "/api/v1/ask",
        json={"question": "x"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 503


# ---- views/file new branches ----


def test_upload_quota_exceeded_returns_402(auth_client, monkeypatch):
    monkeypatch.setitem(cfg.PLAN_LIMITS["free"], "files_max", 0)
    r = auth_client.post(
        "/file/upload/",
        data={"files[]": (io.BytesIO(b"x"), "x.txt")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 402


def test_ask_quota_exceeded_returns_402(auth_client, uploaded_file, monkeypatch):
    monkeypatch.setitem(cfg.PLAN_LIMITS["free"], "asks_per_month", 0)
    r = auth_client.post("/ask/", json={"question": "?"})
    assert r.status_code == 402


def test_ask_stream_quota_exceeded_returns_402(auth_client, uploaded_file, monkeypatch):
    monkeypatch.setitem(cfg.PLAN_LIMITS["free"], "asks_per_month", 0)
    r = auth_client.post("/ask/stream", json={"question": "?"})
    assert r.status_code == 402


def test_download_post_blocked_for_other_workspace(client, db, uploaded_file, user):
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    from filenergy.services import workspaces

    workspaces.ensure_default_for(other)
    client.get("/user/logout/")
    client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = client.get(f"/file/download/?h={uploaded_file.url}")
    assert r.status_code == 403


# ---- billing webhook ----


def test_stripe_webhook_returns_503_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(cfg, "STRIPE_SECRET_KEY", "")
    r = client.post("/webhooks/stripe", data=b"x")
    assert r.status_code == 503


def test_stripe_webhook_handles_event(client, monkeypatch, workspace, db):
    workspace.stripe_customer_id = "cus_1"
    db.session.commit()
    fake = types.ModuleType("stripe")
    fake.api_key = None
    fake.Webhook = type("W", (), {"construct_event": staticmethod(
        lambda payload, sig, secret: {
            "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_1", "status": "active"}},
        }
    )})
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setattr(cfg, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    r = client.post(
        "/webhooks/stripe", data=b"raw",
        headers={"Stripe-Signature": "sig"},
    )
    assert r.status_code == 200
    assert r.get_json()["handled"] is True


def test_stripe_webhook_bad_signature(client, monkeypatch):
    fake = types.ModuleType("stripe")
    fake.api_key = None

    class _Boom:
        @staticmethod
        def construct_event(payload, sig, secret):
            raise ValueError("bad sig")

    fake.Webhook = _Boom
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setattr(cfg, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    r = client.post(
        "/webhooks/stripe", data=b"raw",
        headers={"Stripe-Signature": "sig"},
    )
    assert r.status_code == 400
