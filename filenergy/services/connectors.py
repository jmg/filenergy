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
# Registry
# ---------------------------------------------------------------------------


_CONNECTORS: dict[str, BaseConnector] = {
    GoogleDriveConnector.kind: GoogleDriveConnector(),
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


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
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
