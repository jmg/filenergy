from dataclasses import asdict

from flask import Blueprint, g, jsonify, render_template, request
from flask_login import login_required

from filenergy.services import chat, embeddings

ask_bp = Blueprint("ask", __name__)


@ask_bp.route("/")
@login_required
def index():
    status = {
        "anthropic": bool(chat.is_configured() or False),
        "embeddings": embeddings.is_configured(),
    }
    return render_template("ask/index.html", status=status)


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
        answer = chat.answer_question(g.user, question)
    except chat.ChatUnavailable as exc:
        return jsonify(error=str(exc)), 503

    return jsonify(
        answer=answer.text,
        sources=[asdict(s) for s in answer.sources],
    )
