"""HTTP tests for round-7 views."""
import json

import pytest

from filenergy.models import ConnectorAccount, User, WorkspaceMember


# ---- Conversation export endpoints ----


def test_export_pdf(auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]
    pdf = auth_client.get(f"/ask/c/{cid}/export.pdf")
    assert pdf.status_code == 200
    assert pdf.mimetype == "application/pdf"
    assert pdf.data.startswith(b"%PDF")


def test_export_pdf_404_other_workspace(auth_client, uploaded_file, db, client):
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    auth_client.get("/user/logout/")
    auth_client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = auth_client.get(f"/ask/c/{cid}/export.pdf")
    assert r.status_code == 404


def test_export_docx(auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]
    docx = auth_client.get(f"/ask/c/{cid}/export.docx")
    assert docx.status_code == 200
    assert docx.data[:2] == b"PK"


def test_export_docx_404_other_workspace(auth_client, uploaded_file, db):
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    auth_client.get("/user/logout/")
    auth_client.post("/user/login/", data={"email": "o@o", "password": "p"})
    assert auth_client.get(f"/ask/c/{cid}/export.docx").status_code == 404


def test_export_pdf_unavailable_returns_503(auth_client, uploaded_file, monkeypatch):
    from filenergy.services import exporting

    def boom(conv):
        raise exporting.ExportUnavailable("missing fpdf2")

    monkeypatch.setattr(exporting, "to_pdf", boom)
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]
    assert auth_client.get(f"/ask/c/{cid}/export.pdf").status_code == 503


def test_export_docx_unavailable_returns_503(auth_client, uploaded_file, monkeypatch):
    from filenergy.services import exporting

    def boom(conv):
        raise exporting.ExportUnavailable("missing python-docx")

    monkeypatch.setattr(exporting, "to_docx", boom)
    r = auth_client.post("/ask/", json={"question": "?"})
    cid = r.get_json()["conversation_id"]
    assert auth_client.get(f"/ask/c/{cid}/export.docx").status_code == 503


# ---- /connectors ----


def test_connectors_index_loads(auth_client):
    r = auth_client.get("/connectors/")
    assert r.status_code == 200
    assert b"Google Drive" in r.data


def test_connect_unconfigured_redirects(auth_client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    r = auth_client.get("/connectors/google_drive/connect", follow_redirects=False)
    assert r.status_code == 302


def test_connect_unknown_kind_404(auth_client):
    assert auth_client.get("/connectors/garbage/connect").status_code == 404


def test_connect_redirects_to_google(auth_client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    r = auth_client.get("/connectors/google_drive/connect", follow_redirects=False)
    assert r.status_code == 302
    assert "accounts.google.com" in r.headers["Location"]


def test_connect_requires_admin(client, db, user, workspace):
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
    r = client.get("/connectors/google_drive/connect")
    assert r.status_code == 403


def test_callback_missing_code(auth_client):
    r = auth_client.get("/connectors/google_drive/callback?state=1",
                        follow_redirects=False)
    assert r.status_code == 302  # flash + redirect


def test_callback_unknown_kind_404(auth_client):
    assert auth_client.get(
        "/connectors/garbage/callback?code=c&state=1"
    ).status_code == 404


def test_callback_handles_provider_error(auth_client, monkeypatch, workspace):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    from filenergy.services import connectors as conn_mod

    def boom(*a, **k):
        raise conn_mod.ConnectorError("provider died")

    monkeypatch.setattr(
        conn_mod._CONNECTORS["google_drive"], "complete_oauth", boom
    )
    r = auth_client.get(
        f"/connectors/google_drive/callback?code=C&state={workspace.id}",
        follow_redirects=False,
    )
    assert r.status_code == 302


def test_callback_persists_account(auth_client, monkeypatch, workspace):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")

    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=None):
                if "/token" in req.full_url:
                    return json.dumps({"access_token": "t", "expires_in": 60}).encode()
                return json.dumps({"email": "alice@gmail.com"}).encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    r = auth_client.get(
        f"/connectors/google_drive/callback?code=C&state={workspace.id}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert ConnectorAccount.query.filter_by(workspace_id=workspace.id).one()


def test_sync_404(auth_client):
    assert auth_client.post("/connectors/accounts/9999/sync").status_code == 404


def test_sync_handles_failure(auth_client, db, workspace, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
    )
    db.session.add(a)
    db.session.commit()

    def boom(*args, **kwargs):
        raise OSError("oops")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = auth_client.post(
        f"/connectors/accounts/{a.id}/sync", follow_redirects=False
    )
    assert r.status_code == 302  # flash + redirect to /connectors/
    db.session.refresh(a)
    assert a.last_error


def test_sync_succeeds(auth_client, db, workspace, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
    )
    db.session.add(a)
    db.session.commit()

    def fake(req, timeout=None):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=None):
                return json.dumps({"files": []}).encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    r = auth_client.post(
        f"/connectors/accounts/{a.id}/sync", follow_redirects=False
    )
    assert r.status_code == 302


def test_disconnect(auth_client, db, workspace):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
    )
    db.session.add(a)
    db.session.commit()
    r = auth_client.post(
        f"/connectors/accounts/{a.id}/disconnect", follow_redirects=False
    )
    assert r.status_code == 302
    assert ConnectorAccount.query.count() == 0


def test_disconnect_404(auth_client):
    assert auth_client.post(
        "/connectors/accounts/9999/disconnect"
    ).status_code == 404


def test_disconnect_requires_admin(client, db, user, workspace):
    other = User(email="m@m", username="m")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    db.session.add(WorkspaceMember(
        workspace_id=workspace.id, user_id=other.id, role="member"
    ))
    db.session.commit()
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
    )
    db.session.add(a)
    db.session.commit()
    client.post("/user/login/", data={"email": "m@m", "password": "pw"})
    client.post(f"/w/switch/{workspace.id}")
    r = client.post(f"/connectors/accounts/{a.id}/disconnect")
    assert r.status_code == 403


# ---- SAML stub ----


def test_saml_status(client):
    r = client.get("/saml/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is False
    assert "implementation" in body


def test_saml_login_unconfigured(client, monkeypatch):
    monkeypatch.delenv("SAML_ENABLED", raising=False)
    r = client.get("/saml/login")
    assert r.status_code == 503


def test_saml_login_returns_501_when_stub(client, monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp/x")
    r = client.get("/saml/login")
    assert r.status_code == 501


def test_saml_acs_unconfigured(client, monkeypatch):
    monkeypatch.delenv("SAML_ENABLED", raising=False)
    r = client.post("/saml/acs", data={"SAMLResponse": "x"})
    assert r.status_code == 503


def test_saml_acs_stub_returns_501(client, monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://idp/x")
    r = client.post("/saml/acs", data={"SAMLResponse": "x"})
    assert r.status_code == 501


def test_saml_init_request_raises():
    """Direct unit-level: stub raises NotImplementedError."""
    from filenergy.services import saml_sso
    with pytest.raises(NotImplementedError):
        saml_sso.init_request(redirect_uri="x")
    with pytest.raises(NotImplementedError):
        saml_sso.process_response({})
