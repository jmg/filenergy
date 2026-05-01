"""Tier-4 feature tests:

- Real WebAuthn ceremony plumbing (begin/complete via py_webauthn).
- Job queue retries (sync + thread + RQ paths).
- Webhook delivery retries on 5xx / network errors.
- Digest async fan-out via jobs.enqueue.
- Tailwind compiled-bundle switch.
"""
from __future__ import annotations

import pytest


# ---------- WebAuthn full ceremony ---------------------------------------


def test_webauthn_begin_registration_persists_challenge_in_session(client, user):
    """begin_registration stashes challenge + user_handle so the
    follow-up complete_registration can verify against them."""
    from filenergy.services import webauthn

    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    with client.session_transaction() as s:
        # No challenge before begin.
        assert webauthn._REG_CHALLENGE_KEY not in s
    r = client.post("/settings/security/webauthn/begin")
    assert r.status_code == 200
    with client.session_transaction() as s:
        assert webauthn._REG_CHALLENGE_KEY in s


def test_webauthn_begin_authentication_lists_credentials(app, db, user):
    from filenergy.services import webauthn

    cred = webauthn.register_stub(user, label="Key")
    with app.test_request_context():
        options = webauthn.begin_authentication(user)
        assert options["challenge"]
        assert options["allowCredentials"]
        # The stub credential id appears in allowCredentials.
        ids = [c["id"] for c in options["allowCredentials"]]
        assert cred.credential_id in ids


def test_webauthn_complete_registration_rejects_without_challenge(app, user):
    """Without a challenge stashed in session, completion can't proceed."""
    from filenergy.services import webauthn

    with app.test_request_context():
        with pytest.raises(webauthn.WebAuthnError):
            webauthn.complete_registration(user, response={})


def test_webauthn_endpoints_require_login(client):
    r = client.post("/settings/security/webauthn/begin")
    # Flask-Login redirects unauthenticated requests to /user/login/.
    assert r.status_code in (302, 401)


def test_two_factor_webauthn_endpoints_need_pending_login(client):
    r = client.post("/user/2fa/webauthn/begin")
    assert r.status_code == 400
    r = client.post("/user/2fa/webauthn/complete", json={})
    assert r.status_code == 400


def test_two_factor_webauthn_complete_rejects_when_assertion_fails(
    monkeypatch, client, db, user,
):
    """Bad assertion → 400, no login."""
    from filenergy.services import webauthn

    webauthn.register_stub(user, label="Key")
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    monkeypatch.setattr(
        "filenergy.services.webauthn.complete_authentication",
        lambda user, *, response: False,
    )
    r = client.post("/user/2fa/webauthn/complete", json={"response": {}})
    assert r.status_code == 400
    assert r.get_json()["error"] == "verification failed"


def test_two_factor_webauthn_complete_logs_in_on_success(
    monkeypatch, client, db, user,
):
    """Successful assertion finalises the login."""
    from filenergy.services import webauthn

    webauthn.register_stub(user, label="Key")
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    monkeypatch.setattr(
        "filenergy.services.webauthn.complete_authentication",
        lambda user, *, response: True,
    )
    r = client.post("/user/2fa/webauthn/complete", json={"response": {}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["next"]
    # Pending 2FA cleared.
    with client.session_transaction() as s:
        from filenergy.views.user import PENDING_2FA_KEY
        assert PENDING_2FA_KEY not in s


def test_two_factor_webauthn_complete_handles_library_error(
    monkeypatch, client, db, user,
):
    """If py_webauthn raises, return 400 with the message."""
    from filenergy.services import webauthn

    webauthn.register_stub(user, label="Key")
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )

    def boom(*a, **k):
        raise webauthn.WebAuthnError("library says no")

    monkeypatch.setattr(
        "filenergy.services.webauthn.complete_authentication", boom
    )
    r = client.post("/user/2fa/webauthn/complete", json={"response": {}})
    assert r.status_code == 400
    assert "library says no" in r.get_json()["error"]


def test_two_factor_webauthn_begin_propagates_library_error(
    monkeypatch, client, db, user,
):
    """If begin_authentication raises, /2fa/webauthn/begin returns 400."""
    from filenergy.services import webauthn
    from filenergy.views.user import PENDING_2FA_KEY

    # Force the user into the pending-2FA state.
    webauthn.register_stub(user, label="Key")
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    with client.session_transaction() as s:
        assert PENDING_2FA_KEY in s

    def boom(_user):
        raise webauthn.WebAuthnError("nope")
    monkeypatch.setattr(
        "filenergy.services.webauthn.begin_authentication", boom,
    )
    r = client.post("/user/2fa/webauthn/begin")
    assert r.status_code == 400
    assert "nope" in r.get_json()["error"]


def test_settings_webauthn_complete_success(monkeypatch, client, db, user):
    """The settings complete endpoint records the credential and returns id."""
    from filenergy.models import WebAuthnCredential
    from filenergy.services import webauthn

    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )

    def fake_complete(user, *, response, label):
        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id="fake-id",
            public_key="fake-pk",
            sign_count=0,
            label=label,
        )
        from filenergy import db as _db
        _db.session.add(cred)
        _db.session.commit()
        return cred

    monkeypatch.setattr(
        "filenergy.services.webauthn.complete_registration", fake_complete
    )
    r = client.post(
        "/settings/security/webauthn/complete",
        json={"label": "MyKey", "response": {}},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["label"] == "MyKey"
    assert body["id"]


def test_settings_webauthn_complete_returns_400_on_error(
    monkeypatch, client, user,
):
    """Library errors surface as 400 with the message."""
    from filenergy.services import webauthn

    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )

    def boom(*a, **k):
        raise webauthn.WebAuthnError("bad challenge")
    monkeypatch.setattr(
        "filenergy.services.webauthn.complete_registration", boom
    )
    r = client.post(
        "/settings/security/webauthn/complete",
        json={"response": {}},
    )
    assert r.status_code == 400
    assert "bad challenge" in r.get_json()["error"]


