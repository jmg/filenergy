"""Tests for incremental sync via per-account cursors."""
import json

import pytest

from filenergy.models import ConnectorAccount, File
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


# ---- Google Drive ----


def test_drive_first_sync_persists_modified_time_cursor(
    db, user, workspace, app, monkeypatch,
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="t", refresh_token=None,
    )
    db.session.add(a)
    db.session.commit()

    listing = {
        "files": [
            {"id": "f1", "name": "a.csv",
             "mimeType": "text/csv",
             "modifiedTime": "2026-04-30T10:00:00Z"},
        ]
    }
    seen_urls: list[str] = []

    def fake(req, timeout=None):
        seen_urls.append(req.full_url)
        body = (
            json.dumps(listing) if "files?" in req.full_url and "alt" not in req.full_url
            else b"col1,col2\n"
        )

        class R:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, n=None):
                return body if isinstance(body, bytes) else body.encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with app.test_request_context():
        connectors.get("google_drive").sync(a, user=user, workspace=workspace)

    db.session.refresh(a)
    assert a.sync_cursor == "2026-04-30T10:00:00Z"
    # The first listing call had no q= filter.
    assert not any("q=" in u for u in seen_urls)


def test_drive_second_sync_uses_cursor_filter(db, user, workspace, app, monkeypatch):
    """Second sync sends `q=modifiedTime > 'cursor'`."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="t", refresh_token=None,
        sync_cursor="2026-04-29T00:00:00Z",
    )
    db.session.add(a)
    db.session.commit()

    seen_urls: list[str] = []

    def fake(req, timeout=None):
        seen_urls.append(req.full_url)

        class R:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, n=None): return json.dumps({"files": []}).encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with app.test_request_context():
        connectors.get("google_drive").sync(a, user=user, workspace=workspace)
    assert any(
        "q=modifiedTime+%3E+%272026-04-29T00%3A00%3A00Z%27" in u
        for u in seen_urls
    )


# ---- Notion ----


def test_notion_sync_persists_next_cursor(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="notion", access_token="secret_x",
    )
    db.session.add(a)
    db.session.commit()
    pages = {
        "results": [{
            "id": "p1",
            "properties": {
                "Name": {"type": "title",
                         "title": [{"plain_text": "Page 1"}]},
            },
        }],
        "next_cursor": "cursor-abc",
    }
    blocks = {"results": [
        {"type": "paragraph",
         "paragraph": {"rich_text": [{"plain_text": "hi"}]}},
    ]}
    _stub(monkeypatch, [
        (lambda r: "search" in r.full_url, json.dumps(pages)),
        (lambda r: "blocks" in r.full_url, json.dumps(blocks)),
    ])
    with app.test_request_context():
        connectors.get("notion").sync(a, user=user, workspace=workspace)
    db.session.refresh(a)
    assert a.sync_cursor == "cursor-abc"


def test_notion_clears_cursor_when_exhausted(db, user, workspace, app, monkeypatch):
    """When `next_cursor` is null, we drop the cursor so the next tick starts over."""
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="notion",
        access_token="secret_x", sync_cursor="prev",
    )
    db.session.add(a)
    db.session.commit()
    _stub(monkeypatch, [
        (lambda r: "search" in r.full_url,
         json.dumps({"results": [], "next_cursor": None})),
    ])
    with app.test_request_context():
        connectors.get("notion").sync(a, user=user, workspace=workspace)
    db.session.refresh(a)
    assert a.sync_cursor is None


def test_notion_sync_uses_start_cursor_when_set(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="notion",
        access_token="secret_x", sync_cursor="cursor-xyz",
    )
    db.session.add(a)
    db.session.commit()

    captured: list = []

    def fake(req, timeout=None):
        captured.append(req.data or b"")

        class R:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, n=None):
                return json.dumps({"results": [], "next_cursor": None}).encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with app.test_request_context():
        connectors.get("notion").sync(a, user=user, workspace=workspace)
    body = json.loads(captured[0].decode())
    assert body.get("start_cursor") == "cursor-xyz"


# ---- Dropbox ----


def test_dropbox_first_sync_persists_cursor(db, user, workspace, app, monkeypatch):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox",
        access_token="t", refresh_token=None,
    )
    db.session.add(a)
    db.session.commit()

    listing = {
        "entries": [],
        "cursor": "dbx-cursor-1",
    }
    _stub(monkeypatch, [
        (lambda r: "list_folder" in r.full_url and "continue" not in r.full_url,
         json.dumps(listing)),
    ])
    with app.test_request_context():
        connectors.get("dropbox").sync(a, user=user, workspace=workspace)
    db.session.refresh(a)
    assert a.sync_cursor == "dbx-cursor-1"


def test_dropbox_second_sync_uses_continue_endpoint(
    db, user, workspace, app, monkeypatch,
):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "y")
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="dropbox",
        access_token="t", refresh_token=None,
        sync_cursor="dbx-cursor-1",
    )
    db.session.add(a)
    db.session.commit()
    seen_urls: list[str] = []

    def fake(req, timeout=None):
        seen_urls.append(req.full_url)

        class R:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, n=None):
                return json.dumps(
                    {"entries": [], "cursor": "dbx-cursor-2"}
                ).encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with app.test_request_context():
        connectors.get("dropbox").sync(a, user=user, workspace=workspace)
    assert any("/continue" in u for u in seen_urls)
    db.session.refresh(a)
    assert a.sync_cursor == "dbx-cursor-2"


# ---- Slack ----


def test_slack_first_sync_uses_oldest_zero_and_saves_latest_ts(
    db, user, workspace, app, monkeypatch,
):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="slack", access_token="xoxb-t",
    )
    db.session.add(a)
    db.session.commit()

    channels = {"ok": True, "channels": [{"id": "C1", "name": "general"}]}
    history = {"ok": True, "messages": [
        {"user": "U1", "text": "hello", "ts": "1700000000.000100"},
        {"user": "U2", "text": "world", "ts": "1700000010.000200"},
    ]}
    seen_oldest: list[str] = []

    def fake(req, timeout=None):
        if "conversations.history" in req.full_url:
            # Capture the oldest= param.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(req.full_url).query)
            seen_oldest.append(qs.get("oldest", [""])[0])

        class R:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, n=None):
                if "conversations.list" in req.full_url:
                    return json.dumps(channels).encode()
                return json.dumps(history).encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with app.test_request_context():
        connectors.get("slack").sync(a, user=user, workspace=workspace)

    assert seen_oldest == ["0"]
    db.session.refresh(a)
    assert a.sync_cursor == "1700000010.000200"


def test_slack_second_sync_uses_saved_cursor(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="slack",
        access_token="xoxb-t", sync_cursor="1700000010.000200",
    )
    db.session.add(a)
    db.session.commit()

    channels = {"ok": True, "channels": [{"id": "C1", "name": "general"}]}
    history = {"ok": True, "messages": []}
    seen_oldest: list[str] = []

    def fake(req, timeout=None):
        if "conversations.history" in req.full_url:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(req.full_url).query)
            seen_oldest.append(qs.get("oldest", [""])[0])

        class R:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, n=None):
                if "conversations.list" in req.full_url:
                    return json.dumps(channels).encode()
                return json.dumps(history).encode()

        return R()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with app.test_request_context():
        connectors.get("slack").sync(a, user=user, workspace=workspace)
    assert seen_oldest == ["1700000010.000200"]


def test_slack_appends_delta_to_existing_transcript(
    db, user, workspace, app, monkeypatch,
):
    """When the channel's transcript file already exists, Slack appends."""
    import os
    from filenergy.services.file import FileService

    # Pre-create the transcript file.
    os.makedirs("/tmp/filenergy-slack-test", exist_ok=True)
    path = "/tmp/filenergy-slack-test/slack-general.txt"
    with open(path, "wb") as fd:
        fd.write(b"old content")
    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="slack-general.txt", path=path, url="szz",
    )
    db.session.add(f)
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="slack", access_token="xoxb-t",
    )
    db.session.add(a)
    db.session.commit()

    channels = {"ok": True, "channels": [{"id": "C1", "name": "general"}]}
    history = {"ok": True, "messages": [
        {"user": "U1", "text": "new line", "ts": "1700000020.000300"},
    ]}
    _stub(monkeypatch, [
        (lambda r: "conversations.list" in r.full_url, json.dumps(channels)),
        (lambda r: "conversations.history" in r.full_url, json.dumps(history)),
    ])
    with app.test_request_context():
        connectors.get("slack").sync(a, user=user, workspace=workspace)

    body = open(path, "rb").read()
    assert b"old content" in body
    assert b"new line" in body
