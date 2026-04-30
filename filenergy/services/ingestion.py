"""Ingest content from non-upload sources.

Today: URL ingestion (fetch + strip HTML). Future connectors (GDrive,
Notion, Slack) will follow the same shape: produce a `(name, bytes,
mime)` triple and hand it to `materialize_blob`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from html.parser import HTMLParser
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from filenergy import app, db, settings
from filenergy.models import File
from filenergy.services import events
from filenergy.services.file import FileService

log = logging.getLogger(__name__)


_FETCH_TIMEOUT = 15
_MAX_BYTES = 10 * 1024 * 1024  # cap remote pages at 10 MB
_USER_AGENT = "Filenergy/1 (+https://filenergy.dev)"


class IngestionError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    """Light HTML→text extractor: drop scripts/styles, collapse whitespace."""

    _SKIP = {"script", "style", "noscript", "svg", "header", "footer", "nav"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._title: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self._title.append(data)
            return
        self._buf.append(data)

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._title)).strip()

    @property
    def text(self) -> str:
        raw = "".join(self._buf)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", raw)).strip()


def _safe_filename_from_url(url: str, fallback_title: str = "") -> str:
    parsed = urlparse(url)
    path_basename = (parsed.path or "/").strip("/").split("/")[-1]
    if path_basename and "." in path_basename:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", path_basename).strip("-")
    title_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", fallback_title or "page")
    title_slug = title_slug.strip("-")[:64] or "page"
    return f"{title_slug}.html"


def fetch_url(url: str) -> tuple[str, str, str]:
    """Fetch a URL. Returns (filename, plain_text, raw_html_or_text).

    Raises IngestionError on any network/parse failure or unsupported
    content type. Strips HTML for the indexable text but stores the raw
    document on disk.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise IngestionError("Only http(s) URLs are supported")
    req = urllib_request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib_request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
            data = resp.read(_MAX_BYTES + 1)
    except urllib_error.HTTPError as exc:
        raise IngestionError(f"Remote returned HTTP {exc.code}") from exc
    except Exception as exc:
        raise IngestionError(f"Failed to fetch URL: {exc}") from exc

    if len(data) > _MAX_BYTES:
        raise IngestionError("Page exceeds 10 MB limit")

    if not ctype.startswith(("text/", "application/json", "application/xml")):
        raise IngestionError(f"Unsupported content type: {ctype}")

    body = data.decode("utf-8", errors="replace")
    if "html" in ctype or body.lstrip().startswith("<"):
        parser = _TextExtractor()
        try:
            parser.feed(body)
        except Exception:
            text, title = body, ""
        else:
            text, title = parser.text, parser.title
    else:
        text, title = body, ""

    if not text.strip():
        raise IngestionError("No text could be extracted from the page")

    name = _safe_filename_from_url(url, title)
    return name, text, body


def materialize_blob(
    *, user, workspace, name: str, content: bytes,
    pre_extracted_text: str | None = None,
) -> File:
    """Write a remote/synthetic blob to disk and persist a File row."""
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    base_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "ingested"
    digest = hashlib.sha1(content).hexdigest()[:8]
    safe_name = f"{digest}-{base_name}"
    path = os.path.join(settings.UPLOAD_DIR, safe_name)
    with open(path, "wb") as fd:
        fd.write(content)

    db_file = FileService()._persist_upload(
        path=path,
        name=base_name,
        user=user,
        workspace_id=workspace.id,
        is_public=False,
        size_bytes=len(content),
    )
    if pre_extracted_text is not None:
        db_file.text_content = pre_extracted_text
        db.session.commit()

    events.log_event(
        events.FILE_UPLOADED,
        user=user, workspace_id=workspace.id,
        file_id=db_file.id, name=base_name, source="ingestion",
    )
    return db_file


def ingest_url(*, user, workspace, url: str, sync_index: bool | None = None) -> File:
    """End-to-end: fetch a URL, persist it, kick off indexing."""
    name, text, raw = fetch_url(url)
    db_file = materialize_blob(
        user=user, workspace=workspace, name=name,
        content=raw.encode("utf-8"), pre_extracted_text=text,
    )
    if sync_index is None:
        sync_index = settings.SYNC_INDEXING or app.config.get("TESTING", False)
    if sync_index:
        FileService().index_file(db_file)
    else:
        threading.Thread(
            target=lambda: _async_index(db_file.id),
            name=f"index-{db_file.id}", daemon=True,
        ).start()
    return db_file


def _async_index(file_id: int) -> None:
    with app.app_context():
        f = File.query.get(file_id)
        if f is not None:
            FileService().index_file(f)
