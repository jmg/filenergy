from dataclasses import asdict

from flask import (
    Blueprint,
    Response,
    g,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from flask_login import login_required

from filenergy.services import (
    billing,
    chat,
    collections,
    conversations,
    embeddings,
    events,
    rate_limit,
)

ask_bp = Blueprint("ask", __name__)


def _ask_status():
    return {
        "anthropic": chat.is_configured(),
        "embeddings": embeddings.is_configured(),
    }


@ask_bp.route("/")
@login_required
def index():
    convs = conversations.list_for_user(g.user, g.workspace)
    coll_slug = request.args.get("collection")
    file_id = request.args.get("file_id")
    scope_collection = collections.get_by_slug(g.workspace, coll_slug) if coll_slug else None
    scope_file = None
    if file_id and file_id.isdigit():
        from filenergy.models import File
        scope_file = File.query.filter_by(
            id=int(file_id), workspace_id=g.workspace.id
        ).first()
    return render_template(
        "ask/index.html",
        status=_ask_status(),
        conversations=convs,
        active_conversation=None,
        usage=billing.usage_summary(g.workspace),
        scope_collection=scope_collection,
        scope_file=scope_file,
        all_collections=collections.list_for_workspace(g.workspace),
    )


@ask_bp.route("/c/<int:conversation_id>")
@login_required
def view_conversation(conversation_id):
    from filenergy.models import Conversation

    conv = Conversation.query.filter_by(
        id=conversation_id, user_id=g.user.id, workspace_id=g.workspace.id
    ).first()
    if conv is None:
        return "Not found", 404
    convs = conversations.list_for_user(g.user, g.workspace)
    return render_template(
        "ask/index.html",
        status=_ask_status(),
        conversations=convs,
        active_conversation=conv,
        history=list(conv.messages),
        usage=billing.usage_summary(g.workspace),
        scope_collection=None,
        scope_file=None,
        all_collections=collections.list_for_workspace(g.workspace),
    )


def _coerce_conversation_id(payload):
    cid = payload.get("conversation_id")
    try:
        return int(cid) if cid else None
    except (TypeError, ValueError):
        return None


def _coerce_scope(payload):
    """Optional scope: file_id (single file) OR collection_id."""
    file_id = payload.get("file_id")
    coll_id = payload.get("collection_id")
    try:
        file_id = int(file_id) if file_id else None
    except (TypeError, ValueError):
        file_id = None
    try:
        coll_id = int(coll_id) if coll_id else None
    except (TypeError, ValueError):
        coll_id = None
    return coll_id, file_id


def _validate_scope(workspace, collection_id, file_id):
    """Reject scope IDs that aren't part of this workspace."""
    from filenergy.models import Collection, File

    if collection_id is not None:
        if Collection.query.filter_by(
            id=collection_id, workspace_id=workspace.id
        ).first() is None:
            return False
    if file_id is not None:
        if File.query.filter_by(
            id=file_id, workspace_id=workspace.id
        ).first() is None:
            return False
    return True


@ask_bp.route("/", methods=["POST"])
@login_required
def ask():
    payload = request.get_json(silent=True) or request.form
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify(error="Question is required"), 400

    if not chat.is_configured():
        return jsonify(
            error=(
                "Chat is not configured. Set ANTHROPIC_API_KEY and "
                "VOYAGE_API_KEY in your environment."
            )
        ), 503

    try:
        billing.ensure_can_ask(g.workspace)
    except billing.QuotaExceeded as exc:
        events.log_event(
            events.ASK_QUOTA_EXCEEDED,
            user=g.user, workspace_id=g.workspace.id, kind=exc.kind,
        )
        return jsonify(error=str(exc), kind=exc.kind), 402

    try:
        rate_limit.check_ask(g.user)
    except rate_limit.RateLimited as exc:
        events.log_event(
            events.ASK_RATE_LIMITED,
            user=g.user, workspace_id=g.workspace.id, question=question[:120],
        )
        resp = jsonify(error=str(exc), retry_after=exc.retry_after)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp

    coll_id, file_id = _coerce_scope(payload)
    if not _validate_scope(g.workspace, coll_id, file_id):
        return jsonify(error="Scope not in this workspace"), 404

    conversation = conversations.get_or_create(
        g.user, g.workspace, _coerce_conversation_id(payload)
    )
    history = conversations.history(conversation)
    conversations.add_user_message(conversation, question)
    events.log_event(
        events.ASK_QUESTION,
        user=g.user,
        workspace_id=g.workspace.id,
        conversation_id=conversation.id,
        question_chars=len(question),
        collection_id=coll_id,
        file_id=file_id,
    )

    try:
        answer = chat.answer_question(
            g.workspace, question, history=history,
            collection_id=coll_id, file_id=file_id,
        )
    except chat.ChatUnavailable as exc:
        events.log_event(
            events.ASK_FAILED,
            user=g.user, workspace_id=g.workspace.id, reason=str(exc)[:200],
        )
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        events.log_event(
            events.ASK_FAILED,
            user=g.user, workspace_id=g.workspace.id, reason=str(exc)[:200],
        )
        return jsonify(error="Failed to answer: " + str(exc)), 500

    msg = conversations.add_assistant_message(
        conversation, answer.text, answer.sources,
        chunk_citations=answer.chunk_citations,
    )
    events.log_event(
        events.ASK_ANSWERED,
        user=g.user,
        workspace_id=g.workspace.id,
        conversation_id=conversation.id,
        message_id=msg.id,
        sources=len(answer.sources),
    )

    return jsonify(
        conversation_id=conversation.id,
        message_id=msg.id,
        answer=answer.text,
        sources=[asdict(s) for s in answer.sources],
    )


@ask_bp.route("/stream", methods=["POST"])
@login_required
def ask_stream():
    payload = request.get_json(silent=True) or request.form
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify(error="Question is required"), 400

    # Optional vision: list of {media_type, data (base64)} dicts. We cap
    # at 5 images per turn and 5 MB each so a malicious upload can't
    # blow up the prompt size — Claude's per-message limit handles the
    # rest, but failing fast here saves a round-trip.
    images = payload.get("images") or []
    if not isinstance(images, list):
        images = []
    images = [
        img for img in images
        if isinstance(img, dict)
        and isinstance(img.get("media_type"), str)
        and isinstance(img.get("data"), str)
        and img["media_type"].startswith("image/")
        and len(img["data"]) <= 5 * 1024 * 1024  # base64-encoded length
    ][:5]

    if not chat.is_configured():
        return jsonify(error="Chat is not configured"), 503

    try:
        billing.ensure_can_ask(g.workspace)
    except billing.QuotaExceeded as exc:
        events.log_event(
            events.ASK_QUOTA_EXCEEDED,
            user=g.user, workspace_id=g.workspace.id, kind=exc.kind,
        )
        return jsonify(error=str(exc), kind=exc.kind), 402

    try:
        rate_limit.check_ask(g.user)
    except rate_limit.RateLimited as exc:
        events.log_event(
            events.ASK_RATE_LIMITED, user=g.user, workspace_id=g.workspace.id,
        )
        resp = jsonify(error=str(exc), retry_after=exc.retry_after)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp

    coll_id, file_id = _coerce_scope(payload)
    if not _validate_scope(g.workspace, coll_id, file_id):
        return jsonify(error="Scope not in this workspace"), 404

    conversation = conversations.get_or_create(
        g.user, g.workspace, _coerce_conversation_id(payload)
    )
    history_snapshot = [
        {"role": m.role, "content": m.content}
        for m in conversations.history(conversation)
    ]
    conversations.add_user_message(conversation, question)
    events.log_event(
        events.ASK_QUESTION,
        user=g.user,
        workspace_id=g.workspace.id,
        conversation_id=conversation.id,
        question_chars=len(question),
        collection_id=coll_id,
        file_id=file_id,
    )
    user_id = g.user.id
    workspace_id = g.workspace.id
    conv_id = conversation.id
    scope_collection_id = coll_id
    scope_file_id = file_id
    request_images = images

    def generate():
        yield chat._sse("meta", {"conversation_id": conv_id})

        from filenergy.models import Conversation, User, Workspace
        from filenergy.services import conversations as conv_service

        user_obj = User.query.get(user_id)
        workspace_obj = Workspace.query.get(workspace_id)

        class _M:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        history_objs_local = [_M(h["role"], h["content"]) for h in history_snapshot]

        full_text_parts: list[str] = []
        sources_payload: list[dict] = []
        chunk_citations: list = []
        for chunk_str in chat.stream_answer(
            workspace_obj, question, history=history_objs_local,
            collection_id=scope_collection_id, file_id=scope_file_id,
            images=request_images,
        ):
            yield chunk_str
            if chunk_str.startswith("event: token"):
                data_line = chunk_str.split("\n", 1)[1]
                if data_line.startswith("data: "):
                    import json as _json
                    try:
                        full_text_parts.append(
                            _json.loads(data_line[6:])["text"]
                        )
                    except Exception:
                        pass
            elif chunk_str.startswith("event: done"):
                data_line = chunk_str.split("\n", 1)[1]
                if data_line.startswith("data: "):
                    import json as _json
                    try:
                        parsed = _json.loads(data_line[6:])
                        sources_payload = parsed.get("sources", [])
                        chunk_citations = parsed.get("chunk_citations", [])
                        full_text_parts = [parsed.get("text", "")]
                    except Exception:
                        pass

        conv = Conversation.query.get(conv_id)
        if conv is not None:
            msg = conv_service.add_assistant_message(
                conv, "".join(full_text_parts), sources_payload,
                chunk_citations=chunk_citations,
            )
            events.log_event(
                events.ASK_ANSWERED,
                user=user_obj,
                workspace_id=workspace_id,
                conversation_id=conv_id,
                message_id=msg.id,
                sources=len(sources_payload),
            )
            # Emit a final meta event so the browser can attach the saved
            # message id to the rendered bubble (drives feedback / regenerate).
            import json as _json
            yield (
                "event: meta\n"
                f"data: {_json.dumps({'message_id': msg.id})}\n\n"
            )

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers=headers,
    )


