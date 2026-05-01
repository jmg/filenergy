"""Email-to-ingest.

Each workspace gets a deterministic inbound address derived from its
slug + a shared secret:

    inbox-<slug>-<token>@<FILENERGY_INBOUND_DOMAIN>

Inbound providers (Postmark, SendGrid, Mailgun, Cloudflare Email
Workers) POST a JSON or multipart payload to `/inbound/email`. We
parse the To address, look up the workspace, and ingest the body +
each attachment as separate files.

The token is derived as `HMAC-SHA256(workspace.slug, INBOUND_SECRET)[:12]`
so a leak of one address doesn't help an attacker guess another. The
endpoint also supports a shared-secret header (`X-Inbound-Secret`) for
providers that allow it.

Disabled until both `FILENERGY_INBOUND_DOMAIN` and
`FILENERGY_INBOUND_SECRET` are set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re

from filenergy import db
from filenergy.models import File, Workspace
from filenergy.services import billing, events, ingestion

log = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(
        os.environ.get("FILENERGY_INBOUND_DOMAIN")
        and os.environ.get("FILENERGY_INBOUND_SECRET")
    )


def _domain() -> str:
    return os.environ.get("FILENERGY_INBOUND_DOMAIN", "")


def _secret() -> bytes:
    return os.environ.get("FILENERGY_INBOUND_SECRET", "").encode("utf-8")


def address_for(workspace: Workspace) -> str:
    """Return the per-workspace inbound address. Display this in
    Settings → Workspace so users know where to forward email."""
    if not is_configured():
        return ""
    token = hmac.new(_secret(), workspace.slug.encode("utf-8"), hashlib.sha256)
    short = token.hexdigest()[:12]
    return f"inbox-{workspace.slug}-{short}@{_domain()}"


_LOCAL_RE = re.compile(r"^inbox-(?P<slug>[^@]+?)-(?P<token>[a-f0-9]{12})$")


def _resolve_workspace(to_address: str) -> Workspace | None:
    """Parse `inbox-<slug>-<token>@<domain>` and return the workspace,
    or None if the slug or token doesn't match. Constant-time compare
    on the token guards against forgery via timing."""
    if not is_configured() or "@" not in to_address:
        return None
    local, _, domain = to_address.lower().partition("@")
    if domain != _domain().lower():
        return None
    m = _LOCAL_RE.match(local)
    if not m:
        return None
    slug = m.group("slug")
    presented = m.group("token")
    ws = Workspace.query.filter_by(slug=slug).first()
    if ws is None:
        return None
    expected = hmac.new(_secret(), slug.encode("utf-8"), hashlib.sha256).hexdigest()[:12]
    if not hmac.compare_digest(expected, presented):
        return None
    return ws


def ingest_payload(payload: dict) -> dict:
    """Process an inbound provider payload. Provider-agnostic shape:

        {
            "to":      "inbox-acme-abc123@filenergy.io",
            "from":    "alice@example.com",
            "subject": "Q4 contract draft",
            "text":    "Body of the email...",
            "html":    "<p>...</p>",
            "attachments": [
                {"filename": "draft.pdf", "content": "<base64>",
                 "content_type": "application/pdf"},
            ],
        }
    """
    to_addr = (payload.get("to") or "").strip()
    workspace = _resolve_workspace(to_addr)
    if workspace is None:
        return {"ok": False, "error": "no matching workspace"}

    from_addr = (payload.get("from") or "").strip()
    subject = (payload.get("subject") or "Email").strip()[:200]
    body_text = payload.get("text") or ""
    attachments = payload.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []

    user = workspace.owner
    created: list[int] = []

    # 1) Body — only if it has substantive text. Save as `<subject>.md`.
    if body_text and body_text.strip():
        try:
            billing.ensure_can_upload(workspace)
            md = f"# {subject}\n\n_From: {from_addr}_\n\n{body_text}"
            f = ingestion.ingest_text(
                user=user, workspace=workspace,
                name=f"{subject}.md",
                content_bytes=md.encode("utf-8"),
                source="email",
            )
            created.append(f.id)
        except billing.QuotaExceeded:
            pass
        except Exception:
            log.exception("Failed to ingest email body")

    # 2) Attachments — each becomes its own file.
    for att in attachments:
        if not isinstance(att, dict):
            continue
        filename = (att.get("filename") or "attachment").strip()
        b64 = att.get("content") or ""
        if not filename or not b64:
            continue
        try:
            data = base64.b64decode(b64)
        except Exception:
            continue
        if len(data) > 10 * 1024 * 1024:  # 10 MB hard cap
            continue
        try:
            billing.ensure_can_upload(workspace)
            f = ingestion.ingest_text(
                user=user, workspace=workspace,
                name=filename, content_bytes=data, source="email",
            )
            created.append(f.id)
        except billing.QuotaExceeded:
            break
        except Exception:
            log.exception("Failed to ingest attachment %s", filename)

    events.log_event(
        events.FILE_UPLOADED,
        user=user, workspace_id=workspace.id,
        source="email", from_address=from_addr,
        ingested_count=len(created),
    )
    return {"ok": True, "ingested": len(created), "ids": created}
