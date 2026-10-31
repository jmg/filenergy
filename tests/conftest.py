"""Shared pytest fixtures.

Each test gets a fresh in-memory SQLite via app.config override; external
services (Voyage, Anthropic) are stubbed at the module level so tests never
hit the network.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make the project importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force a clean settings state before importing the app. Each test gets a
# private DB and upload dir so they don't leak state between modules.
_TMP = Path(tempfile.mkdtemp(prefix="filenergy-tests-"))
os.environ["FILENERGY_DB_PATH"] = str(_TMP / "test.db")
os.environ["FILENERGY_DB_URI"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["FILENERGY_UPLOAD_DIR"] = str(_TMP / "files")
os.environ["FILENERGY_SECRET_KEY"] = "test-secret-key"
# Tests run jobs synchronously; default retries=0 so webhook tests still
# observe a single delivery attempt. Specific retry tests bump this.
os.environ.setdefault("FILENERGY_WEBHOOK_RETRIES", "0")

import pytest  # noqa: E402

from filenergy import app as flask_app, db as _db, settings  # noqa: E402
from filenergy.services import chat as chat_module  # noqa: E402
from filenergy.services import embeddings as emb_module  # noqa: E402


# ---------- external service stubs ----------

class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, text="stub answer"):
        self.content = [_FakeBlock(text)]


class _FakeStream:
    def __init__(self, tokens=("stub ", "answer"), final_text="stub answer"):
        self._tokens = list(tokens)
        self._final_text = final_text

    @property
    def text_stream(self):
        return iter(self._tokens)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return _FakeMessage(self._final_text)


class FakeAnthropicClient:
    """Recorded usage of the fake client during a test."""

    def __init__(self):
        self.calls: list[dict] = []
        self.tokens: tuple = ("Apples ", "are red. ", "(note.txt)")
        self.final_text: str = "Apples are red. (note.txt)"

    @property
    def messages(self):
        outer = self

        class _M:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                return _FakeStream(outer.tokens, outer.final_text)

        return _M()


class _FakeVoyageResponse:
    def __init__(self, embeddings):
        self.embeddings = embeddings


class FakeVoyageClient:
    def __init__(self):
        self.calls: list[dict] = []

    def embed(self, texts, model, input_type):
        self.calls.append({"texts": texts, "model": model, "input_type": input_type})
        return _FakeVoyageResponse([[1.0, 0.0, 0.0] for _ in texts])


# Stash the real lru_cached factories before any monkeypatching. Tests that
# want to exercise them (e.g. unavailable-key paths) can call these directly.
_REAL_EMB_CLIENT = emb_module._client
_REAL_CHAT_CLIENT = chat_module._client


@pytest.fixture(autouse=True)
def _stub_external_services(monkeypatch):
    """Auto-applied to every test: prevent real API calls.

    We stub the low-level `_client()` factories on both embeddings and chat,
    and pin the API keys in settings so `is_configured()` returns True. This
    keeps the public function code paths (validation, error handling,
    response shaping) under test instead of bypassed.
    """
    fake_anthropic = FakeAnthropicClient()
    fake_voyage = FakeVoyageClient()

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(settings, "VOYAGE_API_KEY", "test-voyage-key")

    _REAL_EMB_CLIENT.cache_clear()
    _REAL_CHAT_CLIENT.cache_clear()
    monkeypatch.setattr(emb_module, "_client", lambda: fake_voyage)
    monkeypatch.setattr(chat_module, "_client", lambda: fake_anthropic)

    yield fake_anthropic

    _REAL_EMB_CLIENT.cache_clear()
    _REAL_CHAT_CLIENT.cache_clear()


# ---------- outbound HTTP (services/outbound.py) ----------
#
# The SSRF guard resolves hosts and opens pinned connections. In tests the
# guard logic runs for real, but DNS answers a fixed public address for any
# hostname (IP literals stay as they are, so private ones are still
# blocked) and the connection goes through `urllib.request.urlopen`, which
# many tests stub. tests/test_outbound.py exercises the real transport.

_PUBLIC_TEST_IP = "93.184.216.34"


def _fake_getaddrinfo(host, port):
    import ipaddress
    import socket

    try:
        address = str(ipaddress.ip_address(host))
    except ValueError:
        address = _PUBLIC_TEST_IP
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


class _UrlopenResponse:
    def __init__(self, status, headers, body):
        from email.message import Message
        import io

        self.status = status
        self.msg = Message()
        for k, v in dict(headers or {}).items():
            self.msg[k] = v
        self._buf = io.BytesIO(body or b"")

    def getheader(self, name, default=None):
        return self.msg.get(name, default)

    def read(self, n=-1):
        return self._buf.read(n)


class _UrlopenConnection:
    def __init__(self, target, ip, timeout):
        self.target, self.timeout = target, timeout
        self._req = None

    def request(self, method, path, body=None, headers=None):
        import urllib.request

        t = self.target
        default = {"http": 80, "https": 443}[t.scheme]
        netloc = t.host if t.port == default else f"{t.host}:{t.port}"
        self._req = urllib.request.Request(
            f"{t.scheme}://{netloc}{path}", data=body,
            headers=dict(headers or {}), method=method,
        )

    def getresponse(self):
        import urllib.error
        import urllib.request

        try:
            r = urllib.request.urlopen(self._req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            return _UrlopenResponse(exc.code, {}, b"")
        return _UrlopenResponse(
            getattr(r, "status", 200), getattr(r, "headers", {}), r.read(),
        )

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _outbound_through_urlopen(monkeypatch):
    from filenergy.services import outbound

    monkeypatch.setattr(outbound, "_getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(outbound, "_open", _UrlopenConnection)


@pytest.fixture
def real_emb_client():
    """Yield the un-stubbed embeddings._client and clear its cache."""
    _REAL_EMB_CLIENT.cache_clear()
    yield _REAL_EMB_CLIENT
    _REAL_EMB_CLIENT.cache_clear()


@pytest.fixture
def real_chat_client():
    """Yield the un-stubbed chat._client and clear its cache."""
    _REAL_CHAT_CLIENT.cache_clear()
    yield _REAL_CHAT_CLIENT
    _REAL_CHAT_CLIENT.cache_clear()


@pytest.fixture
def app():
    """Provide the Flask app with a fresh DB per test.

    The app context stays open for the whole test so service-level code can
    touch `db.session` without manual `with app.app_context():` plumbing.
    """
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    upload_dir = Path(os.environ["FILENERGY_UPLOAD_DIR"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    with flask_app.app_context():
        _db.drop_all()
        _db.create_all()
        try:
            yield flask_app
        finally:
            _db.session.remove()
            _db.drop_all()
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user(app, db):
    """Create a user + their default workspace."""
    from filenergy.models import User
    from filenergy.services import workspaces

    u = User(email="alice@example.com", username="alice@example.com")
    u.set_password("password")
    db.session.add(u)
    db.session.commit()
    workspaces.ensure_default_for(u)
    return u


@pytest.fixture
def workspace(user):
    """The current user's default workspace."""
    from filenergy.services import workspaces

    return workspaces.list_for_user(user)[0]


