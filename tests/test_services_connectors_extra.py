"""Tests for the Notion / Slack / Dropbox connectors."""
import json

import pytest

from filenergy.models import ConnectorAccount, File, utcnow
from filenergy.services import connectors


def _stub(monkeypatch, responses):
    class _R:
        def __init__(self, body):
            self._body = body if isinstance(body, bytes) else body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return self._body if n is None else self._body[:n]

    def fake(req, timeout=None):
        for predicate, body in responses:
            if predicate(req):
                return _R(body)
        raise RuntimeError(f"unexpected URL: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake)


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------


def test_notion_is_configured(monkeypatch):
    notion = connectors.get("notion")
    monkeypatch.delenv("NOTION_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("NOTION_OAUTH_CLIENT_SECRET", raising=False)
    assert notion.is_configured() is False
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_SECRET", "y")
    assert notion.is_configured() is True


def test_notion_authorize_url(monkeypatch):
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_SECRET", "y")
    notion = connectors.get("notion")
    url = notion.authorize_url("http://x/cb", workspace_id=42)
    assert "state=42" in url
    assert "owner=user" in url
    assert "notion.com" in url


def test_notion_authorize_url_unconfigured(monkeypatch):
    monkeypatch.delenv("NOTION_OAUTH_CLIENT_ID", raising=False)
    notion = connectors.get("notion")
    with pytest.raises(connectors.ConnectorError):
        notion.authorize_url("http://x/cb", workspace_id=1)


def test_notion_complete_oauth(monkeypatch, db, workspace, app):
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_SECRET", "y")
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({
            "access_token": "secret_x",
            "workspace_name": "Acme",
        })),
    ])
    notion = connectors.get("notion")
    with app.test_request_context():
        a = notion.complete_oauth(
            code="C", state=str(workspace.id), redirect_uri="http://x/cb"
        )
    assert a.access_token == "secret_x"
    assert a.account_label == "Acme"


def test_notion_complete_oauth_bad_state(monkeypatch, app):
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_SECRET", "y")
    notion = connectors.get("notion")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        notion.complete_oauth(code="c", state="bad", redirect_uri="x")


def test_notion_complete_oauth_unconfigured(monkeypatch, app, workspace):
    monkeypatch.delenv("NOTION_OAUTH_CLIENT_ID", raising=False)
    notion = connectors.get("notion")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        notion.complete_oauth(code="c", state=str(workspace.id), redirect_uri="x")


def test_notion_complete_oauth_no_token(monkeypatch, app, workspace):
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("NOTION_OAUTH_CLIENT_SECRET", "y")
    _stub(monkeypatch, [(lambda r: True, json.dumps({"error": "no"}))])
    notion = connectors.get("notion")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        notion.complete_oauth(
            code="c", state=str(workspace.id), redirect_uri="x"
        )


def test_notion_sync_pulls_pages(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="notion",
        access_token="secret_x", account_label="Acme",
    )
    db.session.add(a)
    db.session.commit()

    pages = {
        "results": [{
            "id": "page-1",
            "properties": {
                "Name": {"type": "title",
                         "title": [{"plain_text": "Q4 Plan"}]},
            },
        }]
    }
    blocks = {
        "results": [
            {"type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "Ship things."}]}},
            {"type": "heading_1",
             "heading_1": {"rich_text": [{"plain_text": "Goals"}]}},
        ]
    }
    _stub(monkeypatch, [
        (lambda r: "search" in r.full_url, json.dumps(pages)),
        (lambda r: "blocks" in r.full_url, json.dumps(blocks)),
    ])

    with app.test_request_context():
        result = connectors.get("notion").sync(a, user=user, workspace=workspace)
    assert result["created"] == 1
    f = File.query.filter_by(workspace_id=workspace.id).one()
    # Spaces are sanitized to dashes in the on-disk filename via materialize_blob.
    assert f.name in ("Q4 Plan.md", "Q4-Plan.md")
    assert b"Ship things" in open(f.path, "rb").read()


def test_notion_sync_skips_existing(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="notion", access_token="t",
    )
    db.session.add(a)
    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="Existing.md", path="/x", url="zzz",
    )
    db.session.add(f)
    db.session.commit()
    pages = {"results": [{
        "id": "p", "properties": {
            "Name": {"type": "title",
                     "title": [{"plain_text": "Existing"}]}}
    }]}
    _stub(monkeypatch, [
        (lambda r: "search" in r.full_url, json.dumps(pages)),
    ])
    with app.test_request_context():
        result = connectors.get("notion").sync(a, user=user, workspace=workspace)
    assert result["created"] == 0
    assert result["skipped"] == 1


