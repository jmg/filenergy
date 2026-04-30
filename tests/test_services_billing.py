import io
import json
import sys
import types

import pytest

from filenergy import settings
from filenergy.models import Event, File
from filenergy.services import billing


def test_plan_for_default_is_free(workspace):
    assert billing.plan_for(workspace)["label"] == "Free"


def test_plan_for_falls_back_to_free_for_unknown(workspace):
    workspace.plan = "garbage-plan"
    assert billing.plan_for(workspace)["label"] == "Free"


def test_storage_used_zero_when_no_files(db, workspace):
    assert billing.storage_used_bytes(workspace) == 0


def test_storage_used_sums_size_bytes(db, user, workspace):
    db.session.add_all([
        File(user_id=user.id, workspace_id=workspace.id, name="a", path="/a", url="u1", size_bytes=1000),
        File(user_id=user.id, workspace_id=workspace.id, name="b", path="/b", url="u2", size_bytes=500),
    ])
    db.session.commit()
    assert billing.storage_used_bytes(workspace) == 1500


def test_files_count(db, user, workspace):
    db.session.add(File(
        user_id=user.id, workspace_id=workspace.id,
        name="x", path="/x", url="u1",
    ))
    db.session.commit()
    assert billing.files_count(workspace) == 1


def test_asks_this_month_counts_only_workspace(db, user, workspace, app):
    from filenergy.services import events
    with app.test_request_context():
        events.log_event(events.ASK_QUESTION, user=user, workspace_id=workspace.id)
        events.log_event(events.ASK_QUESTION, user=user, workspace_id=workspace.id)
        events.log_event(events.ASK_QUESTION, user=user, workspace_id=999)
    assert billing.asks_this_month(workspace) == 2


def test_ensure_can_upload_under_limits(db, workspace):
    billing.ensure_can_upload(workspace)


def test_ensure_can_upload_files_quota(db, user, workspace, monkeypatch):
    monkeypatch.setitem(
        settings.PLAN_LIMITS["free"], "files_max", 1
    )
    db.session.add(File(
        user_id=user.id, workspace_id=workspace.id,
        name="a", path="/a", url="u1",
    ))
    db.session.commit()
    with pytest.raises(billing.QuotaExceeded) as exc:
        billing.ensure_can_upload(workspace)
    assert exc.value.kind == "files"


def test_ensure_can_upload_storage_quota(db, user, workspace, monkeypatch):
    monkeypatch.setitem(
        settings.PLAN_LIMITS["free"], "storage_bytes_max", 100
    )
    db.session.add(File(
        user_id=user.id, workspace_id=workspace.id,
        name="big", path="/x", url="u1", size_bytes=200,
    ))
    db.session.commit()
    with pytest.raises(billing.QuotaExceeded) as exc:
        billing.ensure_can_upload(workspace)
    assert exc.value.kind == "storage_bytes"


def test_ensure_can_ask_quota(db, user, workspace, app, monkeypatch):
    from filenergy.services import events

    monkeypatch.setitem(settings.PLAN_LIMITS["free"], "asks_per_month", 0)
    with pytest.raises(billing.QuotaExceeded):
        billing.ensure_can_ask(workspace)


def test_usage_summary_shape(db, workspace):
    s = billing.usage_summary(workspace)
    assert set(s.keys()) >= {"plan", "label", "files", "storage_bytes", "asks_this_month"}


def test_is_configured_without_key(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    assert billing.is_configured() is False


def test_create_checkout_session_unknown_plan(monkeypatch, workspace):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    with pytest.raises(billing.BillingError):
        billing.create_checkout_session(workspace, "nonsense")


def test_create_checkout_session_missing_price(monkeypatch, workspace):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(settings, "STRIPE_PRICE_PRO", "")
    fake_stripe = _install_fake_stripe(monkeypatch)
    with pytest.raises(billing.BillingError):
        billing.create_checkout_session(workspace, "pro")


def test_create_checkout_session_returns_url(monkeypatch, workspace):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(settings, "STRIPE_PRICE_PRO", "price_x")
    fake_stripe = _install_fake_stripe(monkeypatch)
    url = billing.create_checkout_session(workspace, "pro")
    assert url == "https://stripe.test/checkout/sess_1"
    assert workspace.stripe_customer_id == "cus_1"


def test_handle_webhook_checkout_completed(db, user, workspace, monkeypatch):
    workspace.stripe_customer_id = "cus_1"
    from filenergy import db as real_db
    real_db.session.commit()

    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    fake_stripe = _install_fake_stripe(
        monkeypatch,
        webhook_event={
            "type": "checkout.session.completed",
            "data": {"object": {
                "customer": "cus_1",
                "subscription": "sub_42",
                "metadata": {"plan": "pro"},
            }},
        },
    )
    result = billing.handle_webhook(b"raw", "sig")
    assert result["handled"] is True
    assert workspace.plan == "pro"
    assert workspace.stripe_subscription_id == "sub_42"


def test_handle_webhook_subscription_canceled(db, workspace, monkeypatch):
    workspace.stripe_customer_id = "cus_1"
    workspace.plan = "pro"
    from filenergy import db as real_db
    real_db.session.commit()

    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    _install_fake_stripe(
        monkeypatch,
        webhook_event={
            "type": "customer.subscription.deleted",
            "data": {"object": {"customer": "cus_1"}},
        },
    )
    billing.handle_webhook(b"raw", "sig")
    assert workspace.plan == "free"


def test_handle_webhook_subscription_updated_inactive_downgrades(db, workspace, monkeypatch):
    workspace.stripe_customer_id = "cus_1"
    workspace.plan = "pro"
    from filenergy import db as real_db
    real_db.session.commit()

    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    _install_fake_stripe(
        monkeypatch,
        webhook_event={
            "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_1", "status": "past_due"}},
        },
    )
    billing.handle_webhook(b"raw", "sig")
    assert workspace.plan == "free"


def test_handle_webhook_unknown_customer_returns_handled_false(db, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    _install_fake_stripe(
        monkeypatch,
        webhook_event={
            "type": "checkout.session.completed",
            "data": {"object": {"customer": "cus_unknown"}},
        },
    )
    out = billing.handle_webhook(b"raw", "sig")
    assert out["handled"] is False


def test_handle_webhook_without_secret(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    with pytest.raises(billing.BillingError):
        billing.handle_webhook(b"raw", "sig")


def test_client_without_key(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    with pytest.raises(billing.BillingError):
        billing._client()


def test_client_without_stripe_package(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_x")
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "stripe":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    import builtins
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(billing.BillingError):
        billing._client()


# ---- helpers ----

def _install_fake_stripe(monkeypatch, *, webhook_event=None):
    """Install a fake `stripe` module reachable via `import stripe`."""
    fake = types.ModuleType("stripe")
    fake.api_key = None

    class _Customer:
        @staticmethod
        def create(**kwargs):
            return {"id": "cus_1"}

    class _CheckoutSession:
        @staticmethod
        def create(**kwargs):
            return {"url": "https://stripe.test/checkout/sess_1"}

    class _Webhook:
        @staticmethod
        def construct_event(payload, sig, secret):
            return webhook_event or {}

    fake.Customer = _Customer
    fake.checkout = types.SimpleNamespace(Session=_CheckoutSession)
    fake.Webhook = _Webhook
    monkeypatch.setitem(sys.modules, "stripe", fake)
    return fake
