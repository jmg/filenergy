"""Stripe billing: customers, Checkout sessions, webhook reconciliation,
and plan-quota checks.

Stays usable when Stripe isn't configured: `is_configured()` is False and
all checkout/webhook endpoints surface a clear 503. Quota checks always
work since they're DB-only.
"""
from __future__ import annotations

import logging

from filenergy import db, settings
from filenergy.models import Event, File, Workspace

log = logging.getLogger(__name__)


class BillingError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(settings.STRIPE_SECRET_KEY)


def _client():
    if not is_configured():
        raise BillingError("Stripe is not configured")
    try:
        import stripe  # type: ignore
    except ImportError as exc:
        raise BillingError("stripe package not installed") from exc
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def plan_for(workspace: Workspace) -> dict:
    return settings.PLAN_LIMITS.get(
        workspace.plan or "free", settings.PLAN_LIMITS["free"]
    )


def storage_used_bytes(workspace: Workspace) -> int:
    total = (
        db.session.query(db.func.coalesce(db.func.sum(File.size_bytes), 0))
        .filter(File.workspace_id == workspace.id)
        .scalar()
    )
    return int(total or 0)


def files_count(workspace: Workspace) -> int:
    return File.query.filter_by(workspace_id=workspace.id).count()


def asks_this_month(workspace: Workspace) -> int:
    from datetime import datetime, timezone

    start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return (
        Event.query.filter_by(workspace_id=workspace.id, type="ask.question")
        .filter(Event.created_at >= start)
        .count()
    )


# ---- Quota gates ----


class QuotaExceeded(RuntimeError):
    def __init__(self, kind: str, limit: int, used: int):
        super().__init__(f"{kind} quota exceeded: {used}/{limit}")
        self.kind = kind
        self.limit = limit
        self.used = used


def ensure_can_upload(workspace: Workspace, additional_bytes: int = 0) -> None:
    plan = plan_for(workspace)
    if files_count(workspace) >= plan["files_max"]:
        raise QuotaExceeded("files", plan["files_max"], files_count(workspace))
    used = storage_used_bytes(workspace) + additional_bytes
    if used > plan["storage_bytes_max"]:
        raise QuotaExceeded(
            "storage_bytes", plan["storage_bytes_max"], used
        )


def ensure_can_ask(workspace: Workspace) -> None:
    plan = plan_for(workspace)
    used = asks_this_month(workspace)
    if used >= plan["asks_per_month"]:
        raise QuotaExceeded("asks_per_month", plan["asks_per_month"], used)


def usage_summary(workspace: Workspace) -> dict:
    plan = plan_for(workspace)
    return {
        "plan": workspace.plan or "free",
        "label": plan["label"],
        "files": {"used": files_count(workspace), "limit": plan["files_max"]},
        "storage_bytes": {
            "used": storage_used_bytes(workspace),
            "limit": plan["storage_bytes_max"],
        },
        "asks_this_month": {
            "used": asks_this_month(workspace),
            "limit": plan["asks_per_month"],
        },
        "members_max": plan["members_max"],
    }


# ---- Stripe operations ----


def ensure_customer(workspace: Workspace) -> str:
    """Create the Stripe customer if missing; return its id."""
    if workspace.stripe_customer_id:
        return workspace.stripe_customer_id
    stripe = _client()
    customer = stripe.Customer.create(
        name=workspace.name,
        metadata={"workspace_id": str(workspace.id)},
    )
    workspace.stripe_customer_id = customer["id"]
    db.session.commit()
    return customer["id"]


def create_checkout_session(workspace: Workspace, plan_id: str) -> str:
    """Return the URL the user should be redirected to."""
    if plan_id not in ("pro", "team"):
        raise BillingError("Unknown plan")
    price_id = (
        settings.STRIPE_PRICE_PRO if plan_id == "pro" else settings.STRIPE_PRICE_TEAM
    )
    if not price_id:
        raise BillingError(f"STRIPE_PRICE_{plan_id.upper()} not configured")

    stripe = _client()
    customer_id = ensure_customer(workspace)
    base = settings.APP_BASE_URL.rstrip("/")
    sess = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base}/settings/billing?status=success",
        cancel_url=f"{base}/settings/billing?status=cancelled",
        metadata={"workspace_id": str(workspace.id), "plan": plan_id},
    )
    return sess["url"]


def handle_webhook(payload: bytes, signature: str) -> dict:
    """Verify signature and apply the subscription event to a workspace."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET not configured")
    stripe = _client()
    event = stripe.Webhook.construct_event(
        payload, signature, settings.STRIPE_WEBHOOK_SECRET
    )
    obj = event["data"]["object"]
    type_ = event["type"]

    customer_id = obj.get("customer")
    if customer_id:
        ws = Workspace.query.filter_by(stripe_customer_id=customer_id).first()
    else:
        ws = None

    if ws is None:
        return {"handled": False, "reason": "workspace not found"}

    if type_ == "checkout.session.completed":
        ws.stripe_subscription_id = obj.get("subscription")
        ws.subscription_status = "active"
        plan = (obj.get("metadata") or {}).get("plan")
        if plan in settings.PLAN_LIMITS:
            ws.plan = plan
    elif type_ in (
        "customer.subscription.updated", "customer.subscription.created"
    ):
        status = obj.get("status")
        ws.subscription_status = status
        if status not in ("active", "trialing"):
            # Downgrade on cancellation or past_due.
            ws.plan = "free"
    elif type_ == "customer.subscription.deleted":
        ws.subscription_status = "canceled"
        ws.plan = "free"

    db.session.commit()
    return {"handled": True, "workspace_id": ws.id, "plan": ws.plan}
