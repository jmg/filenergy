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

from filenergy import app
from filenergy.services import chat, conversations, embeddings, events, rate_limit

ask_bp = Blueprint("ask", __name__)


@ask_bp.route("/")
@login_required
def index():
    status = {
        "anthropic": chat.is_configured(),
        "embeddings": embeddings.is_configured(),
    }
    convs = conversations.list_for_user(g.user)
    return render_template(
        "ask/index.html",
        status=status,
        conversations=convs,
        active_conversation=None,
    )


@ask_bp.route("/c/<int:conversation_id>")
@login_required
def view_conversation(conversation_id):
    conv = conversations.get_or_create(g.user, conversation_id)
    if conv.user_id != g.user.id:
        return "Not found", 404
    status = {
        "anthropic": chat.is_configured(),
        "embeddings": embeddings.is_configured(),
    }
    convs = conversations.list_for_user(g.user)
    return render_template(
        "ask/index.html",
        status=status,
        conversations=convs,
        active_conversation=conv,
        history=list(conv.messages),
    )


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
        rate_limit.check_ask(g.user)
    except rate_limit.RateLimited as exc:
        events.log_event(events.ASK_RATE_LIMITED, user=g.user, question=question[:120])
        resp = jsonify(error=str(exc), retry_after=exc.retry_after)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp

    conversation_id = payload.get("conversation_id")
    try:
        conversation_id = int(conversation_id) if conversation_id else None
    except (TypeError, ValueError):
        conversation_id = None

    conversation = conversations.get_or_create(g.user, conversation_id)
    history = conversations.history(conversation)
    conversations.add_user_message(conversation, question)
    events.log_event(
        events.ASK_QUESTION,
        user=g.user,
        conversation_id=conversation.id,
        question_chars=len(question),
    )

    try:
        answer = chat.answer_question(g.user, question, history=history)
    except chat.ChatUnavailable as exc:
        events.log_event(events.ASK_FAILED, user=g.user, reason=str(exc)[:200])
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        events.log_event(events.ASK_FAILED, user=g.user, reason=str(exc)[:200])
        return jsonify(error="Failed to answer: " + str(exc)), 500

    msg = conversations.add_assistant_message(conversation, answer.text, answer.sources)
    events.log_event(
        events.ASK_ANSWERED,
        user=g.user,
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
    """Server-Sent Events endpoint for incremental token rendering."""
    payload = request.get_json(silent=True) or request.form
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify(error="Question is required"), 400

    if not chat.is_configured():
        return jsonify(error="Chat is not configured"), 503

    try:
        rate_limit.check_ask(g.user)
    except rate_limit.RateLimited as exc:
        events.log_event(events.ASK_RATE_LIMITED, user=g.user)
        resp = jsonify(error=str(exc), retry_after=exc.retry_after)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp

    conversation_id = payload.get("conversation_id")
    try:
        conversation_id = int(conversation_id) if conversation_id else None
    except (TypeError, ValueError):
        conversation_id = None

    conversation = conversations.get_or_create(g.user, conversation_id)
    history_objs = conversations.history(conversation)
    # Snapshot history into plain dicts so the generator doesn't depend on
    # the SQLAlchemy session staying open.
    history_snapshot = [
        {"role": m.role, "content": m.content} for m in history_objs
    ]
    conversations.add_user_message(conversation, question)
    events.log_event(
        events.ASK_QUESTION,
        user=g.user,
        conversation_id=conversation.id,
        question_chars=len(question),
    )
    user_id = g.user.id
    conv_id = conversation.id

    def generate():
        # Send the conversation id immediately so the client can update state.
        yield chat._sse("meta", {"conversation_id": conv_id})

        from filenergy.models import User as UserModel

        # Re-fetch the user inside the generator's session lifetime.
        user_obj = UserModel.query.get(user_id)

        # Re-hydrate "messages" the chat layer expects: anything with role/content.
        class _M:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        history_objs_local = [_M(h["role"], h["content"]) for h in history_snapshot]

        full_text_parts: list[str] = []
        sources_payload: list[dict] = []
        for chunk_str in chat.stream_answer(user_obj, question, history=history_objs_local):
            yield chunk_str
            # Best-effort recover the final payload from the SSE strings.
            if chunk_str.startswith("event: token"):
                # extract the JSON after `data: `
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
                        payload = _json.loads(data_line[6:])
                        sources_payload = payload.get("sources", [])
                        full_text_parts = [payload.get("text", "")]
                    except Exception:
                        pass

        # Persist the assistant turn — we're still inside the request's app context.
        from filenergy.models import Conversation
        from filenergy.services import conversations as conv_service

        conv = Conversation.query.get(conv_id)
        if conv is not None:
            msg = conv_service.add_assistant_message(
                conv, "".join(full_text_parts), sources_payload
            )
            events.log_event(
                events.ASK_ANSWERED,
                user=user_obj,
                conversation_id=conv_id,
                message_id=msg.id,
                sources=len(sources_payload),
            )

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers=headers,
    )


@ask_bp.route("/c/<int:conversation_id>/delete", methods=["POST"])
@login_required
def delete_conversation(conversation_id):
    if conversations.delete(g.user, conversation_id):
        return jsonify(ok=True)
    return jsonify(error="Conversation not found"), 404
