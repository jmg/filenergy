from dataclasses import dataclass

from filenergy.models import Conversation, Message
from filenergy.services import conversations


def test_get_or_create_creates_when_no_id(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
    assert c.id is not None
    assert c.user_id == user.id
    assert c.workspace_id == workspace.id


def test_get_or_create_returns_existing(db, user, workspace, app):
    with app.test_request_context():
        first = conversations.get_or_create(user, workspace, None)
        again = conversations.get_or_create(user, workspace, first.id)
    assert again.id == first.id


def test_get_or_create_treats_other_users_id_as_new(db, user, workspace, app):
    from filenergy.models import User
    from filenergy.services import workspaces

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    other_ws = workspaces.ensure_default_for(other)
    with app.test_request_context():
        c = conversations.get_or_create(other, other_ws, None)
        new = conversations.get_or_create(user, workspace, c.id)
    assert new.id != c.id


def test_list_for_user_orders_desc(db, user, workspace, app):
    with app.test_request_context():
        a = conversations.get_or_create(user, workspace, None)
        b = conversations.get_or_create(user, workspace, None)
    listed = conversations.list_for_user(user, workspace)
    assert listed[0].id == b.id
    assert listed[1].id == a.id


def test_add_user_message_sets_title(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        msg = conversations.add_user_message(c, "What's in the docs?")
    assert msg.role == "user"
    assert "What's in the docs?" in c.title


def test_add_user_message_truncates_title(db, user, workspace, app):
    long_q = "a" * 80
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        conversations.add_user_message(c, long_q)
    assert c.title.endswith("...")
    assert len(c.title) <= 70


def test_add_assistant_message_with_dataclass_sources(db, user, workspace, app):
    @dataclass
    class _S:
        file_id: int
        name: str
        url: str
        score: float

    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        msg = conversations.add_assistant_message(
            c, "answer", [_S(1, "a.txt", "h", 0.9)]
        )
    assert msg.sources_json
    assert "a.txt" in msg.sources_json


def test_add_assistant_message_with_dict_sources(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        msg = conversations.add_assistant_message(
            c, "answer", [{"file_id": 1, "name": "x.txt", "url": "h", "score": 1.0}]
        )
    assert "x.txt" in msg.sources_json


def test_add_assistant_message_no_sources(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        msg = conversations.add_assistant_message(c, "answer", [])
    assert msg.sources_json is None


def test_history_returns_oldest_first(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        for i in range(5):
            conversations.add_user_message(c, f"q{i}")
            conversations.add_assistant_message(c, f"a{i}", [])
        out = conversations.history(c, limit=4)
    ids = [m.id for m in out]
    assert ids == sorted(ids)
    assert len(out) == 4


def test_delete_removes_thread(db, user, workspace, app):
    with app.test_request_context():
        c = conversations.get_or_create(user, workspace, None)
        cid = c.id
        assert conversations.delete(user, workspace, cid) is True
        assert Conversation.query.get(cid) is None
        assert conversations.delete(user, workspace, cid) is False


def test_delete_other_users_conversation_fails(db, user, workspace, app):
    from filenergy.models import User
    from filenergy.services import workspaces

    other = User(email="o@o", username="o")
    other.set_password("p")
    db.session.add(other)
    db.session.commit()
    other_ws = workspaces.ensure_default_for(other)
    with app.test_request_context():
        c = conversations.get_or_create(other, other_ws, None)
        assert conversations.delete(user, workspace, c.id) is False
