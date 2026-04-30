from filenergy.middleware import load_user


def test_load_user_returns_user(app, user):
    with app.test_request_context():
        u = load_user(str(user.id))
    assert u.id == user.id


def test_load_user_unknown(app):
    with app.test_request_context():
        assert load_user("9999") is None


def test_before_request_sets_g_user(client):
    """The `g.user` proxy should be wired for every request."""
    with client:
        client.get("/")
        from flask import g

        # `g.user` is set by the before_request hook.
        assert hasattr(g, "user")