def test_notion_sync_skips_blank_pages(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="notion", access_token="t",
    )
    db.session.add(a)
    db.session.commit()
    pages = {"results": [{
        "id": "p", "properties": {
            "Name": {"type": "title",
                     "title": [{"plain_text": "Empty"}]}}
    }]}
    _stub(monkeypatch, [
        (lambda r: "search" in r.full_url, json.dumps(pages)),
        (lambda r: "blocks" in r.full_url, json.dumps({"results": []})),
    ])
    with app.test_request_context():
        result = connectors.get("notion").sync(a, user=user, workspace=workspace)
    assert result["created"] == 0
    assert result["skipped"] == 1


def test_notion_block_text_handles_no_type():
    from filenergy.services.connectors import _notion_block_text
    assert _notion_block_text({}) == ""


def test_notion_page_title_no_title_property():
    from filenergy.services.connectors import _notion_page_title
    assert _notion_page_title({"properties": {"Tags": {"type": "select"}}}) == ""


def test_notion_page_text_handles_failure(monkeypatch):
    """When the blocks endpoint errors, we get an empty transcript, no crash."""
    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    from filenergy.services.connectors import _notion_page_text
    assert _notion_page_text("p", {"Authorization": "x"}) == ""


def test_notion_page_text_empty_id():
    from filenergy.services.connectors import _notion_page_text
    assert _notion_page_text("", {}) == ""


# ---------------------------------------------------------------------------
# Dropbox
# ---------------------------------------------------------------------------


def test_dropbox_is_configured(monkeypatch):
    dropbox = connectors.get("dropbox")
    monkeypatch.delenv("DROPBOX_OAUTH_CLIENT_ID", raising=False)
    assert dropbox.is_configured() is False
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    assert dropbox.is_configured() is True


def test_dropbox_authorize_url(monkeypatch):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    dropbox = connectors.get("dropbox")
    url = dropbox.authorize_url("http://x/cb", workspace_id=7)
    assert "state=7" in url
    assert "token_access_type=offline" in url


def test_dropbox_authorize_unconfigured(monkeypatch):
    monkeypatch.delenv("DROPBOX_OAUTH_CLIENT_ID", raising=False)
    dropbox = connectors.get("dropbox")
    with pytest.raises(connectors.ConnectorError):
        dropbox.authorize_url("http://x/cb", workspace_id=1)


def test_dropbox_complete_oauth(monkeypatch, db, workspace, app):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({
            "access_token": "sl.x", "refresh_token": "r.x",
            "expires_in": 14400, "account_id": "dbid:abc",
        })),
    ])
    dropbox = connectors.get("dropbox")
    with app.test_request_context():
        a = dropbox.complete_oauth(
            code="C", state=str(workspace.id), redirect_uri="http://x/cb"
        )
    assert a.access_token == "sl.x"
    assert a.refresh_token == "r.x"
    assert a.expires_at is not None


def test_dropbox_complete_oauth_unconfigured(monkeypatch, app, workspace):
    monkeypatch.delenv("DROPBOX_OAUTH_CLIENT_ID", raising=False)
    dropbox = connectors.get("dropbox")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        dropbox.complete_oauth(code="c", state=str(workspace.id), redirect_uri="x")


def test_dropbox_complete_oauth_bad_state(monkeypatch, app):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    dropbox = connectors.get("dropbox")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        dropbox.complete_oauth(code="c", state="x", redirect_uri="x")


def test_dropbox_complete_oauth_no_token(monkeypatch, app, workspace):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    _stub(monkeypatch, [(lambda r: True, json.dumps({"error": "x"}))])
    dropbox = connectors.get("dropbox")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        dropbox.complete_oauth(
            code="c", state=str(workspace.id), redirect_uri="x"
        )


