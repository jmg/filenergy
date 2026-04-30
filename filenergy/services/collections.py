"""Collections (folders / notebooks) within a workspace.

Files belong to one Collection or none. Retrieval can be scoped to a
collection so users can chat with a subset of their library.
"""
from __future__ import annotations

import re

from filenergy import db
from filenergy.models import Collection, File


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "collection"
    return base[:48]


def _unique_slug(workspace_id: int, name: str) -> str:
    base = slugify(name)
    candidate = base
    counter = 2
    while Collection.query.filter_by(
        workspace_id=workspace_id, slug=candidate
    ).first() is not None:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def create(workspace, name: str, description: str | None = None) -> Collection:
    name = (name or "").strip() or "Untitled"
    coll = Collection(
        workspace_id=workspace.id,
        name=name,
        slug=_unique_slug(workspace.id, name),
        description=(description or None),
    )
    db.session.add(coll)
    db.session.commit()
    return coll


def get_by_slug(workspace, slug: str) -> Collection | None:
    return Collection.query.filter_by(
        workspace_id=workspace.id, slug=slug
    ).first()


def get(workspace, collection_id: int) -> Collection | None:
    return Collection.query.filter_by(
        workspace_id=workspace.id, id=collection_id
    ).first()


def list_for_workspace(workspace) -> list[Collection]:
    return (
        Collection.query.filter_by(workspace_id=workspace.id)
        .order_by(Collection.id.asc())
        .all()
    )


def rename(collection: Collection, new_name: str) -> None:
    name = (new_name or "").strip()
    if not name:
        return
    collection.name = name
    db.session.commit()


def delete(collection: Collection) -> None:
    """Remove the collection. Files inside fall back to no collection."""
    File.query.filter_by(collection_id=collection.id).update(
        {File.collection_id: None}
    )
    db.session.delete(collection)
    db.session.commit()


def assign_file(file: File, collection: Collection | None) -> None:
    file.collection_id = collection.id if collection is not None else None
    db.session.commit()


def files_in(collection: Collection) -> list[File]:
    return (
        File.query.filter_by(collection_id=collection.id)
        .order_by(File.id.desc())
        .all()
    )
