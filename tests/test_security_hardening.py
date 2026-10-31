"""Regression tests for the October 2026 security review.

1. Uploads were stored at `<UPLOAD_DIR>/<original name>`, shared by every
   account: one user could overwrite, read or delete another's file.
2. `/user/2fa` accepted a bare WebAuthn credential id as the second factor.
3. API keys outlived their owner's membership and weren't capped by role;
   any member could mint admin keys and invite admins.
4. The public download handle was sha256(id), enumerable.
"""
from __future__ import annotations

import hashlib
import io
import json
import os

import pytest

from filenergy import settings
from filenergy.models import ApiKey, File, User, WorkspaceMember


# ---------- helpers -------------------------------------------------------


def _make_user(db, email, password="password"):
    from filenergy.services import workspaces

    u = User(email=email, username=email)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    workspaces.ensure_default_for(u)
    return u


def _login(client, email, password="password"):
    return client.post(
        "/user/login/", data={"email": email, "password": password},
    )


def _as(client, user, workspace=None):
    """Switch the (single) test client to `user`. The fixture keeps one app
    context open, so flask-login's cached user lives in a shared `g`: two
    clients at once would see each other's login."""
    client.get("/user/logout/")
    _login(client, user.email)
    if workspace is not None:
        r = client.post(f"/w/switch/{workspace.id}")
        assert r.status_code == 302


def _upload(client, name, data):
    return client.post(
        "/file/upload/",
        data={"files[]": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
    )


def _add_member(db, workspace, user, role="member"):
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role=role,
    ))
    db.session.commit()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- 1. per-upload storage -----------------------------------------


def test_same_name_uploads_do_not_share_a_file(client, db):
    from filenergy.services import workspaces

    alice = _make_user(db, "alice@x.test")
    mallory = _make_user(db, "mallory@x.test")
    a_ws = workspaces.list_for_user(alice)[0]
    m_ws = workspaces.list_for_user(mallory)[0]

    _as(client, alice)
    assert _upload(client, "report.txt", b"alice's numbers").status_code == 200
    # Mallory uploads the same name afterwards: it used to overwrite Alice's
    # bytes and hand them to Mallory's row.
    _as(client, mallory)
    assert _upload(client, "report.txt", b"mallory's junk").status_code == 200

    fa = File.query.filter_by(workspace_id=a_ws.id).one()
    fm = File.query.filter_by(workspace_id=m_ws.id).one()
    assert fa.name == fm.name == "report.txt"
    assert fa.path != fm.path
    upload_dir = os.path.realpath(settings.UPLOAD_DIR)
    for f, ws in ((fa, a_ws), (fm, m_ws)):
        real = os.path.realpath(f.path)
        assert real.startswith(os.path.join(upload_dir, str(ws.id)) + os.sep)
        assert "report" not in os.path.basename(f.path)
        assert f.path.endswith(".txt")  # extension kept for extraction

    r = client.get(f"/file/downloadnow/?h={fm.url}")
    assert r.status_code == 200 and r.data == b"mallory's junk"
    _as(client, alice)
    r = client.get(f"/file/downloadnow/?h={fa.url}")
    assert r.status_code == 200 and r.data == b"alice's numbers"
    assert fa.text_content == "alice's numbers"


def test_api_delete_only_removes_own_file(client, db):
    from filenergy.services import api_keys, workspaces

    alice = _make_user(db, "alice@x.test")
    mallory = _make_user(db, "mallory@x.test")
    a_ws = workspaces.list_for_user(alice)[0]
    m_ws = workspaces.list_for_user(mallory)[0]
    _as(client, alice)
    _upload(client, "contract.txt", b"secret terms")
    fa = File.query.filter_by(workspace_id=a_ws.id).one()
    client.get("/user/logout/")

    _, token = api_keys.mint(m_ws, mallory, "k")
    r = client.post(
        "/api/v1/files",
        data={"files[]": (io.BytesIO(b"x"), "contract.txt")},
        content_type="multipart/form-data",
        headers=_bearer(token),
    )
    assert r.status_code == 200
    mid = json.loads(r.data)[0]["id"]
    r = client.delete(f"/api/v1/files/{mid}", headers=_bearer(token))
    assert r.status_code == 200

    assert os.path.exists(fa.path)
    _as(client, alice)
    r = client.get(f"/file/downloadnow/?h={fa.url}")
    assert r.status_code == 200 and r.data == b"secret terms"