def test_dropbox_ensure_fresh_skips_when_valid(db, workspace, app, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox",
        access_token="ok", refresh_token="r",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()
    with app.test_request_context():
        assert connectors.get("dropbox")._ensure_fresh(a) == "ok"


def test_dropbox_ensure_fresh_refreshes(db, workspace, app, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox",
        access_token="old", refresh_token="r",
        expires_at=utcnow() - timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({"access_token": "new", "expires_in": 3600})),
    ])
    with app.test_request_context():
        assert connectors.get("dropbox")._ensure_fresh(a) == "new"


def test_dropbox_ensure_fresh_no_refresh(db, workspace, app):
    from datetime import timedelta
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox",
        access_token="just-this", refresh_token=None,
        expires_at=utcnow() - timedelta(hours=1),
    )
    db.session.add(a)
    db.session.commit()
    with app.test_request_context():
        assert connectors.get("dropbox")._ensure_fresh(a) == "just-this"


def test_dropbox_sync_downloads_files(db, user, workspace, app, monkeypatch):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox",
        access_token="t", refresh_token=None,
    )
    db.session.add(a)
    db.session.commit()

    listing = {"entries": [
        {".tag": "file", "name": "report.pdf", "path_lower": "/report.pdf"},
        {".tag": "file", "name": "video.mov", "path_lower": "/video.mov"},
        {".tag": "folder", "name": "subfolder"},
    ]}
    _stub(monkeypatch, [
        (lambda r: "list_folder" in r.full_url, json.dumps(listing)),
        (lambda r: "download" in r.full_url, b"%PDF-1.4 fake"),
    ])
    with app.test_request_context():
        result = connectors.get("dropbox").sync(a, user=user, workspace=workspace)
    assert result["created"] == 1  # only report.pdf
    f = File.query.filter_by(workspace_id=workspace.id).one()
    assert f.name == "report.pdf"


def test_dropbox_sync_skips_existing(db, user, workspace, app, monkeypatch):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox", access_token="t",
    )
    db.session.add(a)
    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="report.pdf", path="/x", url="zzz",
    )
    db.session.add(f)
    db.session.commit()
    listing = {"entries": [{".tag": "file", "name": "report.pdf",
                            "path_lower": "/report.pdf"}]}
    _stub(monkeypatch, [
        (lambda r: "list_folder" in r.full_url, json.dumps(listing)),
    ])
    with app.test_request_context():
        result = connectors.get("dropbox").sync(a, user=user, workspace=workspace)
    assert result["created"] == 0
    assert result["skipped"] == 1


def test_dropbox_sync_handles_download_error(db, user, workspace, app, monkeypatch):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox", access_token="t",
    )
    db.session.add(a)
    db.session.commit()
    listing = {"entries": [{".tag": "file", "name": "report.pdf",
                            "path_lower": "/report.pdf"}]}

    def fake(req, timeout=None):
        if "list_folder" in req.full_url:
            class R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self, n=None): return json.dumps(listing).encode()
            return R()
        raise OSError("download nope")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with app.test_request_context():
        result = connectors.get("dropbox").sync(a, user=user, workspace=workspace)
    assert result["created"] == 0
    assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def test_slack_is_configured(monkeypatch):
    slack = connectors.get("slack")
    monkeypatch.delenv("SLACK_OAUTH_CLIENT_ID", raising=False)
    assert slack.is_configured() is False
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_SECRET", "y")
    assert slack.is_configured() is True


def test_slack_authorize_url(monkeypatch):
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_SECRET", "y")
    slack = connectors.get("slack")
    url = slack.authorize_url("http://x/cb", workspace_id=3)
    assert "state=3" in url
    assert "channels%3Ahistory" in url


def test_slack_authorize_unconfigured(monkeypatch):
    monkeypatch.delenv("SLACK_OAUTH_CLIENT_ID", raising=False)
    slack = connectors.get("slack")
    with pytest.raises(connectors.ConnectorError):
        slack.authorize_url("http://x/cb", 1)


def test_slack_complete_oauth(monkeypatch, db, workspace, app):
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_SECRET", "y")
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({
            "ok": True, "access_token": "xoxb-x",
            "team": {"name": "Acme Slack"},
        })),
    ])
    slack = connectors.get("slack")
    with app.test_request_context():
        a = slack.complete_oauth(
            code="c", state=str(workspace.id), redirect_uri="x",
        )
    assert a.access_token == "xoxb-x"
    assert a.account_label == "Acme Slack"