def test_settings_webauthn_begin_returns_400_on_error(monkeypatch, client, user):
    from filenergy.services import webauthn

    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )

    def boom(_user):
        raise webauthn.WebAuthnError("disabled")
    monkeypatch.setattr(
        "filenergy.services.webauthn.begin_registration", boom
    )
    r = client.post("/settings/security/webauthn/begin")
    assert r.status_code == 400
    assert "disabled" in r.get_json()["error"]


def test_two_factor_webauthn_begin_after_password(client, db, user):
    """Once the password step is past, begin emits FIDO2 options."""
    from filenergy.services import webauthn

    webauthn.register_stub(user, label="Key")
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    r = client.post("/user/2fa/webauthn/begin")
    assert r.status_code == 200
    body = r.get_json()
    assert "challenge" in body
    assert "allowCredentials" in body


# ---------- Job queue retries -------------------------------------------


def test_thread_backend_retries_on_transient_failure(app, monkeypatch):
    """Thread mode also honours retries=N (sleeps suppressed under TESTING)."""
    import sys, types
    from filenergy.services import jobs

    app.config["TESTING"] = False
    attempts = []

    def flap():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("transient")

    sys.modules["__main__flap2"] = types.ModuleType("__main__flap2")
    sys.modules["__main__flap2"].flap = flap

    class _SyncThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("threading.Thread", _SyncThread)
    monkeypatch.delenv("FILENERGY_SYNC_JOBS", raising=False)
    monkeypatch.delenv("FILENERGY_JOBS_BACKEND", raising=False)
    try:
        # Re-enable TESTING-via-config sleep skip.
        app.config["TESTING"] = True
        jobs.enqueue("__main__flap2.flap", retries=3, retry_backoff_seconds=0)
    finally:
        app.config["TESTING"] = True
    assert len(attempts) == 2


def test_rq_backend_uses_retry_policy_when_available(app, monkeypatch):
    """If `rq.Retry` is importable, retries land in queue.enqueue(retry=...)."""
    import sys, types
    from filenergy.services import jobs

    app.config["TESTING"] = False
    monkeypatch.setenv("FILENERGY_JOBS_BACKEND", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.delenv("FILENERGY_SYNC_JOBS", raising=False)

    fake_redis = types.ModuleType("redis")

    class _Conn:
        @staticmethod
        def from_url(url):
            return object()

    fake_redis.Redis = _Conn

    fake_rq = types.ModuleType("rq")
    captured: list = []

    class _Queue:
        def __init__(self, name, connection):
            pass

        def enqueue(self, fn, *args, **kwargs):
            captured.append(("enqueue", args, kwargs))

    class _Retry:
        def __init__(self, max, interval):
            self.max, self.interval = max, interval

    fake_rq.Queue = _Queue
    fake_rq.Retry = _Retry
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "rq", fake_rq)

    try:
        jobs.enqueue(
            "tests.test_tier4_features.test_rq_backend_uses_retry_policy_when_available",
            retries=3, retry_backoff_seconds=2,
        )
    finally:
        app.config["TESTING"] = True

    assert captured
    _, _, kwargs = captured[0]
    retry = kwargs.get("retry")
    assert retry is not None
    assert retry.max == 3
    # Exponential schedule.
    assert retry.interval == [2, 4, 8]


# ---------- Webhook retries ---------------------------------------------