def test_hostile_names_stay_inside_the_workspace_dir(db, workspace):
    from filenergy.services.file import storage_path_for

    base = os.path.realpath(os.path.join(settings.UPLOAD_DIR, str(workspace.id)))
    for name in ("../../etc/passwd", "..", "", "a.PDF", "x.ph p", "y.tar.gz/.."):
        p = os.path.realpath(storage_path_for(workspace.id, name))
        assert os.path.dirname(p) == base, name
    assert storage_path_for(workspace.id, "a.PDF").endswith(".pdf")
    assert storage_path_for(workspace.id, "x.ph p")[-5:].count(".") == 0


def test_legacy_rows_sharing_a_path_keep_the_file_until_the_last(db, user, workspace):
    """Rows from before the fix can point at one shared file; deleting one
    of them must not pull the bytes from under the other."""
    from filenergy.services.file import FileService

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    legacy = os.path.join(settings.UPLOAD_DIR, "notes.txt")
    with open(legacy, "wb") as fd:
        fd.write(b"shared")
    rows = [
        File(user_id=user.id, workspace_id=workspace.id, name="notes.txt",
             path=legacy, url=f"legacy-{i}")
        for i in range(2)
    ]
    db.session.add_all(rows)
    db.session.commit()

    svc = FileService()
    assert svc.delete(rows[0]) is True
    assert os.path.exists(legacy)
    assert svc.get_content(rows[1]) == b"shared"
    assert svc.delete(rows[1]) is True
    assert not os.path.exists(legacy)


def test_ingested_blobs_get_their_own_file(db, user, workspace):
    from filenergy.services import ingestion
    from filenergy.services.file import FileService

    other = _make_user(db, "bob@x.test")
    from filenergy.services import workspaces
    other_ws = workspaces.list_for_user(other)[0]

    f1 = ingestion.materialize_blob(
        user=user, workspace=workspace, name="page.html", content=b"<p>hi</p>",
    )
    f2 = ingestion.materialize_blob(
        user=other, workspace=other_ws, name="page.html", content=b"<p>hi</p>",
    )
    assert f1.path != f2.path
    FileService().delete(f2)
    assert os.path.exists(f1.path)


def test_download_header_survives_hostile_names(auth_client, db, workspace):
    _upload(auth_client, "report.txt", b"data")
    f = File.query.filter_by(workspace_id=workspace.id).one()
    f.name = 'a"b\r\nX-Evil: 1\\ñ.txt'
    db.session.commit()
    r = auth_client.get(f"/file/downloadnow/?h={f.url}")
    assert r.status_code == 200
    cd = r.headers["Content-Disposition"]
    assert "\n" not in cd and "\r" not in cd
    assert "X-Evil" not in r.headers
    assert cd.startswith('attachment; filename="a_b__X-Evil: 1__.txt"')
    assert "filename*=UTF-8''" in cd


# ---------- 4. random download handle -------------------------------------


def test_url_handle_is_random(auth_client, db, workspace):
    _upload(auth_client, "a.txt", b"1")
    _upload(auth_client, "b.txt", b"2")
    for f in File.query.filter_by(workspace_id=workspace.id).all():
        assert f.url != hashlib.sha256(str(f.id).encode()).hexdigest()
        assert len(f.url) == 43


def test_rotate_legacy_urls(db, user, workspace):
    from filenergy.services.file import rotate_legacy_urls

    legacy = File(user_id=user.id, workspace_id=workspace.id, name="old",
                  path="/nope", url="placeholder")
    keep = File(user_id=user.id, workspace_id=workspace.id, name="new",
                path="/nope", url="x" * 64)
    db.session.add_all([legacy, keep])
    db.session.commit()
    legacy.url = hashlib.sha256(str(legacy.id).encode()).hexdigest()
    db.session.commit()

    assert rotate_legacy_urls() == 1
    db.session.refresh(legacy)
    db.session.refresh(keep)
    assert legacy.url != hashlib.sha256(str(legacy.id).encode()).hexdigest()
    assert len(legacy.url) == 43
    assert keep.url == "x" * 64
    assert rotate_legacy_urls() == 0


# ---------- 2. second factor ----------------------------------------------


def _pending_2fa(client, user):
    r = _login(client, user.email)
    assert "/user/2fa" in r.headers["Location"]


def _begin(client):
    r = client.post("/user/2fa/webauthn/begin")
    assert r.status_code == 200
    return r.get_json()["challenge"]


def _logged_in(client):
    return client.get("/file/list/").status_code == 200


