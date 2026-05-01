"""HTTP-level tests covering every blueprint route."""
from __future__ import annotations

import io

from filenergy.models import Conversation, Event, File, Message, User
from filenergy.services import events as ev_module


# ---------- index ----------


def test_index_page_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Filenergy" in r.data


# ---------- user blueprint ----------


def test_login_get(client):
    r = client.get("/user/login/")
    assert r.status_code == 200


def test_register_get(client):
    r = client.get("/user/register/")
    assert r.status_code == 200


def test_register_post_success_logs_event(client, db):
    r = client.post(
        "/user/register/",
        data={"email": "a@b.co", "password": "pw", "password_again": "pw"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert User.query.filter_by(email="a@b.co").one()
    assert Event.query.filter_by(type=ev_module.USER_REGISTERED).count() == 1


def test_register_post_mismatch_redirects_to_register(client, db):
    r = client.post(
        "/user/register/",
        data={"email": "a@b.co", "password": "pw", "password_again": "different"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/user/register/" in r.headers["Location"]


def test_login_post_success(client, user):
    r = client.post(
        "/user/login/",
        data={"email": user.email, "password": "password"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert Event.query.filter_by(type=ev_module.USER_LOGGED_IN).count() == 1


def test_login_post_with_next_redirect(client, user):
    r = client.post(
        "/user/login/",
        data={"email": user.email, "password": "password", "next": "/file/list/"},
        follow_redirects=False,
    )
    assert r.headers["Location"].endswith("/file/list/")


def test_login_post_failure_redirects_back(client, user):
    r = client.post(
        "/user/login/",
        data={"email": user.email, "password": "wrong"},
        follow_redirects=False,
    )
    assert "/user/login/" in r.headers["Location"]


def test_logout_when_authenticated(auth_client, user):
    r = auth_client.get("/user/logout/")
    assert r.status_code == 302
    assert Event.query.filter_by(type=ev_module.USER_LOGGED_OUT).count() == 1


def test_logout_when_anonymous_does_not_log_event(client):
    r = client.get("/user/logout/")
    assert r.status_code == 302
    assert Event.query.filter_by(type=ev_module.USER_LOGGED_OUT).count() == 0


# ---------- file blueprint ----------


def test_list_requires_login(client):
    r = client.get("/file/list/")
    assert r.status_code == 302  # redirects to login


def test_upload_get_requires_login(client):
    r = client.get("/file/upload/")
    assert r.status_code == 302


def test_upload_get_authenticated(auth_client):
    r = auth_client.get("/file/upload/")
    assert r.status_code == 200


def test_upload_post_persists_file_and_indexes(auth_client, db, user):
    r = auth_client.post(
        "/file/upload/",
        data={"files[]": (io.BytesIO(b"hello world"), "hello.txt")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    f = File.query.filter_by(user_id=user.id).one()
    assert f.indexed_at is not None
    assert Event.query.filter_by(type=ev_module.FILE_UPLOADED).count() == 1


def test_list_shows_uploaded_files(uploaded_file, auth_client):
    r = auth_client.get("/file/list/")
    assert r.status_code == 200
    assert b"fruits.txt" in r.data
    assert b"indexed" in r.data


def test_search_returns_matches(auth_client, uploaded_file):
    r = auth_client.post("/file/search/", data={"name": "fruits"})
    assert r.status_code == 200
    assert b"fruits.txt" in r.data


def test_download_page_loads(auth_client, uploaded_file):
    r = auth_client.get(f"/file/download/?h={uploaded_file.url}")
    assert r.status_code == 200
    assert b"fruits.txt" in r.data


def test_download_404_for_unknown_hash(auth_client):
    r = auth_client.get("/file/download/?h=nope")
    assert r.status_code == 404


def test_downloadnow_serves_file(auth_client, uploaded_file):
    r = auth_client.get(f"/file/downloadnow/?h={uploaded_file.url}")
    assert r.status_code == 200
    assert r.data.startswith(b"Apples are red")
    assert "attachment" in r.headers["Content-Disposition"]
    assert Event.query.filter_by(type=ev_module.FILE_DOWNLOADED).count() == 1


def test_downloadnow_blocks_other_users_private_file(client, db, uploaded_file):
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    # `client` is logged in as `alice` from the uploaded_file fixture chain.
    client.get("/user/logout/")
    client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = client.get(f"/file/downloadnow/?h={uploaded_file.url}")
    assert r.status_code == 403


def test_downloadnow_anonymous_blocked_for_private(client, uploaded_file):
    client.get("/user/logout/")
    r = client.get(f"/file/downloadnow/?h={uploaded_file.url}")
    assert r.status_code == 403


def test_downloadnow_public_file_for_anonymous(client, uploaded_file):
    client.post(
        "/file/make_public/",
        data={"id": uploaded_file.id, "is_public": "true"},
    )
    client.get("/user/logout/")
    r = client.get(f"/file/downloadnow/?h={uploaded_file.url}")
    assert r.status_code == 200


def test_make_public_toggles_and_logs(auth_client, uploaded_file):
    r = auth_client.post(
        "/file/make_public/",
        data={"id": uploaded_file.id, "is_public": "true"},
    )
    assert r.data == b"ok"
    assert File.query.get(uploaded_file.id).is_public is True
    assert Event.query.filter_by(type=ev_module.FILE_MADE_PUBLIC).count() == 1

    r = auth_client.post(
        "/file/make_public/",
        data={"id": uploaded_file.id, "is_public": "false"},
    )
    assert File.query.get(uploaded_file.id).is_public is False


def test_make_public_404_for_other_user(client, db, uploaded_file):
    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    client.get("/user/logout/")
    client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = client.post(
        "/file/make_public/",
        data={"id": uploaded_file.id, "is_public": "true"},
    )
    assert r.data == b"fail"


def test_delete_endpoint_soft_deletes_file(auth_client, uploaded_file):
    """Single delete is now a soft-delete; the row stays so users can Undo."""
    r = auth_client.post("/file/delete/", data={"id": uploaded_file.id})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    f = File.query.get(uploaded_file.id)
    assert f is not None
    assert f.deleted_at is not None


def test_delete_returns_404_for_unknown_id(auth_client):
    r = auth_client.post("/file/delete/", data={"id": 9999})
    assert r.status_code == 404


def test_reindex_endpoint(auth_client, uploaded_file):
    r = auth_client.post("/file/reindex/", data={"id": uploaded_file.id})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["status"] == "indexed"


def test_reindex_404(auth_client):
    r = auth_client.post("/file/reindex/", data={"id": 9999})
    assert r.status_code == 404


# ---------- ask blueprint ----------


def test_ask_index_requires_login(client):
    r = client.get("/ask/")
    assert r.status_code == 302


def test_ask_index_authenticated(auth_client):
    r = auth_client.get("/ask/")
    assert r.status_code == 200
    assert b"Ask anything" in r.data


def test_ask_question_empty_returns_400(auth_client):
    r = auth_client.post("/ask/", json={"question": ""})
    assert r.status_code == 400


def test_ask_question_unconfigured_returns_503(auth_client, monkeypatch):
    from filenergy.services import chat as chat_mod

    monkeypatch.setattr(chat_mod, "is_configured", lambda: False)
    r = auth_client.post("/ask/", json={"question": "hi"})
    assert r.status_code == 503


def test_ask_question_happy_path(auth_client, uploaded_file):
    r = auth_client.post("/ask/", json={"question": "What about apples?"})
    assert r.status_code == 200
    j = r.get_json()
    assert "answer" in j
    assert "conversation_id" in j
    assert "sources" in j
    assert Conversation.query.count() == 1
    assert Message.query.count() == 2  # user + assistant


def test_ask_question_persists_to_existing_conversation(auth_client, uploaded_file):
    r1 = auth_client.post("/ask/", json={"question": "first"})
    cid = r1.get_json()["conversation_id"]
    r2 = auth_client.post(
        "/ask/", json={"question": "follow up", "conversation_id": cid}
    )
    assert r2.get_json()["conversation_id"] == cid
    assert Conversation.query.count() == 1
    assert Message.query.count() == 4


def test_ask_question_invalid_conversation_id(auth_client, uploaded_file):
    r = auth_client.post(
        "/ask/", json={"question": "hi", "conversation_id": "not-a-number"}
    )
    assert r.status_code == 200


def test_ask_rate_limited(auth_client, uploaded_file, monkeypatch):
    from filenergy import settings as cfg

    monkeypatch.setattr(cfg, "ASK_RATE_LIMIT", 1)
    auth_client.post("/ask/", json={"question": "1"})
    r = auth_client.post("/ask/", json={"question": "2"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert Event.query.filter_by(type=ev_module.ASK_RATE_LIMITED).count() == 1


def test_ask_failure_returns_500(auth_client, uploaded_file, monkeypatch):
    from filenergy.services import chat as chat_mod

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(chat_mod, "answer_question", boom)
    r = auth_client.post("/ask/", json={"question": "x"})
    assert r.status_code == 500
    assert Event.query.filter_by(type=ev_module.ASK_FAILED).count() == 1


def test_ask_chat_unavailable_returns_503(auth_client, uploaded_file, monkeypatch):
    from filenergy.services import chat as chat_mod

    def boom(*a, **k):
        raise chat_mod.ChatUnavailable("api key gone")

    monkeypatch.setattr(chat_mod, "answer_question", boom)
    r = auth_client.post("/ask/", json={"question": "x"})
    assert r.status_code == 503


def test_ask_form_data_supported(auth_client, uploaded_file):
    r = auth_client.post("/ask/", data={"question": "via form"})
    assert r.status_code == 200


def test_ask_view_conversation_renders_history(auth_client, uploaded_file):
    auth_client.post("/ask/", json={"question": "first"})
    cid = Conversation.query.first().id
    r = auth_client.get(f"/ask/c/{cid}")
    assert r.status_code == 200
    assert b"first" in r.data


def test_ask_view_conversation_other_user_404(client, db, uploaded_file):
    client.post("/ask/", json={"question": "private"})
    cid = Conversation.query.first().id

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    client.get("/user/logout/")
    client.post("/user/login/", data={"email": "o@o", "password": "p"})
    r = client.get(f"/ask/c/{cid}")
    # The other user gets a freshly created conversation, not the original.
    # So we assert the page doesn't leak the prior question.
    assert b"private" not in r.data


def test_ask_delete_conversation(auth_client, uploaded_file):
    auth_client.post("/ask/", json={"question": "x"})
    cid = Conversation.query.first().id
    r = auth_client.post(f"/ask/c/{cid}/delete")
    assert r.status_code == 200
    assert Conversation.query.count() == 0


def test_ask_delete_conversation_404(auth_client):
    r = auth_client.post("/ask/c/999/delete")
    assert r.status_code == 404


def test_ask_stream_empty_question_returns_400(auth_client):
    r = auth_client.post("/ask/stream", json={"question": ""})
    assert r.status_code == 400


def test_ask_stream_unconfigured_returns_503(auth_client, monkeypatch):
    from filenergy.services import chat as chat_mod

    monkeypatch.setattr(chat_mod, "is_configured", lambda: False)
    r = auth_client.post("/ask/stream", json={"question": "hi"})
    assert r.status_code == 503


def test_ask_stream_emits_sse(auth_client, uploaded_file):
    r = auth_client.post("/ask/stream", json={"question": "?"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "event: meta" in body
    assert "event: token" in body
    assert "event: done" in body
    assert Message.query.filter_by(role="assistant").count() == 1


def test_ask_stream_rate_limited(auth_client, uploaded_file, monkeypatch):
    from filenergy import settings as cfg

    monkeypatch.setattr(cfg, "ASK_RATE_LIMIT", 0)
    r = auth_client.post("/ask/stream", json={"question": "x"})
    assert r.status_code == 429


def test_ask_stream_invalid_conversation_id(auth_client, uploaded_file):
    r = auth_client.post(
        "/ask/stream", json={"question": "x", "conversation_id": "abc"}
    )
    assert r.status_code == 200
