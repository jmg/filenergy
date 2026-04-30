"""Tests for the connectors framework + Google Drive reference impl."""
import json
import sys
import types

import pytest

from filenergy.models import ConnectorAccount, File, utcnow
from filenergy.services import connectors


# ---- registry ----


def test_get_returns_known_connector():
    assert connectors.get("google_drive").label == "Google Drive"


def test_get_returns_none_for_unknown():
    assert connectors.get("nope") is None


def test_all_connectors_includes_drive():
    kinds = {c.kind for c in connectors.all_connectors()}
    assert "google_drive" in kinds


def test_list_accounts_isolates_by_workspace(db, workspace, app):
    from filenergy.models import User
    from filenergy.services import workspaces as ws_service

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    other_ws = ws_service.ensure_default_for(other)
    with app.test_request_context():
        a = ConnectorAccount(workspace_id=workspace.id, kind="google_drive",
                             account_label="me@x", access_token="t1")
        b = ConnectorAccount(workspace_id=other_ws.id, kind="google_drive",
                             account_label="them@x", access_token="t2")
        db.session.add_all([a, b])
        db.session.commit()
    assert {x.account_label for x in connectors.list_accounts(workspace)} == {"me@x"}


def test_disconnect_deletes_account(db, workspace, app):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
    )
    db.session.add(a)
    db.session.commit()
    with app.test_request_context():
        connectors.disconnect(a)
    assert ConnectorAccount.query.count() == 0


# ---- Google Drive: configuration ----


def test_drive_is_configured(monkeypatch):
    drive = connectors.get("google_drive")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert drive.is_configured() is False
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    assert drive.is_configured() is True


def test_authorize_url_unconfigured_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    drive = connectors.get("google_drive")
    with pytest.raises(connectors.ConnectorError):
        drive.authorize_url("http://x/cb", workspace_id=1)


def test_authorize_url_includes_state_and_scopes(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "abc")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "def")
    drive = connectors.get("google_drive")
    url = drive.authorize_url("http://x/cb", workspace_id=42)
    assert "state=42" in url
    assert "drive.readonly" in url
    assert "access_type=offline" in url


# ---- complete_oauth ----


