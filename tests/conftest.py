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
    """Create a user via the service layer (so password hashing works)."""
    from filenergy.models import User

    u = User(email="alice@example.com", username="alice@example.com")
    u.set_password("password")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def auth_client(client, user):
    """Logged-in test client."""
    client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
    )
    return client


@pytest.fixture
def uploaded_file(auth_client, db, user):
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

    return File.query.filter_by(user_id=user.id).first()
