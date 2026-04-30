from dataclasses import dataclass

from filenergy.models import Conversation, Message
from filenergy.services import conversations


def test_get_or_create_creates_when_no_id(db, user, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, None)
    assert c.id is not None
    assert c.user_id == user.id


def test_get_or_create_returns_existing(db, user, app):
    with app.test_request_context():
        first = conversations.get_or_create(user, None)
        again = conversations.get_or_create(user, first.id)
    assert again.id == first.id


def test_get_or_create_treats_other_users_id_as_new(db, user, app):
    from filenergy.models import User

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    with app.test_request_context():
        c = conversations.get_or_create(other, None)
        # User asks for `c.id` but they aren't `other`; should create a fresh one.
        new = conversations.get_or_create(user, c.id)
    assert new.id != c.id


def test_list_for_user_orders_desc(db, user, app):
    with app.test_request_context():
        a = conversations.get_or_create(user, None)
        b = conversations.get_or_create(user, None)
    listed = conversations.list_for_user(user)
    assert listed[0].id == b.id
    assert listed[1].id == a.id


def test_add_user_message_sets_title(db, user, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, None)
        msg = conversations.add_user_message(c, "What's in the docs?")
    assert msg.role == "user"
    assert "What's in the docs?" in c.title


def test_add_user_message_truncates_title(db, user, app):
    long_q = "a" * 80
    with app.test_request_context():
        c = conversations.get_or_create(user, None)
        conversations.add_user_message(c, long_q)
    assert c.title.endswith("...")
    assert len(c.title) <= 70


def test_add_assistant_message_with_dataclass_sources(db, user, app):
    @dataclass
    class _S:
        file_id: int
        name: str
        url: str
        score: float

    with app.test_request_context():
        c = conversations.get_or_create(user, None)
        msg = conversations.add_assistant_message(
            c, "answer", [_S(1, "a.txt", "h", 0.9)]
        )
    assert msg.sources_json
    assert "a.txt" in msg.sources_json


def test_add_assistant_message_with_dict_sources(db, user, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, None)
        msg = conversations.add_assistant_message(
            c, "answer", [{"file_id": 1, "name": "x.txt", "url": "h", "score": 1.0}]
        )
    assert "x.txt" in msg.sources_json


def test_add_assistant_message_no_sources(db, user, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, None)
        msg = conversations.add_assistant_message(c, "answer", [])
    assert msg.sources_json is None


def test_history_returns_oldest_first(db, user, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, None)
        for i in range(5):
            conversations.add_user_message(c, f"q{i}")
            conversations.add_assistant_message(c, f"a{i}", [])
        out = conversations.history(c, limit=4)
    # 4 most recent, but in oldest-first order — IDs must be ascending.
    ids = [m.id for m in out]
    assert ids == sorted(ids)
    assert len(out) == 4


def test_delete_removes_thread(db, user, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, None)
        cid = c.id
        assert conversations.delete(user, cid) is True
        assert Conversation.query.get(cid) is None
        assert conversations.delete(user, cid) is False  # second time → False


def test_delete_other_users_conversation_fails(db, user, app):
    from filenergy.models import User

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    with app.test_request_context():
        c = conversations.get_or_create(other, None)
        assert conversations.delete(user, c.id) is False
