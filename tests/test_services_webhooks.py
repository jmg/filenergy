"""Tests for outbound webhook subscriptions and delivery."""
import hashlib
import hmac
import json

import pytest

from filenergy.models import WebhookDelivery, WebhookSubscription
from filenergy.services import webhooks


def test_sign_is_hmac_sha256():
    sig = webhooks.sign("topsecret", b"hello")
    expected = "sha256=" + hmac.new(b"topsecret", b"hello", hashlib.sha256).hexdigest()
    assert sig == expected


def test_create_returns_plaintext_secret(db, workspace, app):
    with app.test_request_context():
        sub, secret = webhooks.create(
            workspace, "https://example.com/hook",
            ["file.uploaded", "ask.answered"],
        )
    assert secret  # plaintext returned
    assert sub.secret == secret
    assert sub.event_types == ["ask.answered", "file.uploaded"]
    assert sub.enabled is True


def test_list_for_workspace_isolates(db, user, workspace, app):
    from filenergy.services import workspaces as ws_service

    other = _make_user(db, "x@x")
    other_ws = ws_service.ensure_default_for(other)
    with app.test_request_context():
        webhooks.create(workspace, "https://a/", ["file.uploaded"])
        webhooks.create(other_ws, "https://b/", ["file.uploaded"])
    listed = webhooks.list_for_workspace(workspace)
    assert len(listed) == 1


def test_get_returns_only_workspace_subs(db, workspace, app):
    from filenergy.services import workspaces as ws_service

    other = _make_user(db, "x@x")
    other_ws = ws_service.ensure_default_for(other)
    with app.test_request_context():
        sub, _ = webhooks.create(other_ws, "https://x/", ["file.uploaded"])
    assert webhooks.get(workspace, sub.id) is None


def test_set_enabled_toggles(db, workspace, app):
    with app.test_request_context():
        sub, _ = webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.set_enabled(sub, False)
    assert sub.enabled is False


def test_delete_removes(db, workspace, app):
    with app.test_request_context():
        sub, _ = webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.delete(sub)
    assert WebhookSubscription.query.count() == 0


def test_event_types_returns_empty_for_invalid_json(db, workspace):
    sub = WebhookSubscription(
        workspace_id=workspace.id, url="x", secret="s",
        events_json="not json",
    )
    assert sub.event_types == []


def test_dispatch_skips_when_no_subscribers(db, workspace, app):
    with app.test_request_context():
        sent = webhooks.dispatch(workspace.id, "file.uploaded", {"k": "v"})
    assert sent == 0


def test_dispatch_skips_when_event_not_subscribed(db, workspace, app):
    with app.test_request_context():
        webhooks.create(workspace, "https://x/", ["ask.answered"])
        sent = webhooks.dispatch(workspace.id, "file.uploaded", {})
    assert sent == 0


def test_dispatch_delivers_under_testing(db, workspace, app, monkeypatch):
    """In TESTING mode delivery runs synchronously."""
    captured = {}

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return b"ok"

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = req.data
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with app.test_request_context():
        sub, secret = webhooks.create(
            workspace, "https://hook.test/x", ["file.uploaded"]
        )
        sent = webhooks.dispatch(workspace.id, "file.uploaded", {"file_id": 7})

    assert sent == 1
    expected_sig = webhooks.sign(secret, captured["body"])
    # Header keys are normalized; check case-insensitively.
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["x-filenergy-signature"] == expected_sig
    assert headers_lower["x-filenergy-event"] == "file.uploaded"
    body = json.loads(captured["body"])
    assert body["event"] == "file.uploaded"
    assert body["data"] == {"file_id": 7}

    delivery = WebhookDelivery.query.first()
    assert delivery.response_status == 200
    assert delivery.delivered_at is not None
    db.session.refresh(sub)
    assert sub.last_status == 200
    assert sub.failure_count == 0


def test_dispatch_records_http_failure(db, workspace, app, monkeypatch):
    class _FakeResponse:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return b"server died"

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse(),
    )

    with app.test_request_context():
        sub, _ = webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.dispatch(workspace.id, "file.uploaded", {})

    delivery = WebhookDelivery.query.first()
    assert delivery.response_status == 500
    assert delivery.error == "HTTP 500"
    db.session.refresh(sub)
    assert sub.failure_count == 1


def test_dispatch_handles_network_error(db, workspace, app, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with app.test_request_context():
        webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.dispatch(workspace.id, "file.uploaded", {})

    delivery = WebhookDelivery.query.first()
    assert delivery.error and "connection refused" in delivery.error
    assert delivery.delivered_at is None


def test_dispatch_handles_http_error(db, workspace, app, monkeypatch):
    from urllib.error import HTTPError

    def boom(req, timeout=None):
        raise HTTPError(req.full_url, 502, "bad gateway", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with app.test_request_context():
        sub, _ = webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.dispatch(workspace.id, "file.uploaded", {})

    delivery = WebhookDelivery.query.first()
    assert delivery.response_status == 502
    db.session.refresh(sub)
    assert sub.failure_count == 1


def test_dispatch_skips_when_subscription_missing(db, workspace, app):
    """Race: subscription was deleted between dispatch and delivery."""
    with app.test_request_context():
        result = webhooks._deliver_one(9999, "file.uploaded", "{}")
    assert result is None


def test_deliveries_for_returns_recent(db, workspace, app, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: type(
            "R", (), {
                "__enter__": lambda self: self,
                "__exit__": lambda *a: False,
                "status": 200,
                "read": lambda self, n=None: b"",
            },
        )(),
    )
    with app.test_request_context():
        sub, _ = webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.dispatch(workspace.id, "file.uploaded", {})
    assert len(webhooks.deliveries_for(sub)) == 1


def _make_user(db, email):
    from filenergy.models import User

    u = User(email=email, username=email)
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u
