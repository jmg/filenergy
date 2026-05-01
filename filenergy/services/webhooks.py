"""Outbound webhooks.

Customers register a URL + secret + interested events. When an event of
interest fires, we deliver a JSON POST with an HMAC-SHA256 signature in
`X-Filenergy-Signature: sha256=<hex>`. Delivery runs in a daemon thread
so request latency isn't tied to the consumer's response time.

Subscriptions and deliveries are per-workspace; one subscription can opt
into many event types via the JSON-encoded `events_json` column.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from filenergy import app, db
from filenergy.models import (
    WebhookDelivery,
    WebhookSubscription,
    utcnow,
)
from filenergy.services import jobs

log = logging.getLogger(__name__)


HEADER_SIGNATURE = "X-Filenergy-Signature"
HEADER_EVENT = "X-Filenergy-Event"
HEADER_DELIVERY = "X-Filenergy-Delivery"
TIMEOUT_SECONDS = 10


def sign(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def create(workspace, url: str, events: list[str]) -> tuple[WebhookSubscription, str]:
    """Create a subscription. Returns (row, plaintext_secret).

    Plaintext secret is shown once at creation; we store it in the row so
    we can sign deliveries, but never expose it again via the UI.
    """
    secret = secrets.token_urlsafe(32)
    sub = WebhookSubscription(
        workspace_id=workspace.id,
        url=url.strip(),
        secret=secret,
        events_json=json.dumps(sorted(set(events))),
        enabled=True,
    )
    db.session.add(sub)
    db.session.commit()
    return sub, secret


def list_for_workspace(workspace) -> list[WebhookSubscription]:
    return (
        WebhookSubscription.query.filter_by(workspace_id=workspace.id)
        .order_by(WebhookSubscription.id.desc())
        .all()
    )


def get(workspace, subscription_id: int) -> WebhookSubscription | None:
    return WebhookSubscription.query.filter_by(
        id=subscription_id, workspace_id=workspace.id
    ).first()


def delete(sub: WebhookSubscription) -> None:
    db.session.delete(sub)
    db.session.commit()


def set_enabled(sub: WebhookSubscription, enabled: bool) -> None:
    sub.enabled = enabled
    db.session.commit()


def deliveries_for(sub: WebhookSubscription, *, limit: int = 25):
    return (
        WebhookDelivery.query.filter_by(subscription_id=sub.id)
        .order_by(WebhookDelivery.id.desc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch(workspace_id: int, event_type: str, payload: dict[str, Any]) -> int:
    """Schedule webhook deliveries for an event. Returns count scheduled."""
    subs = (
        WebhookSubscription.query.filter_by(
            workspace_id=workspace_id, enabled=True
        )
        .all()
    )
    interested = [s for s in subs if event_type in s.event_types]
    if not interested:
        return 0

    body = json.dumps({"event": event_type, "data": payload, "workspace_id": workspace_id})
    # 5 attempts with 2s exponential backoff (2/4/8/16s) covers a
    # consumer briefly under load without DOSing them. Tests pin
    # FILENERGY_WEBHOOK_RETRIES=0 so each delivery runs exactly once.
    import os
    retries = int(os.environ.get("FILENERGY_WEBHOOK_RETRIES", "4"))
    backoff = int(os.environ.get("FILENERGY_WEBHOOK_BACKOFF_S", "2"))
    for sub in interested:
        jobs.enqueue(
            "filenergy.services.webhooks._deliver_one",
            sub.id, event_type, body,
            retries=retries,
            retry_backoff_seconds=backoff,
        )
    return len(interested)


class WebhookDeliveryFailed(Exception):
    """Raised after `_deliver_one` records a transient failure so the
    jobs-queue retry policy can re-run the delivery. 4xx responses are
    intentionally NOT retried (client error in the consumer's payload
    handler — retrying just hammers them)."""


def _deliver_one(subscription_id: int, event_type: str, body: str) -> WebhookDelivery:
    sub = WebhookSubscription.query.get(subscription_id)
    if sub is None:
        return None  # type: ignore[return-value]

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type=event_type,
        payload_json=body,
    )
    db.session.add(delivery)
    db.session.commit()

    body_bytes = body.encode("utf-8")
    signature = sign(sub.secret, body_bytes)
    headers = {
        "Content-Type": "application/json",
        HEADER_SIGNATURE: signature,
        HEADER_EVENT: event_type,
        HEADER_DELIVERY: str(delivery.id),
        "User-Agent": "Filenergy-Webhook/1",
    }

    transient_failure = False
    try:
        req = urllib_request.Request(
            sub.url, data=body_bytes, headers=headers, method="POST"
        )
        with urllib_request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.status
            response_body = resp.read(2048).decode("utf-8", errors="replace")
        delivery.response_status = status
        delivery.response_body = response_body
        delivery.delivered_at = utcnow()
        sub.last_status = status
        sub.last_attempt_at = utcnow()
        if status >= 500:
            sub.failure_count = (sub.failure_count or 0) + 1
            delivery.error = f"HTTP {status}"
            transient_failure = True
        elif status >= 400:
            sub.failure_count = (sub.failure_count or 0) + 1
            delivery.error = f"HTTP {status}"
        else:
            sub.failure_count = 0
    except urllib_error.HTTPError as exc:
        delivery.response_status = exc.code
        delivery.error = f"HTTPError {exc.code}"
        sub.last_status = exc.code
        sub.last_attempt_at = utcnow()
        sub.failure_count = (sub.failure_count or 0) + 1
        transient_failure = exc.code >= 500
    except Exception as exc:
        delivery.error = str(exc)[:500]
        sub.last_attempt_at = utcnow()
        sub.failure_count = (sub.failure_count or 0) + 1
        transient_failure = True

    db.session.commit()
    if transient_failure:
        raise WebhookDeliveryFailed(delivery.error or "transient failure")
    return delivery
