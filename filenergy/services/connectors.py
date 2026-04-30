"""Pluggable third-party source connectors.

Each connector implements:
- `kind`: a short identifier (e.g. "google_drive")
- `is_configured()`: True when the env var pair (CLIENT_ID + CLIENT_SECRET)
  is set
- `authorize_url(callback_uri, workspace_id) -> str`: where to send the
  user's browser to grant access
- `complete_oauth(callback_request) -> ConnectorAccount`: handle the
  callback, store tokens, return the persisted account row
- `sync(account, workspace, user) -> dict`: pull new files into the
  workspace, return `{"created": N, "skipped": N}` for the UI

Today: Google Drive. The same shape is meant to plug in Notion, Slack,
Dropbox, etc.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any

from filenergy import db
from filenergy.models import ConnectorAccount, utcnow
from filenergy.services import ingestion

log = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    pass


class BaseConnector:
    kind: str = ""
    label: str = ""

    def is_configured(self) -> bool:
        return False

    def authorize_url(self, redirect_uri: str, workspace_id: int) -> str:
        raise NotImplementedError

    def complete_oauth(
        self, *, code: str, state: str, redirect_uri: str
    ) -> ConnectorAccount:
        raise NotImplementedError

    def sync(self, account: ConnectorAccount, *, user, workspace) -> dict:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


_GOOGLE_OAUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DRIVE_LIST_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_DOWNLOAD_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_DRIVE_SCOPES = (
    "openid email profile "
    "https://www.googleapis.com/auth/drive.readonly"
)

# Native Drive types we know how to export to indexable formats.
_DRIVE_EXPORTS = {
    "application/vnd.google-apps.document": (
        "text/plain", ".txt",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "text/csv", ".csv",
    ),
    "application/vnd.google-apps.presentation": (
        "text/plain", ".txt",
    ),
}

_DIRECT_INDEXABLE_MIMES = {
    "application/pdf", "text/plain", "text/markdown", "text/csv",
    "text/html", "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class GoogleDriveConnector(BaseConnector):
    kind = "google_drive"
    label = "Google Drive"

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
            and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        )

    def authorize_url(self, redirect_uri: str, workspace_id: int) -> str:
        if not self.is_configured():
            raise ConnectorError("Google OAuth client not configured")
        params = {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _DRIVE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": str(workspace_id),
        }
        return f"{_GOOGLE_OAUTH_URL}?{urllib.parse.urlencode(params)}"

    def complete_oauth(
        self, *, code: str, state: str, redirect_uri: str
    ) -> ConnectorAccount:
        if not self.is_configured():
            raise ConnectorError("Google OAuth client not configured")
        try:
            workspace_id = int(state)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("Invalid state") from exc

        token = _post_form(_GOOGLE_TOKEN_URL, {
            "code": code,
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        access_token = token.get("access_token")
        if not access_token:
            raise ConnectorError("Google did not return an access token")
        userinfo = _get_json(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        label = userinfo.get("email") or userinfo.get("name") or "Drive"

        account = ConnectorAccount(
            workspace_id=workspace_id,
            kind=self.kind,
            account_label=label,
            access_token=access_token,
            refresh_token=token.get("refresh_token"),
            expires_at=(
                utcnow() + timedelta(seconds=int(token["expires_in"]))
                if token.get("expires_in")
                else None
            ),
        )
        db.session.add(account)
        db.session.commit()
        return account

    # ---- token refresh ----

    def _ensure_fresh(self, account: ConnectorAccount) -> str:
        if account.expires_at and account.expires_at > utcnow() + timedelta(minutes=2):
            return account.access_token
        if not account.refresh_token:
            return account.access_token  # best-effort
        token = _post_form(_GOOGLE_TOKEN_URL, {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        })
        access = token.get("access_token") or account.access_token
        account.access_token = access
        if token.get("expires_in"):
            account.expires_at = utcnow() + timedelta(seconds=int(token["expires_in"]))
        db.session.commit()
        return access

    # ---- sync ----

    def sync(self, account: ConnectorAccount, *, user, workspace,
             max_files: int = 25) -> dict:
        access = self._ensure_fresh(account)
        headers = {"Authorization": f"Bearer {access}"}

        listing = _get_json(
            _DRIVE_LIST_URL,
            params={
                "pageSize": str(max_files),
                "fields": "files(id,name,mimeType,modifiedTime,size)",
                "orderBy": "modifiedTime desc",
            },
            headers=headers,
        )
        items = listing.get("files", [])

        from filenergy.models import File
        created = 0
        skipped = 0
        for entry in items:
            mime = entry.get("mimeType")
            name = entry.get("name") or "drive-file"
            existing = File.query.filter_by(
                workspace_id=workspace.id, name=name,
            ).first()
            if existing is not None:
                skipped += 1
                continue
            content = _fetch_drive_blob(entry, access)
            if content is None:
                skipped += 1
                continue
            ext_name = _suggested_filename(entry)
            ingestion.materialize_blob(
                user=user, workspace=workspace,
                name=ext_name, content=content,
            )
            created += 1

        account.last_synced_at = utcnow()
        account.last_error = None
        db.session.commit()
        return {"created": created, "skipped": skipped}


def _suggested_filename(entry: dict) -> str:
    name = entry.get("name") or "drive-file"
    mime = entry.get("mimeType") or ""
    if mime in _DRIVE_EXPORTS and "." not in name:
        return name + _DRIVE_EXPORTS[mime][1]
    return name


def _fetch_drive_blob(entry: dict, access: str) -> bytes | None:
    headers = {"Authorization": f"Bearer {access}"}
    file_id = entry.get("id")
    mime = entry.get("mimeType") or ""
    if mime in _DRIVE_EXPORTS:
        export_mime = _DRIVE_EXPORTS[mime][0]
        url = f"{_DRIVE_DOWNLOAD_URL.format(file_id=file_id)}/export"
        params = {"mimeType": export_mime}
    elif mime in _DIRECT_INDEXABLE_MIMES or mime.startswith("text/"):
        url = _DRIVE_DOWNLOAD_URL.format(file_id=file_id)
        params = {"alt": "media"}
    else:
        return None
    try:
        return _get_bytes(url, params=params, headers=headers)
    except ConnectorError as exc:
        log.warning("Drive blob fetch failed for %s: %s", file_id, exc)
        return None


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------


_NOTION_OAUTH_URL = "https://api.notion.com/v1/oauth/authorize"
_NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"
_NOTION_SEARCH_URL = "https://api.notion.com/v1/search"
_NOTION_BLOCKS_URL = "https://api.notion.com/v1/blocks/{block_id}/children"
_NOTION_API_VERSION = "2022-06-28"


class NotionConnector(BaseConnector):
    kind = "notion"
    label = "Notion"

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("NOTION_OAUTH_CLIENT_ID")
            and os.environ.get("NOTION_OAUTH_CLIENT_SECRET")
        )

    def authorize_url(self, redirect_uri: str, workspace_id: int) -> str:
        if not self.is_configured():
            raise ConnectorError("Notion OAuth client not configured")
        params = {
            "client_id": os.environ["NOTION_OAUTH_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "owner": "user",
            "state": str(workspace_id),
        }
        return f"{_NOTION_OAUTH_URL}?{urllib.parse.urlencode(params)}"

    def complete_oauth(
        self, *, code: str, state: str, redirect_uri: str
    ) -> ConnectorAccount:
        if not self.is_configured():
            raise ConnectorError("Notion OAuth client not configured")
        try:
            workspace_id = int(state)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("Invalid state") from exc

        # Notion uses HTTP Basic auth on the token endpoint.
        import base64
        client_id = os.environ["NOTION_OAUTH_CLIENT_ID"]
        client_secret = os.environ["NOTION_OAUTH_CLIENT_SECRET"]
        basic = base64.b64encode(
            f"{client_id}:{client_secret}".encode("utf-8")
        ).decode("ascii")

        token = _post_json(_NOTION_TOKEN_URL, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }, headers={
            "Authorization": f"Basic {basic}",
            "Notion-Version": _NOTION_API_VERSION,
        })
        access_token = token.get("access_token")
        if not access_token:
            raise ConnectorError("Notion did not return an access token")

        label = (
            (token.get("workspace_name"))
            or (token.get("owner") or {}).get("user", {}).get("name")
            or "Notion"
        )
        account = ConnectorAccount(
            workspace_id=workspace_id,
            kind=self.kind,
            account_label=label,
            access_token=access_token,
            refresh_token=None,  # Notion access tokens are long-lived
        )
        db.session.add(account)
        db.session.commit()
        return account

    def sync(self, account: ConnectorAccount, *, user, workspace,
             max_pages: int = 25) -> dict:
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Notion-Version": _NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
        result = _post_json(_NOTION_SEARCH_URL, {
            "filter": {"property": "object", "value": "page"},
            "page_size": max_pages,
        }, headers=headers)
        pages = result.get("results", [])

        from filenergy.models import File
        created = 0
        skipped = 0
        for page in pages:
            page_id = page.get("id")
            title = _notion_page_title(page) or f"page-{page_id[:8]}"
            existing = File.query.filter_by(
                workspace_id=workspace.id, name=title + ".md",
            ).first()
            if existing is not None:
                skipped += 1
                continue
            text = _notion_page_text(page_id, headers)
            if not text:
                skipped += 1
                continue
            ingestion.materialize_blob(
                user=user, workspace=workspace,
                name=title + ".md",
                content=text.encode("utf-8"),
            )
            created += 1

        account.last_synced_at = utcnow()
        account.last_error = None
        db.session.commit()
        return {"created": created, "skipped": skipped}


def _notion_page_title(page: dict) -> str:
    """Best-effort title extraction across page-shape variants."""
    props = (page.get("properties") or {})
    for prop in props.values():
        if prop.get("type") == "title":
            chunks = prop.get("title") or []
            return "".join(c.get("plain_text", "") for c in chunks).strip()
    return ""


def _notion_page_text(page_id: str, headers: dict) -> str:
    """Recursively flatten block children into plain text."""
    if not page_id:
        return ""
    try:
        body = _get_json(
            _NOTION_BLOCKS_URL.format(block_id=page_id),
            headers=headers,
        )
    except ConnectorError as exc:
        log.warning("Notion block fetch failed for %s: %s", page_id, exc)
        return ""
    parts: list[str] = []
    for block in body.get("results", []):
        text = _notion_block_text(block)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _notion_block_text(block: dict) -> str:
    btype = block.get("type")
    if not btype:
        return ""
    inner = block.get(btype) or {}
    rich = inner.get("rich_text") or inner.get("text") or []
    return "".join(r.get("plain_text", "") for r in rich).strip()


# ---------------------------------------------------------------------------
# Dropbox
# ---------------------------------------------------------------------------


_DROPBOX_OAUTH_URL = "https://www.dropbox.com/oauth2/authorize"
_DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
_DROPBOX_LIST_URL = "https://api.dropboxapi.com/2/files/list_folder"
_DROPBOX_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"

_DROPBOX_INDEXABLE_EXTS = {
    ".pdf", ".txt", ".md", ".markdown", ".docx", ".csv", ".json",
    ".html", ".htm", ".log",
}


class DropboxConnector(BaseConnector):
    kind = "dropbox"
    label = "Dropbox"

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("DROPBOX_OAUTH_CLIENT_ID")
            and os.environ.get("DROPBOX_OAUTH_CLIENT_SECRET")
        )

    def authorize_url(self, redirect_uri: str, workspace_id: int) -> str:
        if not self.is_configured():
            raise ConnectorError("Dropbox OAuth client not configured")
        params = {
            "client_id": os.environ["DROPBOX_OAUTH_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "token_access_type": "offline",
            "state": str(workspace_id),
        }
        return f"{_DROPBOX_OAUTH_URL}?{urllib.parse.urlencode(params)}"

    def complete_oauth(
        self, *, code: str, state: str, redirect_uri: str
    ) -> ConnectorAccount:
        if not self.is_configured():
            raise ConnectorError("Dropbox OAuth client not configured")
        try:
            workspace_id = int(state)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("Invalid state") from exc

        token = _post_form(_DROPBOX_TOKEN_URL, {
            "code": code,
            "client_id": os.environ["DROPBOX_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["DROPBOX_OAUTH_CLIENT_SECRET"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        access = token.get("access_token")
        if not access:
            raise ConnectorError("Dropbox did not return an access token")
        account = ConnectorAccount(
            workspace_id=workspace_id,
            kind=self.kind,
            account_label=token.get("account_id") or "Dropbox",
            access_token=access,
            refresh_token=token.get("refresh_token"),
            expires_at=(
                utcnow() + timedelta(seconds=int(token["expires_in"]))
                if token.get("expires_in") else None
            ),
        )
        db.session.add(account)
        db.session.commit()
        return account

    def _ensure_fresh(self, account: ConnectorAccount) -> str:
        if account.expires_at and account.expires_at > utcnow() + timedelta(minutes=2):
            return account.access_token
        if not account.refresh_token:
            return account.access_token
        token = _post_form(_DROPBOX_TOKEN_URL, {
            "client_id": os.environ["DROPBOX_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["DROPBOX_OAUTH_CLIENT_SECRET"],
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        })
        access = token.get("access_token") or account.access_token
        account.access_token = access
        if token.get("expires_in"):
            account.expires_at = utcnow() + timedelta(seconds=int(token["expires_in"]))
        db.session.commit()
        return access

    def sync(self, account: ConnectorAccount, *, user, workspace,
             max_files: int = 50) -> dict:
        access = self._ensure_fresh(account)
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
        }
        listing = _post_json(_DROPBOX_LIST_URL, {
            "path": "",
            "recursive": False,
            "limit": max_files,
        }, headers=headers)
        entries = listing.get("entries", [])

        from filenergy.models import File
        created = 0
        skipped = 0
        for entry in entries:
            if entry.get(".tag") != "file":
                skipped += 1
                continue
            name = entry.get("name") or "dropbox-file"
            ext = os.path.splitext(name)[1].lower()
            if ext not in _DROPBOX_INDEXABLE_EXTS:
                skipped += 1
                continue
            existing = File.query.filter_by(
                workspace_id=workspace.id, name=name,
            ).first()
            if existing is not None:
                skipped += 1
                continue
            try:
                content = _dropbox_download(entry.get("path_lower") or entry.get("name"), access)
            except ConnectorError as exc:
                log.warning("Dropbox download failed for %s: %s", name, exc)
                skipped += 1
                continue
            ingestion.materialize_blob(
                user=user, workspace=workspace, name=name, content=content,
            )
            created += 1

        account.last_synced_at = utcnow()
        account.last_error = None
        db.session.commit()
        return {"created": created, "skipped": skipped}


def _dropbox_download(path: str, access: str) -> bytes:
    """Dropbox content API: token + Dropbox-API-Arg header (JSON path).

    No body; the file content comes back as the response body.
    """
    headers = {
        "Authorization": f"Bearer {access}",
        "Dropbox-API-Arg": json.dumps({"path": path}),
    }
    req = urllib.request.Request(
        _DROPBOX_DOWNLOAD_URL, data=b"", headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        raise ConnectorError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


_SLACK_OAUTH_URL = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_SLACK_LIST_CHANNELS_URL = "https://slack.com/api/conversations.list"
_SLACK_HISTORY_URL = "https://slack.com/api/conversations.history"


class SlackConnector(BaseConnector):
    kind = "slack"
    label = "Slack"

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("SLACK_OAUTH_CLIENT_ID")
            and os.environ.get("SLACK_OAUTH_CLIENT_SECRET")
        )

    def authorize_url(self, redirect_uri: str, workspace_id: int) -> str:
        if not self.is_configured():
            raise ConnectorError("Slack OAuth client not configured")
        params = {
            "client_id": os.environ["SLACK_OAUTH_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            # Read-only bot scopes.
            "scope": "channels:history,channels:read,groups:history,groups:read",
            "state": str(workspace_id),
        }
        return f"{_SLACK_OAUTH_URL}?{urllib.parse.urlencode(params)}"

    def complete_oauth(
        self, *, code: str, state: str, redirect_uri: str
    ) -> ConnectorAccount:
        if not self.is_configured():
            raise ConnectorError("Slack OAuth client not configured")
        try:
            workspace_id = int(state)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("Invalid state") from exc

        token = _post_form(_SLACK_TOKEN_URL, {
            "code": code,
            "client_id": os.environ["SLACK_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["SLACK_OAUTH_CLIENT_SECRET"],
            "redirect_uri": redirect_uri,
        })
        if not token.get("ok"):
            raise ConnectorError(f"Slack OAuth failed: {token.get('error')}")
        access = token.get("access_token")
        team = (token.get("team") or {}).get("name") or "Slack"
        if not access:
            raise ConnectorError("Slack did not return an access token")

        account = ConnectorAccount(
            workspace_id=workspace_id,
            kind=self.kind,
            account_label=team,
            access_token=access,
            refresh_token=None,
        )
        db.session.add(account)
        db.session.commit()
        return account

    def sync(self, account: ConnectorAccount, *, user, workspace,
             max_channels: int = 5, max_messages: int = 200) -> dict:
        headers = {"Authorization": f"Bearer {account.access_token}"}

        listing = _get_json(
            _SLACK_LIST_CHANNELS_URL,
            params={"limit": str(max_channels), "exclude_archived": "true"},
            headers=headers,
        )
        if not listing.get("ok"):
            raise ConnectorError(f"Slack: {listing.get('error')}")
        channels = listing.get("channels", [])

        from filenergy.models import File
        created = 0
        skipped = 0
        for channel in channels:
            name = channel.get("name") or "channel"
            file_name = f"slack-{name}.txt"
            existing = File.query.filter_by(
                workspace_id=workspace.id, name=file_name,
            ).first()
            if existing is not None:
                skipped += 1
                continue
            transcript = _slack_channel_transcript(
                channel["id"], headers, max_messages
            )
            if not transcript:
                skipped += 1
                continue
            ingestion.materialize_blob(
                user=user, workspace=workspace, name=file_name,
                content=transcript.encode("utf-8"),
            )
            created += 1

        account.last_synced_at = utcnow()
        account.last_error = None
        db.session.commit()
        return {"created": created, "skipped": skipped}


def _slack_channel_transcript(channel_id: str, headers: dict, limit: int) -> str:
    try:
        body = _get_json(
            _SLACK_HISTORY_URL,
            params={"channel": channel_id, "limit": str(limit)},
            headers=headers,
        )
    except ConnectorError as exc:
        log.warning("Slack history failed for %s: %s", channel_id, exc)
        return ""
    if not body.get("ok"):
        return ""
    lines = []
    for msg in reversed(body.get("messages", [])):  # oldest first
        text = msg.get("text") or ""
        if not text:
            continue
        user_id = msg.get("user") or msg.get("bot_id") or "?"
        lines.append(f"{user_id}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_CONNECTORS: dict[str, BaseConnector] = {
    GoogleDriveConnector.kind: GoogleDriveConnector(),
    NotionConnector.kind: NotionConnector(),
    DropboxConnector.kind: DropboxConnector(),
    SlackConnector.kind: SlackConnector(),
}


def get(kind: str) -> BaseConnector | None:
    return _CONNECTORS.get(kind)


def all_connectors() -> list[BaseConnector]:
    return list(_CONNECTORS.values())


def list_accounts(workspace) -> list[ConnectorAccount]:
    return (
        ConnectorAccount.query.filter_by(workspace_id=workspace.id)
        .order_by(ConnectorAccount.id.desc())
        .all()
    )


def disconnect(account: ConnectorAccount) -> None:
    db.session.delete(account)
    db.session.commit()


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no requests/httpx dep just for this)
# ---------------------------------------------------------------------------


def _post_form(url: str, data: dict, headers: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    return _open_json(req)


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    return _open_json(req)


def _get_json(url: str, *, params: dict[str, str] | None = None,
              headers: dict[str, str] | None = None) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    return _open_json(req)


def _get_bytes(url: str, *, params: dict[str, str] | None = None,
               headers: dict[str, str] | None = None) -> bytes:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        raise ConnectorError(str(exc)) from exc


def _open_json(req) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception as exc:
        raise ConnectorError(str(exc)) from exc
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ConnectorError(f"Bad JSON from upstream: {exc}") from exc
