import io
import sys

import pytest

from filenergy.admin import AuthModelView
from filenergy.models import File, User


def test_authmodelview_blocks_anonymous(client):
    """Admin pages should redirect/forbid anonymous users."""
    r = client.get("/admin/admin_user/")
    # Flask-Admin returns 403 or redirects depending on config.
    assert r.status_code in (302, 403, 404)


def test_authmodelview_blocks_non_superuser(auth_client):
    r = auth_client.get("/admin/admin_user/")
    assert r.status_code in (302, 403, 404)


def test_authmodelview_allows_superuser(client, db):
    u = User(email="root@x", username="root", is_superuser=True)
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    client.post("/user/login/", data={"email": "root@x", "password": "pw"})
    r = client.get("/admin/admin_user/")
    assert r.status_code == 200


def test_authmodelview_is_accessible_unit(app, db, user):
    """Unit-level: is_accessible() reads g.user."""
    with app.test_request_context():
        from flask import g

        g.user = user  # not a superuser
        assert AuthModelView(File, db.session).is_accessible() is False
        user.is_superuser = True
        db.session.commit()
        assert AuthModelView(File, db.session).is_accessible() is True


# ---------- manage.py CLI ----------


def test_cmd_create_superuser(client, db, capsys):
    import manage

    manage.cmd_create_superuser("root@x", "pw")
    captured = capsys.readouterr()
    assert "created superuser" in captured.out
    u = User.query.filter_by(email="root@x").one()
    assert u.is_superuser is True


def test_cmd_create_superuser_idempotent(client, db, capsys):
    import manage

    u = User(email="root@x", username="root", is_superuser=True)
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()

    manage.cmd_create_superuser("root@x", "pw")
    captured = capsys.readouterr()
    assert "already exists" in captured.out


def test_cmd_reindex(client, db, user, capsys, auth_client):
    auth_client.post(
        "/file/upload/",
        data={"files[]": (io.BytesIO(b"hello world"), "x.txt")},
        content_type="multipart/form-data",
    )
    import manage

    manage.cmd_reindex()
    captured = capsys.readouterr()
    assert "reindexed" in captured.out


def test_main_unknown_command_exits(monkeypatch, capsys):
    import manage

    with pytest.raises(SystemExit):
        manage.main(["manage.py", "garbage-command"])
    captured = capsys.readouterr()
    # Help text printed on stdout.
    assert "Usage" in captured.out


def test_main_dispatches_to_reindex(monkeypatch):
    import manage

    called = []
    monkeypatch.setattr(manage, "cmd_reindex", lambda: called.append(True))
    manage.main(["manage.py", "reindex"])
    assert called == [True]


def test_main_dispatches_to_create_superuser(monkeypatch):
    import manage

    captured = []
    monkeypatch.setattr(
        manage,
        "cmd_create_superuser",
        lambda email, password: captured.append((email, password)),
    )
    manage.main(["manage.py", "create-superuser", "u@e", "pw"])
    assert captured == [("u@e", "pw")]


def test_main_no_args_runs_server(monkeypatch):
    import manage

    called = {}

    def fake_run(**kw):
        called.update(kw)

    monkeypatch.setattr(manage.app, "run", fake_run)
    manage.main(["manage.py"])
    assert called.get("debug") is True
    assert called.get("port") == 5000