def test_passkey_login_verifies_a_real_assertion(client, db, user, passkey):
    cred = passkey.enroll(db, user)
    _pending_2fa(client, user)
    challenge = _begin(client)
    r = client.post(
        "/user/2fa/webauthn/complete",
        json={"response": passkey.assertion(challenge)},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["ok"] is True
    assert _logged_in(client)
    db.session.refresh(cred)
    assert cred.sign_count == 1


@pytest.mark.parametrize("tamper", [
    "other_key", "wrong_challenge", "wrong_origin", "wrong_rp", "old_counter",
])
def test_passkey_login_rejects_bad_assertions(client, db, user, passkey, tamper):
    from tests.conftest import SoftPasskey

    cred = passkey.enroll(db, user)
    cred.sign_count = 5
    db.session.commit()
    _pending_2fa(client, user)
    challenge = _begin(client)

    kwargs = {"sign_count": 6}
    signer = passkey
    if tamper == "other_key":
        signer = SoftPasskey()
        signer.credential_id = passkey.credential_id
    elif tamper == "wrong_challenge":
        challenge = challenge[::-1]
    elif tamper == "wrong_origin":
        kwargs["origin"] = "https://evil.example"
    elif tamper == "wrong_rp":
        kwargs["rp_id"] = "evil.example"
    elif tamper == "old_counter":
        kwargs["sign_count"] = 5
    response = signer.assertion(challenge, **kwargs)

    r = client.post("/user/2fa/webauthn/complete", json={"response": response})
    assert r.status_code == 400
    assert not _logged_in(client)


def test_passkey_assertion_cannot_be_replayed(client, db, user, passkey):
    passkey.enroll(db, user)
    _pending_2fa(client, user)
    response = passkey.assertion(_begin(client))
    # The challenge is single-use: a second begin issues a new one and the
    # old assertion no longer matches.
    _begin(client)
    r = client.post("/user/2fa/webauthn/complete", json={"response": response})
    assert r.status_code == 400
    assert not _logged_in(client)


def test_bare_credential_id_is_not_a_second_factor(client, db, user, passkey):
    """The attack: password + the id that begin hands out."""
    passkey.enroll(db, user)
    _pending_2fa(client, user)
    leaked = client.post("/user/2fa/webauthn/begin").get_json()
    cred_id = leaked["allowCredentials"][0]["id"]
    r = client.post("/user/2fa", data={"webauthn_credential_id": cred_id})
    assert "/user/2fa" in r.headers["Location"]
    r = client.post(
        "/user/2fa", data={"webauthn_credential_id": cred_id, "code": ""},
    )
    assert "/user/2fa" in r.headers["Location"]
    assert not _logged_in(client)


def test_totp_still_completes_login_with_a_passkey_enrolled(client, db, user, passkey):
    import pyotp
    from filenergy.models import utcnow

    passkey.enroll(db, user)
    user.totp_secret = pyotp.random_base32()
    user.totp_enabled_at = utcnow()
    db.session.commit()
    _pending_2fa(client, user)
    r = client.post(
        "/user/2fa", data={"code": pyotp.TOTP(user.totp_secret).now()},
    )
    assert "/user/2fa" not in r.headers["Location"]
    assert _logged_in(client)


def test_stub_credential_neither_gates_nor_passes_login(client, db, user):
    from filenergy.services import webauthn

    cred = webauthn.register_stub(user, label="Stub")
    r = _login(client, user.email)
    # No verifiable factor → plain password login (a stub can't sign, so
    # gating on it would only lock the user out).
    assert "/user/2fa" not in r.headers["Location"]
    with client.application.test_request_context():
        from flask import session
        session[webauthn._AUTH_CHALLENGE_KEY] = "abc"
        assert webauthn.complete_authentication(
            user, response={"id": cred.credential_id},
        ) is False


# ---------- 3. API keys and roles ------------------------------------------


@pytest.fixture
def member(db, workspace):
    m = _make_user(db, "member@x.test")
    _add_member(db, workspace, m, "member")
    return m


def test_key_stops_working_when_member_is_removed(client, db, workspace, member):
    from filenergy.services import api_keys, workspaces

    record, token = api_keys.mint(workspace, member, "k")
    assert client.get("/api/v1/files", headers=_bearer(token)).status_code == 200

    assert workspaces.remove_member(workspace, member.id) is True
    db.session.refresh(record)
    assert record.revoked_at is not None
    assert client.get("/api/v1/files", headers=_bearer(token)).status_code == 401


def test_verify_requires_current_membership_even_if_not_revoked(
    client, db, workspace, member,
):
    """Memberships dropped by other paths (old SCIM code, manual SQL)."""
    from filenergy.services import api_keys

    _, token = api_keys.mint(workspace, member, "k")
    WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=member.id,
    ).delete()
    db.session.commit()
    assert api_keys.verify(token) is None
    assert client.get("/api/v1/files", headers=_bearer(token)).status_code == 401