def test_webhook_5xx_response_retries(app, db, workspace, monkeypatch):
    """Setting FILENERGY_WEBHOOK_RETRIES > 0 re-fires deliveries on 5xx."""
    from filenergy.models import WebhookDelivery
    from filenergy.services import webhooks

    monkeypatch.setenv("FILENERGY_WEBHOOK_RETRIES", "3")
    monkeypatch.setenv("FILENERGY_WEBHOOK_BACKOFF_S", "0")

    class _FakeResponse:
        status = 500
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): return b"down"

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResponse())

    with app.test_request_context():
        webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.dispatch(workspace.id, "file.uploaded", {})

    deliveries = WebhookDelivery.query.all()
    # 1 initial + 3 retries.
    assert len(deliveries) == 4


def test_webhook_4xx_response_does_not_retry(app, db, workspace, monkeypatch):
    """4xx is a client-side bug in the consumer's handler — retrying just
    hammers them. Only 5xx + network errors cause retry."""
    from filenergy.models import WebhookDelivery
    from filenergy.services import webhooks

    monkeypatch.setenv("FILENERGY_WEBHOOK_RETRIES", "3")
    monkeypatch.setenv("FILENERGY_WEBHOOK_BACKOFF_S", "0")

    class _FakeResponse:
        status = 422
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): return b"bad payload"

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResponse())

    with app.test_request_context():
        webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.dispatch(workspace.id, "file.uploaded", {})

    deliveries = WebhookDelivery.query.all()
    assert len(deliveries) == 1


def test_webhook_network_error_retries(app, db, workspace, monkeypatch):
    """Network errors are transient by definition."""
    from filenergy.models import WebhookDelivery
    from filenergy.services import webhooks

    monkeypatch.setenv("FILENERGY_WEBHOOK_RETRIES", "2")
    monkeypatch.setenv("FILENERGY_WEBHOOK_BACKOFF_S", "0")

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with app.test_request_context():
        webhooks.create(workspace, "https://x/", ["file.uploaded"])
        webhooks.dispatch(workspace.id, "file.uploaded", {})

    deliveries = WebhookDelivery.query.all()
    assert len(deliveries) == 3  # 1 + 2 retries


# ---------- Digest async fan-out ----------------------------------------


def test_send_pending_async_enqueues_per_user(
    monkeypatch, db, user, workspace, uploaded_file,
):
    """async_dispatch=True hands each eligible user to the jobs queue."""
    from filenergy.services import digest, email as email_service

    monkeypatch.setattr(email_service, "send", lambda to, subject, body: True)
    n = digest.send_pending(async_dispatch=True)
    assert n == 1
    db.session.refresh(user)
    # send_for_user updates last_digest_sent_at on success.
    assert user.last_digest_sent_at is not None


def test_send_for_user_skips_when_already_sent_recently(
    monkeypatch, db, user, workspace, uploaded_file,
):
    from filenergy.models import utcnow
    from filenergy.services import digest, email as email_service

    user.last_digest_sent_at = utcnow()
    db.session.commit()

    monkeypatch.setattr(email_service, "send", lambda **k: True)
    assert digest.send_for_user(user.id) is False


def test_send_for_user_raises_for_retry_on_smtp_failure(
    monkeypatch, db, user, workspace, uploaded_file,
):
    """Real SMTP outages should bubble as DigestSendFailed so the jobs
    queue retries instead of marking the digest as sent."""
    from filenergy.services import digest, email as email_service

    monkeypatch.setattr(email_service, "send", lambda to, subject, body: False)
    with pytest.raises(digest.DigestSendFailed):
        digest.send_for_user(user.id)
    db.session.refresh(user)
    assert user.last_digest_sent_at is None


# ---------- Tailwind bundle switch --------------------------------------


def test_base_uses_compiled_bundle_when_present(client, app, monkeypatch, tmp_path):
    """When static/css/app.css exists, base.html links to it instead of
    pulling the Play CDN."""
    import os, shutil
    from pathlib import Path

    bundle_path = Path(app.static_folder) / "css" / "app.css"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text("/* compiled */", encoding="utf-8")
    try:
        r = client.get("/")
        assert r.status_code == 200
        assert b"/static/css/app.css" in r.data
        assert b"cdn.tailwindcss.com" not in r.data
    finally:
        os.remove(bundle_path)


def test_base_falls_back_to_cdn_without_bundle(client, app):
    """Default dev path: no bundle file → CDN + inline component classes."""
    import os
    from pathlib import Path

    bundle_path = Path(app.static_folder) / "css" / "app.css"
    if bundle_path.exists():
        os.remove(bundle_path)
    r = client.get("/")
    assert r.status_code == 200
    assert b"cdn.tailwindcss.com" in r.data
