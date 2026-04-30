from filenergy.models import (
    Chunk,
    Conversation,
    Event,
    File,
    Message,
    User,
    utcnow,
)


def test_utcnow_returns_aware_datetime():
    now = utcnow()
    assert now.tzinfo is not None


def test_user_password_hash_and_check(db):
    u = User(email="x@y.co", username="x")
    u.set_password("secret")
    assert u.password != "secret"
    assert u.check_password("secret")
    assert not u.check_password("wrong")


def test_user_flask_login_protocol(db):
    u = User(email="x@y.co", username="x", id=42)
    assert u.is_authenticated is True
    assert u.is_active is True
    assert u.is_anonymous is False
    assert u.get_id() == "42"


def test_user_str_falls_back(db):
    u = User(email="x@y.co", username="x")
    assert str(u) == "x@y.co"
    u.email = None
    assert str(u) == "x"
    u.username = None
    u.id = 7
    assert str(u) == "User<7>"


def test_file_index_status_pending_indexed_error(db, user):
    f = File(user_id=user.id, name="a.txt", path="/tmp/a.txt", url="h")
    db.session.add(f)
    db.session.commit()
    assert f.index_status == "pending"
    f.index_error = "boom"
    assert f.index_status == "error"
    f.indexed_at = utcnow()
    assert f.index_status == "indexed"


def test_chunk_belongs_to_file(db, user):
    f = File(user_id=user.id, name="a.txt", path="/tmp/a.txt", url="h")
    db.session.add(f)
    db.session.commit()
    c = Chunk(file_id=f.id, position=0, content="hello", embedding="[]")
    db.session.add(c)
    db.session.commit()
    assert c.file is f


def test_conversation_and_messages(db, user):
    c = Conversation(user_id=user.id, title="t")
    db.session.add(c)
    db.session.commit()
    db.session.add_all([
        Message(conversation_id=c.id, role="user", content="hi"),
        Message(conversation_id=c.id, role="assistant", content="hi back"),
    ])
    db.session.commit()
    assert c.messages.count() == 2
    assert [m.role for m in c.messages] == ["user", "assistant"]


def test_event_optional_user(db, user):
    e1 = Event(type="x", user_id=user.id)
    e2 = Event(type="y")
    db.session.add_all([e1, e2])
    db.session.commit()
    assert Event.query.count() == 2
    assert e2.user is None