@pytest.fixture
def auth_client(client, user):
    """Logged-in test client. The login also sets g.workspace."""
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    return client


@pytest.fixture
def uploaded_file(auth_client, db, user, workspace):
    """Upload one indexable file, return the resulting File row."""
    import io

    auth_client.post(
        "/file/upload/",
        data={
            "files[]": (
                io.BytesIO(b"Apples are red. Bananas are yellow."),
                "fruits.txt",
            ),
        },
        content_type="multipart/form-data",
    )
    from filenergy.models import File

    return File.query.filter_by(workspace_id=workspace.id).first()


# ---------- WebAuthn: software authenticator ----------

def _b64url(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


class SoftPasskey:
    """A P-256 software authenticator. `enroll` stores its public key the way
    the real registration ceremony does; `assertion` signs a genuine
    WebAuthn assertion that py_webauthn verifies like a browser's."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = _b64url(os.urandom(32))
        self.sign_count = 0

    def cose_public_key(self) -> bytes:
        import cbor2

        nums = self._key.public_key().public_numbers()
        return cbor2.dumps({
            1: 2,    # kty: EC2
            3: -7,   # alg: ES256
            -1: 1,   # crv: P-256
            -2: nums.x.to_bytes(32, "big"),
            -3: nums.y.to_bytes(32, "big"),
        })

    def enroll(self, db, user, label="Key"):
        from filenergy.models import WebAuthnCredential

        cred = WebAuthnCredential(
            user_id=user.id,
            credential_id=self.credential_id,
            public_key=_b64url(self.cose_public_key()),
            sign_count=0,
            label=label,
        )
        db.session.add(cred)
        db.session.commit()
        return cred

    def assertion(self, challenge: str, *, rp_id="localhost",
                  origin="http://localhost:5000", sign_count=None) -> dict:
        import hashlib
        import json as _json

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        self.sign_count = (
            self.sign_count + 1 if sign_count is None else sign_count
        )
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([0x01])  # user present
            + self.sign_count.to_bytes(4, "big")
        )
        client_data = _json.dumps({
            "type": "webauthn.get",
            "challenge": challenge,
            "origin": origin,
            "crossOrigin": False,
        }).encode()
        signature = self._key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": self.credential_id,
            "rawId": self.credential_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(client_data),
                "authenticatorData": _b64url(auth_data),
                "signature": _b64url(signature),
                "userHandle": None,
            },
        }


@pytest.fixture
def passkey(monkeypatch):
    """A fresh software authenticator; RP settings pinned to the defaults
    it signs for."""
    monkeypatch.delenv("FILENERGY_RP_ID", raising=False)
    monkeypatch.delenv("FILENERGY_RP_ORIGIN", raising=False)
    monkeypatch.delenv("FILENERGY_WEBAUTHN_DISABLED", raising=False)
    return SoftPasskey()