def _mock_urlopen(monkeypatch, responses):
    """`responses` is a list of (predicate(req) -> response_body)."""
    calls = []

    class _R:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return self._body if n is None else self._body[:n]

    def fake(req, timeout=None):
        calls.append(req.full_url)
        for predicate, body in responses:
            if predicate(req):
                return _R(body if isinstance(body, bytes) else body.encode())
        raise RuntimeError(f"unexpected URL: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


def test_complete_oauth_persists_account(db, workspace, monkeypatch, app):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")

    _mock_urlopen(monkeypatch, [
        (lambda r: "/token" in r.full_url,
         json.dumps({"access_token": "ya29.x", "refresh_token": "1//y",
                     "expires_in": 3600})),
        (lambda r: "userinfo" in r.full_url,
         json.dumps({"email": "alice@gmail.com"})),
    ])
    drive = connectors.get("google_drive")
    with app.test_request_context():
        account = drive.complete_oauth(
            code="C", state=str(workspace.id), redirect_uri="http://x/cb"
        )
    assert account.access_token == "ya29.x"
    assert account.refresh_token == "1//y"
    assert account.account_label == "alice@gmail.com"
    assert account.expires_at is not None


def test_complete_oauth_rejects_bad_state(monkeypatch, app):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    drive = connectors.get("google_drive")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        drive.complete_oauth(code="C", state="not-a-number",
                              redirect_uri="http://x/cb")


def test_complete_oauth_handles_missing_token(monkeypatch, app, workspace):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    _mock_urlopen(monkeypatch, [
        (lambda r: True, json.dumps({"error": "invalid_grant"})),
    ])
    drive = connectors.get("google_drive")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        drive.complete_oauth(
            code="C", state=str(workspace.id), redirect_uri="http://x/cb"
        )


def test_complete_oauth_unconfigured_raises(monkeypatch, app):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    drive = connectors.get("google_drive")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        drive.complete_oauth(code="C", state="1", redirect_uri="http://x/cb")


# ---- token refresh ----


def test_ensure_fresh_skips_when_token_still_valid(db, workspace, monkeypatch, app):
    from datetime import timedelta
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="still-good", refresh_token="r",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()
    drive = connectors.get("google_drive")
    with app.test_request_context():
        assert drive._ensure_fresh(a) == "still-good"


def test_ensure_fresh_refreshes_when_expired(db, workspace, monkeypatch, app):
    from datetime import timedelta
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="old", refresh_token="r",
        expires_at=utcnow() - timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()
    _mock_urlopen(monkeypatch, [
        (lambda r: "/token" in r.full_url,
         json.dumps({"access_token": "new-token", "expires_in": 3600})),
    ])
    drive = connectors.get("google_drive")
    with app.test_request_context():
        assert drive._ensure_fresh(a) == "new-token"
    db.session.refresh(a)
    assert a.access_token == "new-token"


def test_ensure_fresh_without_refresh_token_returns_current(db, workspace, app):
    from datetime import timedelta
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="just-this", refresh_token=None,
        expires_at=utcnow() - timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()
    drive = connectors.get("google_drive")
    with app.test_request_context():
        assert drive._ensure_fresh(a) == "just-this"


# ---- sync ----


def test_sync_pulls_drive_files(db, user, workspace, monkeypatch, app):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    from datetime import timedelta
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="t", refresh_token="r",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()

    listing = {
        "files": [
            {"id": "f1", "name": "Quarterly Report",
             "mimeType": "application/vnd.google-apps.document"},
            {"id": "f2", "name": "data.csv", "mimeType": "text/csv"},
            {"id": "f3", "name": "movie.mov", "mimeType": "video/quicktime"},
        ]
    }

    _mock_urlopen(monkeypatch, [
        (lambda r: "files?" in r.full_url and "alt" not in r.full_url and "export" not in r.full_url,
         json.dumps(listing)),
        (lambda r: "/export" in r.full_url, b"Exported text"),
        (lambda r: "alt=media" in r.full_url, b"col1,col2\n1,2\n"),
    ])

    drive = connectors.get("google_drive")
    with app.test_request_context():
        result = drive.sync(a, user=user, workspace=workspace)
    assert result["created"] == 2  # gdoc + csv
    assert result["skipped"] == 1  # mov rejected
    files = File.query.filter_by(workspace_id=workspace.id).all()
    names = {f.name for f in files}
    assert "Quarterly-Report.txt" in names
    assert "data.csv" in names
    db.session.refresh(a)
    assert a.last_synced_at is not None


def test_sync_skips_existing_filenames(db, user, workspace, monkeypatch, app):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    from datetime import timedelta
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="t", refresh_token=None,
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.session.add(a)
    # Pre-existing file with the same name.
    existing = File(
        user_id=user.id, workspace_id=workspace.id,
        name="data.csv", path="/tmp/p", url="zzzz",
    )
    db.session.add(existing)
    db.session.commit()

    _mock_urlopen(monkeypatch, [
        (lambda r: "files?" in r.full_url and "alt" not in r.full_url and "export" not in r.full_url,
         json.dumps({"files": [
             {"id": "f", "name": "data.csv", "mimeType": "text/csv"},
         ]})),
    ])
    drive = connectors.get("google_drive")
    with app.test_request_context():
        result = drive.sync(a, user=user, workspace=workspace)
    assert result["created"] == 0
    assert result["skipped"] == 1


def test_sync_handles_blob_fetch_error(db, user, workspace, monkeypatch, app):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    from datetime import timedelta
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="t", refresh_token=None,
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()

    listing = {"files": [
        {"id": "f1", "name": "data.csv", "mimeType": "text/csv"},
    ]}

    def fake(req, timeout=None):
        if "files?" in req.full_url and "alt" not in req.full_url:
            class R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self, n=None): return json.dumps(listing).encode()
            return R()
        raise OSError("network kaput")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    drive = connectors.get("google_drive")
    with app.test_request_context():
        result = drive.sync(a, user=user, workspace=workspace)
    assert result["created"] == 0
    assert result["skipped"] == 1
