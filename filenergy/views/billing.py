"""Stripe webhook receiver."""
from flask import Blueprint, jsonify, request

from filenergy.services import billing, events

billing_bp = Blueprint("billing", __name__)


@billing_bp.route("/stripe", methods=["POST"])
def stripe_webhook():
    if not billing.is_configured():
        return jsonify(error="Stripe not configured"), 503
    try:
        result = billing.handle_webhook(
            request.data, request.headers.get("Stripe-Signature", "")
        )
    except billing.BillingError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 400

    events.log_event(
        events.BILLING_SUBSCRIPTION_UPDATED,
        workspace_id=result.get("workspace_id"),
        plan=result.get("plan"),
    )
    return jsonify(result)
