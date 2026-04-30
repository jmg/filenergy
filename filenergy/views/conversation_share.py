"""Public read-only conversation share landing.

URL: /sc/<token>. Anonymous; gates on `ConversationShareLink.is_active()`.
Increments view_count on each successful render.
"""
from flask import Blueprint, abort, render_template

from filenergy.services import conversation_shares

conversation_share_bp = Blueprint("conversation_share", __name__)


@conversation_share_bp.route("/<token>")
def landing(token):
    link = conversation_shares.find_active(token)
    if link is None:
        abort(404)
    conversation_shares.record_view(link)
    return render_template(
        "conversation_share/landing.html",
        link=link,
        conversation=link.conversation,
        messages=list(link.conversation.messages),
    )
