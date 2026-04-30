"""Programmatic API. Token auth via Bearer header.

Auth: `Authorization: Bearer <fk_...>` or `X-API-Key: <fk_...>`. Keys
mint at `/settings/keys`; pass scopes to restrict what the bearer can
do (`files:read`, `ask:write`, etc.). An empty-scope key has full
access (back-compat).
"""
from dataclasses import asdict
from functools import wraps

from flask import Blueprint, g, jsonify, request

from filenergy import db
from filenergy.models import (
    Collection, ConversationShareLink, File, ShareLink, User,
    Workspace, WorkspaceMember, WorkspaceInvitation,
    WebhookSubscription, ApiKey, Conversation, Message, MessageCitation,
)
from filenergy.services import (
    api_keys, billing, chat, collections as coll_service, conversation_shares,
    conversations, events, share_links, webhooks, workspaces,
)
from filenergy.services.file import FileService

api_v1_bp = Blueprint("api_v1", __name__)


def token_required(*scopes: str):
    """Decorator: enforce a Bearer token + every scope in `scopes`.

    Empty `scopes` argument list means the route only needs auth, no
    specific scope. The key's stored scopes (if any) must include each
    of `scopes`; an empty stored-scope list = full access.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            token = auth.split()[1] if auth.lower().startswith("bearer ") else ""
            if not token:
                token = request.headers.get("X-API-Key", "")
            record = api_keys.verify(token)
            if record is None:
                return jsonify(error="Invalid API key"), 401
            for scope in scopes:
                if not record.has_scope(scope):
                    return jsonify(error=f"Missing scope: {scope}"), 403
            g.api_key = record
            g.user = record.user
            g.workspace = record.workspace
            events.log_event(
                events.API_KEY_USED,
                user=record.user,
                workspace_id=record.workspace_id,
                key_id=record.id,
                endpoint=request.endpoint,
            )
            return f(*args, **kwargs)

        return wrapper

    return decorator


# ---- health ----


@api_v1_bp.route("/health")
def health():
    return jsonify(ok=True)


# ---- files ----


@api_v1_bp.route("/files", methods=["GET"])
@token_required("files:read")
def list_files():
    files = (
        File.query.filter_by(workspace_id=g.workspace.id)
        .order_by(File.id.desc())
        .limit(int(request.args.get("limit", 100)))
        .all()
    )
    return jsonify(files=[_file_to_dict(f) for f in files])


@api_v1_bp.route("/files", methods=["POST"])
@token_required("files:write")
def upload():
    try:
        billing.ensure_can_upload(g.workspace)
    except billing.QuotaExceeded as exc:
        return jsonify(error=str(exc), kind=exc.kind), 402
    body = FileService().save_file(request, g.user, g.workspace, sync_index=True)
    return body, 200, {"Content-Type": "application/json"}


@api_v1_bp.route("/files/<int:file_id>", methods=["GET"])
@token_required("files:read")
def get_file(file_id):
    f = File.query.filter_by(id=file_id, workspace_id=g.workspace.id).first()
    if f is None:
        return jsonify(error="Not found"), 404
    return jsonify(_file_to_dict(f, include_summary=True))


@api_v1_bp.route("/files/<int:file_id>", methods=["DELETE"])
@token_required("files:write")
def delete_file(file_id):
    f = File.query.filter_by(id=file_id, workspace_id=g.workspace.id).first()
    if f is None:
        return jsonify(error="Not found"), 404
    FileService().delete(f)
    return jsonify(ok=True)


def _file_to_dict(f, *, include_summary=False):
    out = {
        "id": f.id,
        "name": f.name,
        "url": f.url,
        "size_bytes": f.size_bytes,
        "indexed": f.indexed_at is not None,
        "status": f.index_status,
        "is_public": bool(f.is_public),
        "collection_id": f.collection_id,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }
    if include_summary:
        out["summary"] = f.summary
        out["suggested_questions"] = f.suggested_questions
    return out


# ---- ask + conversations ----


@api_v1_bp.route("/ask", methods=["POST"])
@token_required("ask:write")
def ask():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify(error="Missing 'question'"), 400

    if not chat.is_configured():
        return jsonify(error="Chat is not configured"), 503

    try:
        billing.ensure_can_ask(g.workspace)
    except billing.QuotaExceeded as exc:
        return jsonify(error=str(exc), kind=exc.kind), 402

    cid = payload.get("conversation_id")
    try:
        cid = int(cid) if cid else None
    except (TypeError, ValueError):
        cid = None

    conversation = conversations.get_or_create(g.user, g.workspace, cid)
    history = conversations.history(conversation)
    conversations.add_user_message(conversation, question)
    events.log_event(
        events.ASK_QUESTION,
        user=g.user, workspace_id=g.workspace.id,
        conversation_id=conversation.id, via="api",
    )

    try:
        answer = chat.answer_question(g.workspace, question, history=history)
    except chat.ChatUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        return jsonify(error=str(exc)), 500

    msg = conversations.add_assistant_message(
        conversation, answer.text, answer.sources,
        chunk_citations=answer.chunk_citations,
    )
    events.log_event(
        events.ASK_ANSWERED,
        user=g.user, workspace_id=g.workspace.id,
        conversation_id=conversation.id, message_id=msg.id, via="api",
    )

    return jsonify(
        conversation_id=conversation.id,
        message_id=msg.id,
        answer=answer.text,
        sources=[asdict(s) for s in answer.sources],
    )


@api_v1_bp.route("/conversations", methods=["GET"])
@token_required("conversations:read")
def list_conversations():
    rows = (
        Conversation.query.filter_by(workspace_id=g.workspace.id)
        .order_by(Conversation.id.desc())
        .limit(int(request.args.get("limit", 100)))
        .all()
    )
    return jsonify(conversations=[
        {
            "id": c.id,
            "title": c.title,
            "user_id": c.user_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in rows
    ])


@api_v1_bp.route("/conversations/<int:conv_id>", methods=["GET"])
@token_required("conversations:read")
def get_conversation(conv_id):
    conv = Conversation.query.filter_by(
        id=conv_id, workspace_id=g.workspace.id
    ).first()
    if conv is None:
        return jsonify(error="Not found"), 404
    return jsonify({
        "id": conv.id,
        "title": conv.title,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "sources_json": m.sources_json,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in conv.messages
        ],
    })


@api_v1_bp.route("/conversations/<int:conv_id>", methods=["DELETE"])
@token_required("conversations:write")
def delete_conversation(conv_id):
    conv = Conversation.query.filter_by(
        id=conv_id, workspace_id=g.workspace.id
    ).first()
    if conv is None:
        return jsonify(error="Not found"), 404
    db.session.delete(conv)
    db.session.commit()
    return jsonify(ok=True)


# ---- collections ----


@api_v1_bp.route("/collections", methods=["GET"])
@token_required("collections:read")
def list_collections():
    return jsonify(collections=[
        {"id": c.id, "name": c.name, "slug": c.slug, "description": c.description}
        for c in coll_service.list_for_workspace(g.workspace)
    ])


@api_v1_bp.route("/collections", methods=["POST"])
@token_required("collections:write")
def create_collection():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify(error="Missing 'name'"), 400
    coll = coll_service.create(
        g.workspace, name, payload.get("description") or "",
    )
    return jsonify(id=coll.id, slug=coll.slug, name=coll.name)


@api_v1_bp.route("/collections/<int:coll_id>", methods=["DELETE"])
@token_required("collections:write")
def delete_collection(coll_id):
    coll = coll_service.get(g.workspace, coll_id)
    if coll is None:
        return jsonify(error="Not found"), 404
    coll_service.delete(coll)
    return jsonify(ok=True)


@api_v1_bp.route("/collections/<int:coll_id>/files/<int:file_id>", methods=["PUT"])
@token_required("collections:write", "files:write")
def assign_file_to_collection(coll_id, file_id):
    coll = coll_service.get(g.workspace, coll_id)
    if coll is None:
        return jsonify(error="Collection not found"), 404
    f = File.query.filter_by(id=file_id, workspace_id=g.workspace.id).first()
    if f is None:
        return jsonify(error="File not found"), 404
    coll_service.assign_file(f, coll)
    return jsonify(ok=True, collection_id=coll.id)


# ---- share links (files) ----


@api_v1_bp.route("/files/<int:file_id>/share-links", methods=["POST"])
@token_required("share_links:write")
def create_share_link(file_id):
    f = File.query.filter_by(id=file_id, workspace_id=g.workspace.id).first()
    if f is None:
        return jsonify(error="Not found"), 404
    payload = request.get_json(silent=True) or {}
    ttl = payload.get("ttl_hours")
    max_dl = payload.get("max_downloads")
    link = share_links.create(
        f, created_by=g.user,
        ttl_hours=int(ttl) if ttl else None,
        max_downloads=int(max_dl) if max_dl else None,
    )
    return jsonify(token=link.token, expires_at=link.expires_at.isoformat() if link.expires_at else None)


@api_v1_bp.route("/share-links/<int:link_id>", methods=["DELETE"])
@token_required("share_links:write")
def revoke_share_link(link_id):
    link = (
        ShareLink.query.join(File, ShareLink.file_id == File.id)
        .filter(ShareLink.id == link_id, File.workspace_id == g.workspace.id)
        .first()
    )
    if link is None:
        return jsonify(error="Not found"), 404
    share_links.revoke(link)
    return jsonify(ok=True)


# ---- webhooks ----


@api_v1_bp.route("/webhooks", methods=["GET"])
@token_required("webhooks:read")
def list_webhooks():
    rows = webhooks.list_for_workspace(g.workspace)
    return jsonify(webhooks=[
        {
            "id": s.id,
            "url": s.url,
            "events": s.event_types,
            "enabled": bool(s.enabled),
            "last_status": s.last_status,
            "failure_count": s.failure_count or 0,
        }
        for s in rows
    ])


@api_v1_bp.route("/webhooks", methods=["POST"])
@token_required("webhooks:write")
def create_webhook():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    event_types = payload.get("events") or []
    if not url or not isinstance(event_types, list):
        return jsonify(error="Missing 'url' or 'events'"), 400
    sub, secret = webhooks.create(g.workspace, url, event_types)
    return jsonify(id=sub.id, secret=secret, events=sub.event_types)


@api_v1_bp.route("/webhooks/<int:sub_id>", methods=["DELETE"])
@token_required("webhooks:write")
def delete_webhook(sub_id):
    sub = webhooks.get(g.workspace, sub_id)
    if sub is None:
        return jsonify(error="Not found"), 404
    webhooks.delete(sub)
    return jsonify(ok=True)


# ---- members + invitations ----


@api_v1_bp.route("/members", methods=["GET"])
@token_required("members:read")
def list_members():
    rows = workspaces.members(g.workspace)
    user_ids = [m.user_id for m in rows]
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()}
    return jsonify(members=[
        {
            "user_id": m.user_id,
            "email": users.get(m.user_id).email if users.get(m.user_id) else None,
            "role": m.role,
        }
        for m in rows
    ])


@api_v1_bp.route("/invitations", methods=["POST"])
@token_required("members:write")
def create_invitation():
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    role = (payload.get("role") or "member")
    if not email:
        return jsonify(error="Missing 'email'"), 400
    inv = workspaces.invite(g.workspace, g.user, email, role)
    return jsonify(id=inv.id, token=inv.token, role=inv.role)


# ---- conversation share links ----


@api_v1_bp.route("/conversations/<int:conv_id>/share-links", methods=["POST"])
@token_required("share_links:write", "conversations:read")
def share_conversation(conv_id):
    conv = Conversation.query.filter_by(
        id=conv_id, workspace_id=g.workspace.id
    ).first()
    if conv is None:
        return jsonify(error="Not found"), 404
    payload = request.get_json(silent=True) or {}
    ttl = payload.get("ttl_hours")
    link = conversation_shares.create(
        conv, created_by=g.user,
        ttl_hours=int(ttl) if ttl else None,
    )
    return jsonify(token=link.token, url=f"/sc/{link.token}")
