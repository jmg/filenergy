"""Regression tests for the second security review (October 2026).

1. SSRF: `/file/from_url/` fetched any http(s) URL (loopback, the Docker
   network, cloud metadata), followed redirects, and stored the answer as a
   downloadable file. Webhooks POSTed (blind) to the same places.
2. `POST /file/search/` needed no login and, with an empty name, listed
   every public file of every workspace with the hash that downloads it.
3. `/2fa` took unlimited TOTP guesses, and the pending login survived them.
4. Connector OAuth: `state` was the workspace id and the callback didn't
   check that the returning user belonged to it.
5. API keys read (and deleted) other members' chats.
"""
from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from filenergy.models import (
    ConnectorAccount, Conversation, File, User, WebhookDelivery,
    WebhookSubscription, WorkspaceMember,
)
from filenergy.services import outbound
from filenergy.services.outbound import _open as REAL_OPEN


def _make_user(db, email, password="password"):
    from filenergy.services import workspaces

    u = User(email=email, username=email)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    workspaces.ensure_default_for(u)
    return u


def _login(client, email, password="password"):
    return client.post("/user/login/", data={"email": email, "password": password})


# ---------- 1. outbound guard ---------------------------------------------


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.17.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00:ec2::254",
    "::ffff:127.0.0.1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::1", "not-an-ip",
])
def test_non_public_addresses_are_refused(address):
    assert outbound.is_public_ip(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert outbound.is_public_ip(address) is True


@pytest.mark.parametrize("url", [
    "ftp://example.com/", "file:///etc/passwd", "gopher://example.com/",
    "http://example.com:8080/", "https://example.com:22/",
    "http://user:pw@example.com/", "http:///nohost",
])
def test_bad_urls_are_refused_before_connecting(url):
    with pytest.raises(outbound.UnsafeURL):
        outbound.parse_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "http://10.0.0.5:80/admin",
])
def test_private_literals_are_refused(url):
    with pytest.raises(outbound.UnsafeURL):
        outbound.request("GET", url)