def test_scim_deprovision_revokes_keys(client, monkeypatch, db, workspace, member):
    from filenergy.services import api_keys

    record, _ = api_keys.mint(workspace, member, "k")
    monkeypatch.setenv("FILENERGY_SCIM_TOKEN", "good-token")
    r = client.delete(
        f"/scim/v2/Users/{member.id}",
        headers={
            "Authorization": "Bearer good-token",
            "X-Filenergy-Workspace-Slug": workspace.slug,
        },
    )
    assert r.status_code == 204
    db.session.refresh(record)
    assert record.revoked_at is not None


def test_member_full_access_key_excludes_admin_scopes(db, workspace, member):
    from filenergy.services import api_keys

    record, _ = api_keys.mint(workspace, member, "k")
    assert record.scopes  # stored explicitly, not "full access"
    assert "members:write" not in record.scopes
    assert "events:read" not in record.scopes
    assert "files:write" in record.scopes


def test_member_cannot_mint_admin_scopes(client, db, workspace, member):
    from filenergy.services import api_keys

    with pytest.raises(api_keys.ScopeNotAllowed):
        api_keys.mint(workspace, member, "k", scopes=["members:write"])

    _as(client, member, workspace)
    r = client.post("/settings/keys", data={"name": "x", "scopes": ["members:write"]})
    assert r.status_code == 302
    assert ApiKey.query.filter_by(user_id=member.id).count() == 0


def test_non_member_cannot_mint(db, workspace):
    from filenergy.services import api_keys

    outsider = _make_user(db, "out@x.test")
    with pytest.raises(api_keys.ScopeNotAllowed):
        api_keys.mint(workspace, outsider, "k")


def test_legacy_full_access_key_of_member_cannot_invite(client, db, workspace, member):
    """A key minted before the fix (scopes NULL = full access) is still
    capped by its owner's role."""
    from filenergy.services import api_keys

    _, token = api_keys.mint(workspace, member, "k")
    ApiKey.query.filter_by(user_id=member.id).update({ApiKey.scopes_json: None})
    db.session.commit()

    r = client.post(
        "/api/v1/invitations", json={"email": "x@x.test", "role": "admin"},
        headers=_bearer(token),
    )
    assert r.status_code == 403
    assert client.get("/api/v1/files", headers=_bearer(token)).status_code == 200


def test_demoted_admin_key_loses_admin_scopes(client, db, workspace):
    from filenergy.services import api_keys

    admin = _make_user(db, "admin@x.test")
    _add_member(db, workspace, admin, "admin")
    _, token = api_keys.mint(workspace, admin, "k")
    body = {"email": "new@x.test", "role": "member"}
    r = client.post("/api/v1/invitations", json=body, headers=_bearer(token))
    assert r.status_code == 200

    m = WorkspaceMember.query.filter_by(
        workspace_id=workspace.id, user_id=admin.id,
    ).one()
    m.role = "member"
    db.session.commit()
    r = client.post("/api/v1/invitations", json=body, headers=_bearer(token))
    assert r.status_code == 403


def test_invite_role_ceiling(db, workspace, user, member):
    from filenergy.services import workspaces

    admin = _make_user(db, "admin@x.test")
    _add_member(db, workspace, admin, "admin")

    assert workspaces.invite(workspace, user, "a@x.test", "admin").role == "admin"
    assert workspaces.invite(workspace, admin, "b@x.test", "admin").role == "admin"
    with pytest.raises(workspaces.InvitationNotAllowed):
        workspaces.invite(workspace, member, "c@x.test", "member")
    with pytest.raises(workspaces.InvitationNotAllowed):
        workspaces.invite(workspace, member, "d@x.test", "admin")


def test_from_url_needs_files_write(client, db, workspace, user):
    from filenergy.services import api_keys

    _, token = api_keys.mint(workspace, user, "ro", scopes=["files:read"])
    r = client.post(
        "/file/from_url/", data={"url": "https://example.com/x"},
        headers=_bearer(token),
    )
    assert r.status_code == 403


def test_member_cannot_revoke_someone_elses_key(client, db, workspace, user, member):
    from filenergy.services import api_keys

    owner_key, _ = api_keys.mint(workspace, user, "owner's")
    _as(client, member, workspace)
    client.post(f"/settings/keys/{owner_key.id}/revoke")
    db.session.refresh(owner_key)
    assert owner_key.revoked_at is None

    own, _ = api_keys.mint(workspace, member, "mine")
    client.post(f"/settings/keys/{own.id}/revoke")
    db.session.refresh(own)
    assert own.revoked_at is not None