@ask_bp.route("/c/<int:conversation_id>/delete", methods=["POST"])
@login_required
def delete_conversation(conversation_id):
    if conversations.delete(g.user, g.workspace, conversation_id):
        return jsonify(ok=True)
    return jsonify(error="Conversation not found"), 404


@ask_bp.route("/c/<int:conversation_id>/rename", methods=["POST"])
@login_required
def rename_conversation(conversation_id):
    """Inline rename of a conversation title from the chat header."""
    from filenergy.models import Conversation

    conv = Conversation.query.filter_by(
        id=conversation_id, user_id=g.user.id, workspace_id=g.workspace.id,
    ).first()
    if conv is None:
        return jsonify(error="Not found"), 404
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify(error="Title is required"), 400
    conv.title = title[:255]
    from filenergy import db
    db.session.commit()
    return jsonify(ok=True, title=conv.title)


@ask_bp.route("/c/<int:conversation_id>/pin", methods=["POST"])
@login_required
def pin_conversation(conversation_id):
    """Toggle pin state. Pinned conversations float to the top of the
    sidebar so users keep their long-running threads at hand."""
    from filenergy import db
    from filenergy.models import Conversation, utcnow

    conv = Conversation.query.filter_by(
        id=conversation_id, user_id=g.user.id, workspace_id=g.workspace.id,
    ).first()
    if conv is None:
        return jsonify(error="Not found"), 404
    conv.pinned_at = None if conv.pinned_at else utcnow()
    db.session.commit()
    return jsonify(ok=True, pinned=conv.pinned_at is not None)


