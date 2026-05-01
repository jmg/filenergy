"""Unified workspace search.

Powers the ⌘K command palette: one query, results across files,
conversations, collections, settings pages.

Files / conversations / collections each return at most `limit` rows;
nav targets are static, deterministic, and ranked above empty result
sets so users always see *something*.
"""
from __future__ import annotations

from filenergy.models import Collection, Conversation, File


# Static nav commands always available — same set the cmdk hard-codes,
# but exposed here so the API endpoint can sort+filter consistently.
NAV_COMMANDS = [
    {"label": "Go to Ask",          "href": "/ask/",         "kind": "nav", "icon": "i-chat"},
    {"label": "Upload files",       "href": "/file/upload/", "kind": "nav", "icon": "i-upload"},
    {"label": "Browse files",       "href": "/file/list/",   "kind": "nav", "icon": "i-files"},
    {"label": "Collections",        "href": "/collections/", "kind": "nav", "icon": "i-folder"},
    {"label": "Connectors",         "href": "/connectors/",  "kind": "nav", "icon": "i-plug"},
    {"label": "Dashboard",          "href": "/dashboard/",   "kind": "nav", "icon": "i-chart"},
    {"label": "Evals dashboard",    "href": "/dashboard/evals", "kind": "nav", "icon": "i-chart"},
    {"label": "Settings: profile",  "href": "/settings/profile",   "kind": "nav", "icon": "i-cog"},
    {"label": "Settings: security", "href": "/settings/security",  "kind": "nav", "icon": "i-cog"},
    {"label": "Settings: workspace","href": "/settings/workspace", "kind": "nav", "icon": "i-cog"},
    {"label": "Settings: API keys", "href": "/settings/keys",      "kind": "nav", "icon": "i-cog"},
    {"label": "Settings: webhooks", "href": "/settings/webhooks",  "kind": "nav", "icon": "i-cog"},
    {"label": "Settings: billing",  "href": "/settings/billing",   "kind": "nav", "icon": "i-cog"},
    {"label": "Audit log",          "href": "/audit/",       "kind": "nav", "icon": "i-files"},
    {"label": "Sign out",           "href": "/user/logout/", "kind": "nav", "icon": "i-x"},
]


def search(workspace, query: str, *, limit: int = 6) -> list[dict]:
    """Return a flat list of matched results. Each entry is a dict with
    `kind`, `label`, `href`, `icon` (and optionally `subtitle`).

    Empty query → just the nav commands. The frontend keeps showing them
    in the natural cmdk order, which is the intent (open palette, no
    typing, see jumps).
    """
    q = (query or "").strip().lower()
    if not q:
        return list(NAV_COMMANDS)

    results: list[dict] = []

    # Nav commands first — instant, no DB hit.
    for cmd in NAV_COMMANDS:
        if q in cmd["label"].lower():
            results.append(cmd)

    if workspace is None:
        return results

    # Files (LIKE on name; cap at `limit`).
    file_q = (
        File.query
        .filter(File.workspace_id == workspace.id)
        .filter(File.name.ilike(f"%{q}%"))
        .order_by(File.id.desc())
        .limit(limit)
    )
    for f in file_q:
        size_kb = (f.size_bytes or 0) / 1024
        results.append({
            "kind": "file",
            "label": f.name,
            "subtitle": f"{size_kb:.1f} KB · {f.index_status}",
            "href": f"/file/{f.id}",
            "icon": "i-files",
        })

    # Conversations (title LIKE).
    conv_q = (
        Conversation.query
        .filter(Conversation.workspace_id == workspace.id)
        .filter(Conversation.title.ilike(f"%{q}%"))
        .order_by(Conversation.id.desc())
        .limit(limit)
    )
    for c in conv_q:
        results.append({
            "kind": "conversation",
            "label": c.title or "Untitled",
            "subtitle": "Conversation",
            "href": f"/ask/c/{c.id}",
            "icon": "i-chat",
        })

    # Collections (name + description).
    coll_q = (
        Collection.query
        .filter(Collection.workspace_id == workspace.id)
        .filter(Collection.name.ilike(f"%{q}%"))
        .order_by(Collection.id.desc())
        .limit(limit)
    )
    for c in coll_q:
        results.append({
            "kind": "collection",
            "label": c.name,
            "subtitle": "Collection",
            "href": f"/collections/{c.slug}",
            "icon": "i-folder",
        })

    return results
