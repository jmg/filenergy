"""Programmatic API. Token auth via Bearer header.

`POST /api/v1/files`  — multipart upload
`POST /api/v1/ask`    — JSON question, returns text + sources
`GET  /api/v1/files`  — list files in workspace
"""
from dataclasses import asdict
from functools import wraps

from flask import Blueprint, g, jsonify, request

from filenergy.models import File
from filenergy.services import api_keys, billing, chat, conversations, events
from filenergy.services.file import FileService

api_v1_bp = Blueprint("api_v1", __name__)


def token_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.split()[1] if auth.lower().startswith("bearer ") else ""
        if not token:
            token = request.headers.get("X-API-Key", "")
        record = api_keys.verify(token)
        if record is None:
            return jsonify(error="Invalid API key"), 401
        # Stash the principal on g so the view can use it.
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


@api_v1_bp.route("/health")
def health():
    return jsonify(ok=True)


@api_v1_bp.route("/files", methods=["GET"])
@token_required
def list_files():
    files = (
        File.query.filter_by(workspace_id=g.workspace.id)
        .order_by(File.id.desc())
        .limit(int(request.args.get("limit", 100)))
        .all()
    )
    return jsonify(files=[
        {
            "id": f.id,
            "name": f.name,
            "url": f.url,
            "size_bytes": f.size_bytes,
            "indexed": f.indexed_at is not None,
            "status": f.index_status,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in files
    ])


@api_v1_bp.route("/files", methods=["POST"])
@token_required
def upload():
    try:
        billing.ensure_can_upload(g.workspace)
    except billing.QuotaExceeded as exc:
        return jsonify(error=str(exc), kind=exc.kind), 402
    body = FileService().save_file(request, g.user, g.workspace, sync_index=True)
    return body, 200, {"Content-Type": "application/json"}


@api_v1_bp.route("/ask", methods=["POST"])
@token_required
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
        conversation, answer.text, answer.sources
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