def test_a_name_resolving_to_any_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(outbound, "_getaddrinfo", lambda host, port: [
        (2, 1, 6, "", ("93.184.216.34", port)),
        (2, 1, 6, "", ("10.0.0.5", port)),
    ])
    with pytest.raises(outbound.UnsafeURL):
        outbound.request("GET", "http://rebind.example/")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/to-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path == "/to-final":
            self.send_response(301)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        body = b"x" * 5000 if self.path == "/big" else (
            f"host={self.headers['Host']} path={self.path}".encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_site(monkeypatch):
    """A real HTTP server on 127.0.0.1 that the guard treats as 'public'
    for the length of the test (only that address), reachable as
    public.test on its port. Everything else stays refused."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setattr(outbound, "_open", REAL_OPEN)
    monkeypatch.setattr(outbound, "ALLOWED_PORTS", {80, 443, port})
    monkeypatch.setattr(outbound, "is_public_ip", lambda a: a == "127.0.0.1")

    def fake_dns(host, p):
        ip = "127.0.0.1" if host == "public.test" else host
        return [(2, 1, 6, "", (ip, p))]

    monkeypatch.setattr(outbound, "_getaddrinfo", fake_dns)
    yield f"http://public.test:{port}"
    server.shutdown()
    server.server_close()


def test_real_transport_connects_to_the_checked_address(local_site):
    r = outbound.request("GET", f"{local_site}/page?q=1")
    assert r.status == 200
    # Connected to the pinned IP, but spoke to the host by name.
    assert r.body.decode() == f"host={urlparse(local_site).netloc} path=/page?q=1"


def test_redirects_are_rechecked_on_every_hop(local_site):
    with pytest.raises(outbound.UnsafeURL):
        outbound.request("GET", f"{local_site}/to-metadata", max_redirects=3)
    r = outbound.request("GET", f"{local_site}/to-final", max_redirects=3)
    assert r.status == 200 and r.body.endswith(b"path=/final")
    # Not following: the 3xx comes back as is.
    assert outbound.request("GET", f"{local_site}/to-final").status == 301


def test_body_is_capped(local_site):
    with pytest.raises(outbound.ResponseTooLarge):
        outbound.request("GET", f"{local_site}/big", max_bytes=1000)
    r = outbound.request("GET", f"{local_site}/big", max_bytes=1000, truncate=True)
    assert len(r.body) == 1000


def test_from_url_refuses_internal_addresses(auth_client, db, workspace, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))
    for url in ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                "http://localhost:8000/", "http://example.com:6379/"]:
        if url.startswith("http://localhost"):
            monkeypatch.setattr(outbound, "_getaddrinfo",
                                lambda h, p: [(2, 1, 6, "", ("127.0.0.1", p))])
        r = auth_client.post("/file/from_url/", data={"url": url})
        assert r.status_code == 400, url
    assert calls == []
    assert File.query.filter_by(workspace_id=workspace.id).count() == 0


def test_webhook_urls_to_internal_addresses_are_refused(auth_client, client, db, user, workspace, app):
    from filenergy.services import api_keys

    r = auth_client.post("/settings/webhooks", data={
        "url": "http://127.0.0.1/hook", "events": ["file.uploaded"],
    })
    assert r.status_code == 302
    with app.test_request_context():
        _, token = api_keys.mint(workspace, user, "full")
    r = client.post(
        "/api/v1/webhooks", json={"url": "http://169.254.169.254/", "events": ["file.uploaded"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert WebhookSubscription.query.count() == 0


def test_webhook_delivery_is_blocked_when_dns_turns_private(db, workspace, app, monkeypatch):
    from filenergy.services import webhooks

    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("FILENERGY_WEBHOOK_RETRIES", "3")
    monkeypatch.setenv("FILENERGY_WEBHOOK_BACKOFF_S", "0")
    with app.test_request_context():
        webhooks.create(workspace, "https://hooks.example/x", ["file.uploaded"])
        monkeypatch.setattr(outbound, "_getaddrinfo",
                            lambda h, p: [(2, 1, 6, "", ("10.0.0.7", p))])
        webhooks.dispatch(workspace.id, "file.uploaded", {})
    deliveries = WebhookDelivery.query.all()
    assert len(deliveries) == 1  # blocked is not transient: no retries
    assert deliveries[0].error.startswith("Blocked")
    assert calls == []


# ---------- 2. search ------------------------------------------------------


def test_search_requires_login_and_stays_in_the_workspace(client, db, user, workspace):
    other = _make_user(db, "other@x.com")
    from filenergy.services import workspaces
    other_ws = workspaces.ensure_default_for(other)
    db.session.add(File(user_id=other.id, workspace_id=other_ws.id,
                        name="their-public.txt", path="/x", url="h-pub", is_public=True))
    db.session.add(File(user_id=user.id, workspace_id=workspace.id,
                        name="mine.txt", path="/x", url="h-mine"))
    db.session.commit()

    r = client.post("/file/search/", data={"name": ""})
    assert r.status_code in (302, 401)
    assert b"their-public" not in r.data and b"h-pub" not in r.data

    _login(client, user.email)
    r = client.post("/file/search/", data={"name": ""})
    assert r.status_code == 200
    assert b"mine.txt" in r.data
    assert b"their-public" not in r.data


# ---------- 3. second factor ----------------------------------------------


def _enable_totp(user, app):
    import pyotp

    from filenergy.services import totp
    with app.test_request_context():
        totp.start_setup(user)
        assert totp.enable(user, pyotp.TOTP(user.totp_secret).now())
    return user.totp_secret


def _valid_code(secret):
    import pyotp
    return pyotp.TOTP(secret).now()


def _logged_in(client):
    return client.get("/file/list/").status_code == 200


def test_five_wrong_codes_kill_the_pending_login(client, user, app):
    secret = _enable_totp(user, app)
    _login(client, user.email)
    pending_cookie = client.get_cookie("session").value
    for _ in range(5):
        client.post("/user/2fa", data={"code": "000000"})
    r = client.post("/user/2fa", data={"code": _valid_code(secret)})
    assert "/user/login/" in r.headers["Location"]
    assert not _logged_in(client)

    # Replaying the cookie from before the failures doesn't reset the count.
    client.set_cookie("session", pending_cookie)
    r = client.post("/user/2fa", data={"code": _valid_code(secret)})
    assert "/user/login/" in r.headers["Location"]
    assert not _logged_in(client)


def test_wrong_codes_add_up_per_account(client, user, app):
    secret = _enable_totp(user, app)
    for _ in range(2):  # two pending logins, five misses each
        _login(client, user.email)
        for _ in range(5):
            client.post("/user/2fa", data={"code": "000000"})
    _login(client, user.email)
    r = client.post("/user/2fa", data={"code": _valid_code(secret)})
    assert "/user/login/" in r.headers["Location"]
    assert not _logged_in(client)


def test_a_few_typos_still_log_in(client, user, app):
    secret = _enable_totp(user, app)
    _login(client, user.email)
    for _ in range(3):
        client.post("/user/2fa", data={"code": "000000"})
    client.post("/user/2fa", data={"code": _valid_code(secret)})
    assert _logged_in(client)


# ---------- 4. connector OAuth state --------------------------------------


@pytest.fixture
def google_configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "y")
    from filenergy.services import connectors as conn_mod

    def fake_complete(*, code, workspace_id, redirect_uri):
        from filenergy import db
        a = ConnectorAccount(workspace_id=workspace_id, kind="google_drive",
                             account_label="attacker@x", access_token="t")
        db.session.add(a)
        db.session.commit()
        return a

    monkeypatch.setattr(conn_mod._CONNECTORS["google_drive"], "complete_oauth", fake_complete)


def _state_from(r):
    return parse_qs(urlparse(r.headers["Location"]).query)["state"][0]


def test_state_is_random_not_the_workspace_id(auth_client, workspace, google_configured):
    state = _state_from(auth_client.get("/connectors/google_drive/connect"))
    assert state != str(workspace.id) and len(state) >= 32


def test_callback_with_a_workspace_id_as_state_is_refused(client, db, user, workspace, google_configured):
    attacker = _make_user(db, "attacker@x.com")
    _login(client, attacker.email)
    client.get("/connectors/google_drive/connect")  # attacker's own flow
    r = client.get(f"/connectors/google_drive/callback?code=C&state={workspace.id}")
    assert r.status_code == 302
    assert ConnectorAccount.query.filter_by(workspace_id=workspace.id).count() == 0


def test_state_is_bound_to_the_session_and_single_use(auth_client, db, workspace, google_configured):
    state = _state_from(auth_client.get("/connectors/google_drive/connect"))
    cookie = auth_client.get_cookie("session").value

    # A session that didn't start this flow (e.g. the victim of a forged
    # callback link) can't complete it.
    with auth_client.session_transaction() as s:
        s.pop("connector_oauth_states", None)
    auth_client.get(f"/connectors/google_drive/callback?code=C&state={state}")
    assert ConnectorAccount.query.count() == 0

    auth_client.set_cookie("session", cookie)

    auth_client.get(f"/connectors/google_drive/callback?code=C&state={state}")
    assert ConnectorAccount.query.filter_by(workspace_id=workspace.id).count() == 1
    auth_client.get(f"/connectors/google_drive/callback?code=C&state={state}")
    assert ConnectorAccount.query.count() == 1


def test_callback_rechecks_the_role(client, db, user, workspace, google_configured):
    admin = _make_user(db, "admin@x.com")
    member = WorkspaceMember(workspace_id=workspace.id, user_id=admin.id, role="admin")
    db.session.add(member)
    db.session.commit()
    _login(client, admin.email)
    client.post(f"/w/switch/{workspace.id}")
    state = _state_from(client.get("/connectors/google_drive/connect"))

    member.role = "member"  # demoted while away at the provider
    db.session.commit()
    r = client.get(f"/connectors/google_drive/callback?code=C&state={state}")
    assert r.status_code == 403
    assert ConnectorAccount.query.count() == 0


# ---------- 5. API: chats are per user ------------------------------------


def test_api_key_only_sees_its_users_chats(client, db, user, workspace, app):
    from filenergy.services import api_keys

    mate = _make_user(db, "mate@x.com")
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=mate.id, role="member"))
    theirs = Conversation(user_id=user.id, workspace_id=workspace.id, title="owner's secret")
    mine = Conversation(user_id=mate.id, workspace_id=workspace.id, title="mine")
    db.session.add_all([theirs, mine])
    db.session.commit()
    with app.test_request_context():
        _, token = api_keys.mint(workspace, mate, "mate")
    h = {"Authorization": f"Bearer {token}"}

    listed = client.get("/api/v1/conversations", headers=h).get_json()["conversations"]
    assert [c["id"] for c in listed] == [mine.id]
    assert client.get(f"/api/v1/conversations/{theirs.id}", headers=h).status_code == 404
    assert client.delete(f"/api/v1/conversations/{theirs.id}", headers=h).status_code in (403, 404)
    assert db.session.get(Conversation, theirs.id) is not None
    assert client.get(f"/api/v1/conversations/{mine.id}", headers=h).status_code == 200