def test_slack_complete_oauth_unconfigured(monkeypatch, app, workspace):
    monkeypatch.delenv("SLACK_OAUTH_CLIENT_ID", raising=False)
    slack = connectors.get("slack")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        slack.complete_oauth(code="c", state=str(workspace.id), redirect_uri="x")


def test_slack_complete_oauth_bad_state(monkeypatch, app):
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_SECRET", "y")
    slack = connectors.get("slack")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        slack.complete_oauth(code="c", state="bad", redirect_uri="x")


def test_slack_complete_oauth_failure_response(monkeypatch, app, workspace):
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_SECRET", "y")
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({"ok": False, "error": "invalid_code"})),
    ])
    slack = connectors.get("slack")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        slack.complete_oauth(code="c", state=str(workspace.id), redirect_uri="x")


def test_slack_complete_oauth_no_token(monkeypatch, app, workspace):
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_SECRET", "y")
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({"ok": True, "team": {"name": "x"}})),
    ])
    slack = connectors.get("slack")
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        slack.complete_oauth(code="c", state=str(workspace.id), redirect_uri="x")


def test_slack_sync_writes_transcripts(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="slack",
        access_token="xoxb-x", account_label="Acme Slack",
    )
    db.session.add(a)
    db.session.commit()

    channels = {"ok": True, "channels": [{"id": "C1", "name": "general"}]}
    history = {"ok": True, "messages": [
        {"user": "U1", "text": "hello"},
        {"user": "U2", "text": "world"},
        {"user": "U1", "text": ""},  # empty skipped
    ]}
    _stub(monkeypatch, [
        (lambda r: "conversations.list" in r.full_url, json.dumps(channels)),
        (lambda r: "conversations.history" in r.full_url, json.dumps(history)),
    ])
    with app.test_request_context():
        result = connectors.get("slack").sync(a, user=user, workspace=workspace)
    assert result["created"] == 1
    f = File.query.filter_by(workspace_id=workspace.id).one()
    assert f.name == "slack-general.txt"


def test_slack_sync_handles_list_failure(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="slack", access_token="t",
    )
    db.session.add(a)
    db.session.commit()
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({"ok": False, "error": "auth"})),
    ])
    with app.test_request_context(), pytest.raises(connectors.ConnectorError):
        connectors.get("slack").sync(a, user=user, workspace=workspace)


def test_slack_sync_skips_existing_transcript(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="slack", access_token="t",
    )
    db.session.add(a)
    f = File(user_id=user.id, workspace_id=workspace.id,
             name="slack-general.txt", path="/x", url="zzz")
    db.session.add(f)
    db.session.commit()
    channels = {"ok": True, "channels": [{"id": "C1", "name": "general"}]}
    _stub(monkeypatch, [
        (lambda r: "conversations.list" in r.full_url, json.dumps(channels)),
    ])
    with app.test_request_context():
        result = connectors.get("slack").sync(a, user=user, workspace=workspace)
    assert result["skipped"] == 1


def test_slack_sync_skips_empty_transcripts(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="slack", access_token="t",
    )
    db.session.add(a)
    db.session.commit()
    channels = {"ok": True, "channels": [{"id": "C1", "name": "x"}]}
    history = {"ok": True, "messages": []}
    _stub(monkeypatch, [
        (lambda r: "conversations.list" in r.full_url, json.dumps(channels)),
        (lambda r: "conversations.history" in r.full_url, json.dumps(history)),
    ])
    with app.test_request_context():
        result = connectors.get("slack").sync(a, user=user, workspace=workspace)
    assert result["skipped"] == 1


def test_slack_channel_transcript_history_failure(monkeypatch):
    """When the history endpoint errors, transcript is empty (no crash)."""
    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    from filenergy.services.connectors import _slack_channel_transcript
    assert _slack_channel_transcript("C", {"Authorization": "x"}, 100) == ""


def test_slack_channel_transcript_not_ok(monkeypatch):
    """`ok: False` from Slack returns an empty transcript instead of crashing."""
    _stub(monkeypatch, [
        (lambda r: True, json.dumps({"ok": False, "error": "rate_limited"})),
    ])
    from filenergy.services.connectors import _slack_channel_transcript
    assert _slack_channel_transcript("C", {"Authorization": "x"}, 100) == ""
