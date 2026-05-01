"""Tier-3 feature tests:

- Workspace + user data export ZIP bundles.
- Workspace-wide 2FA enforcement (require_2fa policy + middleware).
- Weekly email digest builder + scheduler hook.
- WebAuthn / passkey registration as a 2FA factor.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta

import pytest


# ---------- Workspace data export ----------------------------------------


def test_workspace_export_zip_contains_all_artifacts(
    auth_client, db, user, workspace, uploaded_file,
):
    """Export ZIP carries every workspace artifact: files, conversations,
    members, events, metadata."""
    from filenergy.services import exporting

    # Add a conversation + message so the export has something to bundle.
    from filenergy.models import Conversation, Message
    conv = Conversation(
        user_id=user.id, workspace_id=workspace.id, title="Hello world",
    )
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(
        conversation_id=conv.id, role="user", content="What is X?",
    ))
    db.session.add(Message(
        conversation_id=conv.id, role="assistant", content="X is Y.",
    ))
    db.session.commit()

    payload = exporting.workspace_zip(workspace)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = set(zf.namelist())
    assert "workspace.json" in names
    assert "members.csv" in names
    assert "files.json" in names
    assert "conversations.json" in names
    assert "events.csv" in names
    assert f"conversations/{conv.id}.md" in names
    # The uploaded file's bytes should be present too.
    assert any(n.startswith(f"files/{uploaded_file.id}_") for n in names)

    meta = json.loads(zf.read("workspace.json"))
    assert meta["id"] == workspace.id
    assert meta["name"] == workspace.name


def test_workspace_export_endpoint_serves_zip(auth_client, workspace):
    r = auth_client.get("/w/export")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    assert f"{workspace.slug}.zip" in r.headers.get("Content-Disposition", "")
    # First 4 bytes are the zip magic.
    assert r.data[:4] == b"PK\x03\x04"


def test_workspace_export_forbidden_for_non_owner_admin(
    client, db, user, workspace,
):
    """Plain members shouldn't be able to download the workspace dump."""
    from filenergy.models import User, WorkspaceMember
    from filenergy.services import workspaces

    bob = User(email="bob@example.com", username="bob@example.com")
    bob.set_password("password")
    db.session.add(bob)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=bob.id, role="member",
    ))
    db.session.commit()

    client.post("/user/login/", data={"email": bob.email, "password": "password"})
    # Switch into Alice's workspace so the export endpoint sees it.
    client.post(f"/w/switch/{workspace.id}")
    r = client.get("/w/export")
    assert r.status_code == 403


def test_user_export_zip_bundles_owned_workspace(
    auth_client, user, workspace, uploaded_file,
):
    from filenergy.services import exporting

    payload = exporting.user_zip(user)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = set(zf.namelist())
    assert "user.json" in names
    assert "workspaces.json" in names
    assert "api_keys.json" in names
    assert "events.csv" in names
    # Owned workspaces are nested as their own zips.
    assert any(n.startswith("workspaces/") and n.endswith(".zip") for n in names)


def test_user_export_endpoint_serves_zip(auth_client, user):
    r = auth_client.get("/settings/account/export")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    assert f"filenergy-{user.id}.zip" in r.headers.get("Content-Disposition", "")


# ---------- Workspace policy: require_2fa --------------------------------


def test_owner_can_toggle_require_2fa(auth_client, db, workspace):
    r = auth_client.post("/w/policy", data={"require_2fa": "1"})
    assert r.status_code == 302
    db.session.refresh(workspace)
    assert workspace.require_2fa is True

    r = auth_client.post("/w/policy", data={})
    assert r.status_code == 302
    db.session.refresh(workspace)
    assert workspace.require_2fa is False


def test_member_cannot_toggle_workspace_policy(client, db, user, workspace):
    """Only the owner role may set workspace policy."""
    from filenergy.models import User, WorkspaceMember

    bob = User(email="bob@example.com", username="bob@example.com")
    bob.set_password("password")
    db.session.add(bob)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=bob.id, role="admin",
    ))
    db.session.commit()

    client.post("/user/login/", data={"email": bob.email, "password": "password"})
    client.post(f"/w/switch/{workspace.id}")
    r = client.post("/w/policy", data={"require_2fa": "1"})
    assert r.status_code == 403


def test_require_2fa_redirects_users_without_second_factor(
    auth_client, db, workspace,
):
    """A user with no TOTP and no passkey is bounced to the security page."""
    workspace.require_2fa = True
    db.session.commit()

    r = auth_client.get("/file/list/")
    assert r.status_code == 302
    assert "/settings/security" in r.headers["Location"]