@ask_bp.route("/c/<int:conversation_id>/archive", methods=["POST"])
@login_required
def archive_conversation(conversation_id):
    """Toggle archive state. Archived threads are hidden from the
    sidebar but kept in the DB for export + audit."""
    from filenergy import db
    from filenergy.models import Conversation, utcnow

    conv = Conversation.query.filter_by(
        id=conversation_id, user_id=g.user.id, workspace_id=g.workspace.id,
    ).first()
    if conv is None:
        return jsonify(error="Not found"), 404
    conv.archived_at = None if conv.archived_at else utcnow()
    db.session.commit()
    return jsonify(ok=True, archived=conv.archived_at is not None)


@ask_bp.route("/feedback", methods=["POST"])
@login_required
def feedback():
    """Record a thumbs up/down on an assistant message — feeds the
    eval dashboard and lets us spot regressions in answer quality."""
    from filenergy import db
    from filenergy.models import Conversation, Message, MessageFeedback

    payload = request.get_json(silent=True) or {}
    message_id = payload.get("message_id")
    rating = (payload.get("rating") or "").strip()
    if rating not in ("up", "down"):
        return jsonify(error="rating must be 'up' or 'down'"), 400
    msg = Message.query.get(message_id) if message_id else None
    if msg is None:
        return jsonify(error="Message not found"), 404
    conv = Conversation.query.get(msg.conversation_id)
    if conv is None or conv.user_id != g.user.id:
        return jsonify(error="Not found"), 404
    fb = MessageFeedback.query.filter_by(
        message_id=msg.id, user_id=g.user.id,
    ).first()
    if fb is None:
        fb = MessageFeedback(
            message_id=msg.id, user_id=g.user.id, rating=rating,
        )
        db.session.add(fb)
    else:
        fb.rating = rating
    db.session.commit()
    events.log_event(
        events.ASK_FEEDBACK,
        user=g.user, workspace_id=g.workspace.id,
        message_id=msg.id, rating=rating,
    )
    return jsonify(ok=True)


