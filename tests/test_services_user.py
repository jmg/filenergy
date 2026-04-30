from filenergy.models import User
from filenergy.services.user import UserService


def test_register_success(client, db):
    with client:
        client.get("/")  # ensure session
        err = UserService().register("a@b", "pw", "pw")
        assert err is None
        assert User.query.filter_by(email="a@b").one()


def test_register_password_mismatch(client, db):
    err = UserService().register("a@b", "pw", "different")
    assert "match" in err.lower()


def test_register_duplicate_email(client, db, user):
    err = UserService().register(user.email, "pw", "pw")
    assert "already exists" in err.lower()


def test_register_username_defaults_to_email(client, db):
    with client:
        client.get("/")
        UserService().register("c@d", "pw", "pw")
    u = User.query.filter_by(email="c@d").one()
    assert u.username == "c@d"


def test_login_success(client, user):
    with client:
        client.get("/")
        err = UserService().login(user.email, "password")
        assert err is None


def test_login_wrong_password(client, user):
    err = UserService().login(user.email, "wrong")
    assert err == "Email or password incorrect."


def test_login_unknown_email(client, db):
    err = UserService().login("nope@x", "pw")
    assert err == "Email or password incorrect."


def test_logout(client, user):
    with client:
        client.post(
            "/user/login/", data={"email": user.email, "password": "password"}
        )
        UserService().logout()  # idempotent