def test_require_2fa_lets_through_users_with_totp(
    auth_client, db, workspace, user,
):
    from filenergy.services import totp

    workspace.require_2fa = True
    # Stub: pretend TOTP is enabled.
    user.totp_secret = "JBSWY3DPEHPK3PXP"
    from filenergy.models import utcnow
    user.totp_enabled_at = utcnow()
    db.session.commit()

    r = auth_client.get("/file/list/")
    assert r.status_code == 200


def test_require_2fa_lets_through_users_with_webauthn(
    auth_client, db, workspace, user,
):
    from filenergy.services import webauthn

    workspace.require_2fa = True
    db.session.commit()
    webauthn.register_stub(user, label="YubiKey")

    r = auth_client.get("/file/list/")
    assert r.status_code == 200


def test_require_2fa_security_page_still_reachable(auth_client, db, workspace):
    """Users must be able to get to the page that lets them enroll."""
    workspace.require_2fa = True
    db.session.commit()

    r = auth_client.get("/settings/security")
    assert r.status_code == 200


# ---------- Weekly digest -------------------------------------------------


def test_digest_skipped_when_no_activity(db, user, workspace):
    from filenergy.services import digest

    assert digest.build_digest(user, workspace) is None


def test_digest_renders_summary_when_activity_present(
    db, user, workspace, uploaded_file,
):
    from filenergy.services import digest

    rendered = digest.build_digest(user, workspace)
    assert rendered is not None
    subject, body = rendered
    assert workspace.name in subject
    # The new file should show up as one upload.
    assert "1 new file uploaded" in body


def test_users_due_respects_opt_out(db, user):
    from filenergy.services import digest

    user.weekly_digest = False
    db.session.commit()

    due = digest.users_due()
    assert user not in due


def test_users_due_respects_recent_send(db, user):
    from filenergy.models import utcnow
    from filenergy.services import digest

    user.last_digest_sent_at = utcnow()
    db.session.commit()

    due = digest.users_due()
    assert user not in due


def test_send_pending_uses_email_service(
    monkeypatch, db, user, workspace, uploaded_file,
):
    from filenergy.services import digest, email as email_service

    sent = []

    def fake_send(to, subject, body):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(email_service, "send", fake_send)

    n = digest.send_pending()
    assert n == 1
    assert sent[0][0] == user.email

    # Second call shouldn't double-send (last_digest_sent_at was just set).
    n2 = digest.send_pending()
    assert n2 == 0


def test_notifications_endpoint_persists_choice(auth_client, db, user):
    r = auth_client.post("/settings/notifications", data={})
    assert r.status_code == 302
    db.session.refresh(user)
    assert user.weekly_digest is False

    r = auth_client.post("/settings/notifications", data={"weekly_digest": "1"})
    assert r.status_code == 302
    db.session.refresh(user)
    assert user.weekly_digest is True


# ---------- WebAuthn ------------------------------------------------------


def test_register_stub_creates_credential(db, user):
    from filenergy.services import webauthn

    cred = webauthn.register_stub(user, label="YubiKey 5")
    assert cred.id
    assert cred.user_id == user.id
    assert cred.label == "YubiKey 5"
    assert webauthn.has_credential(user) is True


def test_register_endpoint_creates_credential(auth_client, db, user):
    r = auth_client.post(
        "/settings/security/webauthn",
        data={"label": "Macbook Touch ID"},
    )
    assert r.status_code == 302
    from filenergy.models import WebAuthnCredential
    creds = WebAuthnCredential.query.filter_by(user_id=user.id).all()
    assert len(creds) == 1
    assert creds[0].label == "Macbook Touch ID"


def test_delete_endpoint_removes_credential(auth_client, db, user):
    from filenergy.services import webauthn

    cred = webauthn.register_stub(user, label="Backup key")
    r = auth_client.post(f"/settings/security/webauthn/{cred.id}/delete")
    assert r.status_code == 302
    assert webauthn.has_credential(user) is False


def test_verify_assertion_stub_increments_sign_count(db, user):
    from filenergy.services import webauthn

    cred = webauthn.register_stub(user, label="Key")
    assert webauthn.verify_assertion_stub(user, cred.credential_id) is True
    db.session.refresh(cred)
    assert cred.sign_count == 1


def test_verify_assertion_stub_rejects_unknown(db, user):
    from filenergy.services import webauthn

    assert webauthn.verify_assertion_stub(user, "nonexistent") is False


def test_register_stub_disabled_via_env(monkeypatch, db, user):
    from filenergy.services import webauthn

    monkeypatch.setenv("FILENERGY_WEBAUTHN_DISABLED", "true")
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.register_stub(user, label="X")