@ask_bp.route("/c/<int:conversation_id>/share", methods=["POST"])
@login_required
def share_conversation(conversation_id):
    """Mint a public read-only link to this conversation."""
    from filenergy.models import Conversation
    from filenergy.services import conversation_shares

    conv = Conversation.query.filter_by(
        id=conversation_id, user_id=g.user.id, workspace_id=g.workspace.id,
    ).first()
    if conv is None:
        return jsonify(error="Not found"), 404
    ttl_raw = (request.form.get("ttl_hours") or
               (request.get_json(silent=True) or {}).get("ttl_hours"))
    try:
        ttl = int(ttl_raw) if ttl_raw else None
    except (TypeError, ValueError):
        ttl = None
    link = conversation_shares.create(conv, created_by=g.user, ttl_hours=ttl)
    return jsonify(token=link.token, url=f"/sc/{link.token}",
                   expires_at=link.expires_at.isoformat() if link.expires_at else None)


@ask_bp.route("/c/<int:conversation_id>/share/<int:link_id>/revoke", methods=["POST"])
@login_required
def revoke_conversation_share(conversation_id, link_id):
    from filenergy.models import Conversation, ConversationShareLink
    from filenergy.services import conversation_shares

    link = (
        ConversationShareLink.query.join(Conversation)
        .filter(
            ConversationShareLink.id == link_id,
            Conversation.id == conversation_id,
            Conversation.user_id == g.user.id,
            Conversation.workspace_id == g.workspace.id,
        )
        .first()
    )
    if link is None:
        return jsonify(error="Not found"), 404
    conversation_shares.revoke(link)
    return jsonify(ok=True)


def _conversation_for_export(conversation_id):
    from filenergy.models import Conversation
    return Conversation.query.filter_by(
        id=conversation_id, user_id=g.user.id, workspace_id=g.workspace.id,
    ).first()


@ask_bp.route("/c/<int:conversation_id>/export.pdf")
@login_required
def export_pdf(conversation_id):
    from flask import Response

    from filenergy.services import exporting

    conv = _conversation_for_export(conversation_id)
    if conv is None:
        return "Not found", 404
    try:
        body = exporting.to_pdf(conv)
    except exporting.ExportUnavailable as exc:
        return jsonify(error=str(exc)), 503
    return Response(
        body, mimetype="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="conversation-{conv.id}.pdf"',
        },
    )


@ask_bp.route("/c/<int:conversation_id>/export.docx")
@login_required
def export_docx(conversation_id):
    from flask import Response

    from filenergy.services import exporting

    conv = _conversation_for_export(conversation_id)
    if conv is None:
        return "Not found", 404
    try:
        body = exporting.to_docx(conv)
    except exporting.ExportUnavailable as exc:
        return jsonify(error=str(exc)), 503
    return Response(
        body,
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        headers={
            "Content-Disposition":
                f'attachment; filename="conversation-{conv.id}.docx"',
        },
    )


@ask_bp.route("/c/<int:conversation_id>/export.md")
@login_required
def export_markdown(conversation_id):
    """Render the thread as Markdown so users can paste it into docs."""
    from flask import Response

    from filenergy.models import Conversation

    conv = Conversation.query.filter_by(
        id=conversation_id, user_id=g.user.id, workspace_id=g.workspace.id,
    ).first()
    if conv is None:
        return "Not found", 404

    lines: list[str] = [f"# {conv.title or 'Conversation'}", ""]
    if conv.created_at:
        lines.append(f"_Created {conv.created_at.strftime('%Y-%m-%d %H:%M')}_")
        lines.append("")

    for msg in conv.messages:
        speaker = "**You**" if msg.role == "user" else "**Assistant**"
        lines.append(speaker)
        lines.append("")
        lines.append(msg.content or "")
        lines.append("")
        if msg.role == "assistant" and msg.sources_json:
            try:
                import json as _json
                sources = _json.loads(msg.sources_json)
                if sources:
                    lines.append("Sources:")
                    for s in sources:
                        lines.append(f"- {s.get('name', '?')}")
                    lines.append("")
            except Exception:
                pass
        lines.append("---")
        lines.append("")

    body = "\n".join(lines)
    return Response(
        body,
        mimetype="text/markdown",
        headers={
            "Content-Disposition":
                f'attachment; filename="conversation-{conv.id}.md"',
        },
    )
