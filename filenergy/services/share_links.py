"""Public share links with optional TTL and download cap."""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from filenergy import db
from filenergy.models import ShareLink, utcnow


def create(file, *, created_by, ttl_hours: Optional[int] = None,
           max_downloads: Optional[int] = None) -> ShareLink:
    expires_at = utcnow() + timedelta(hours=ttl_hours) if ttl_hours else None
    link = ShareLink(
        file_id=file.id,
        token=secrets.token_urlsafe(24),
        expires_at=expires_at,
        max_downloads=max_downloads,
        download_count=0,
        created_by_id=created_by.id,
    )
    db.session.add(link)
    db.session.commit()
    return link


def find_active(token: str) -> Optional[ShareLink]:
    link = ShareLink.query.filter_by(token=token).first()
    if link is None:
        return None
    return link if link.is_active() else None


def record_download(link: ShareLink) -> None:
    link.download_count = (link.download_count or 0) + 1
    db.session.commit()


def revoke(link: ShareLink) -> None:
    link.revoked_at = utcnow()
    db.session.commit()


def list_for_file(file) -> list[ShareLink]:
    return (
        ShareLink.query.filter_by(file_id=file.id)
        .order_by(ShareLink.id.desc())
        .all()
    )