def test_login_with_webauthn_routes_through_2fa(client, db, user):
    """A user with no TOTP but a registered passkey still hits /2fa."""
    from filenergy.services import webauthn

    webauthn.register_stub(user, label="Key")

    r = client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    assert r.status_code == 302
    assert "/user/2fa" in r.headers["Location"]


def test_two_factor_post_accepts_webauthn_credential(client, db, user):
    """Submitting a credential id at /user/2fa logs the user in."""
    from filenergy.services import webauthn

    cred = webauthn.register_stub(user, label="Key")

    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    r = client.post(
        "/user/2fa",
        data={"webauthn_credential_id": cred.credential_id},
    )
    assert r.status_code == 302
    # Landing on / means we logged in successfully.
    assert "/2fa" not in r.headers["Location"]


# ---------- CLI hook ------------------------------------------------------


def test_send_digests_cli_runs(monkeypatch, app, db, user, workspace, uploaded_file):
    from filenergy.services import email as email_service

    monkeypatch.setattr(email_service, "send", lambda to, subject, body: True)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["send-digests"])
    assert result.exit_code == 0
    assert "sent" in result.output


def test_send_pending_skips_when_email_fails(
    monkeypatch, db, user, workspace, uploaded_file,
):
    """A failing send should leave last_digest_sent_at untouched so the
    next cron tick will retry."""
    from filenergy.services import digest, email as email_service

    monkeypatch.setattr(email_service, "send", lambda to, subject, body: False)
    n = digest.send_pending()
    assert n == 0
    db.session.refresh(user)
    assert user.last_digest_sent_at is None


def test_digest_skips_user_with_no_workspace(monkeypatch, db, app):
    """A user with no membership row shouldn't crash send_pending."""
    from filenergy.models import User
    from filenergy.services import digest, email as email_service

    orphan = User(email="orphan@example.com", username="orphan@example.com")
    orphan.set_password("p")
    db.session.add(orphan)
    db.session.commit()

    monkeypatch.setattr(email_service, "send", lambda **k: True)
    n = digest.send_pending()
    assert n == 0


def test_webauthn_begin_registration_returns_options(client, db, user):
    """The full FIDO2 ceremony emits PublicKeyCredentialCreationOptions
    that the browser feeds to navigator.credentials.create()."""
    from filenergy.services import webauthn

    # begin_* uses Flask session, so wrap in a request context.
    with client.session_transaction():
        pass
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    r = client.post("/settings/security/webauthn/begin")
    assert r.status_code == 200
    options = r.get_json()
    # Required by the WebAuthn spec.
    assert "challenge" in options
    assert "rp" in options
    assert "user" in options
    assert "pubKeyCredParams" in options


def test_webauthn_begin_authentication_requires_credential(db, user):
    """No credentials registered → can't begin auth."""
    from filenergy.services import webauthn

    with pytest.raises(webauthn.WebAuthnError):
        webauthn.begin_authentication(user)


def test_webauthn_complete_authentication_rejects_unknown_credential(
    monkeypatch, app, db, user,
):
    """A response carrying an id we don't have on file is rejected."""
    from flask import session
    from filenergy.services import webauthn

    with app.test_request_context():
        session[webauthn._AUTH_CHALLENGE_KEY] = "abc"
        ok = webauthn.complete_authentication(
            user, response={"id": "unknown-id"}
        )
        assert ok is False


def test_webauthn_complete_authentication_rejects_stub_credential(
    monkeypatch, app, db, user,
):
    """Stub credentials (public_key='stub') can't pass real verification."""
    from flask import session
    from filenergy.services import webauthn

    cred = webauthn.register_stub(user, label="stub")
    with app.test_request_context():
        session[webauthn._AUTH_CHALLENGE_KEY] = "abc"
        ok = webauthn.complete_authentication(
            user, response={"id": cred.credential_id}
        )
        assert ok is False


def test_webauthn_delete_unknown_returns_false(db, user):
    from filenergy.services import webauthn

    assert webauthn.delete(user, 999_999) is False


def test_workspace_export_handles_missing_file_bytes(
    db, user, workspace, uploaded_file,
):
    """Files whose path no longer exists on disk shouldn't break the export."""
    import os
    from filenergy.services import exporting

    if uploaded_file.path and os.path.isfile(uploaded_file.path):
        os.remove(uploaded_file.path)

    payload = exporting.workspace_zip(workspace)
    zf = zipfile.ZipFile(io.BytesIO(payload))
    # Metadata still made it.
    files_meta = json.loads(zf.read("files.json"))
    assert any(f["id"] == uploaded_file.id for f in files_meta)
