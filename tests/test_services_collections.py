from filenergy.models import Collection, File
from filenergy.services import collections


def test_create_assigns_unique_slug(db, workspace, app):
    with app.test_request_context():
        a = collections.create(workspace, "Engineering")
        b = collections.create(workspace, "Engineering")
    assert a.slug == "engineering"
    assert b.slug.startswith("engineering-")


def test_create_blank_name_falls_back(db, workspace, app):
    with app.test_request_context():
        c = collections.create(workspace, "")
    assert c.name == "Untitled"


def test_get_by_slug_filters_by_workspace(db, user, workspace, app):
    from filenergy.services import workspaces as ws_service

    other_user = _make_user(db, "x@x")
    other_ws = ws_service.ensure_default_for(other_user)
    with app.test_request_context():
        mine = collections.create(workspace, "mine")
        theirs = collections.create(other_ws, "mine")  # same name, different ws
    assert collections.get_by_slug(workspace, "mine").id == mine.id
    assert collections.get_by_slug(workspace, "mine").id != theirs.id


def test_list_for_workspace_isolates(db, user, workspace, app):
    from filenergy.services import workspaces as ws_service

    other_user = _make_user(db, "x@x")
    other_ws = ws_service.ensure_default_for(other_user)
    with app.test_request_context():
        collections.create(workspace, "a")
        collections.create(workspace, "b")
        collections.create(other_ws, "c")
    listed = collections.list_for_workspace(workspace)
    assert {c.name for c in listed} == {"a", "b"}


def test_rename_updates_name(db, workspace, app):
    with app.test_request_context():
        c = collections.create(workspace, "Old")
        collections.rename(c, "New")
    assert c.name == "New"


def test_rename_blank_is_noop(db, workspace, app):
    with app.test_request_context():
        c = collections.create(workspace, "Old")
        collections.rename(c, "   ")
    assert c.name == "Old"


def test_delete_uncategorizes_files(db, user, workspace, app):
    with app.test_request_context():
        c = collections.create(workspace, "trash")
        f = File(
            user_id=user.id, workspace_id=workspace.id,
            collection_id=c.id, name="x", path="/x", url="u1",
        )
        db.session.add(f)
        db.session.commit()
        collections.delete(c)
    assert Collection.query.count() == 0
    assert File.query.first().collection_id is None


def test_assign_file(db, user, workspace, app):
    with app.test_request_context():
        c = collections.create(workspace, "engineering")
        f = File(
            user_id=user.id, workspace_id=workspace.id,
            name="x", path="/x", url="u1",
        )
        db.session.add(f)
        db.session.commit()
        collections.assign_file(f, c)
        assert f.collection_id == c.id
        collections.assign_file(f, None)
        assert f.collection_id is None


def test_files_in(db, user, workspace, app):
    with app.test_request_context():
        c = collections.create(workspace, "engineering")
        for n in ("a", "b", "c"):
            f = File(
                user_id=user.id, workspace_id=workspace.id,
                collection_id=c.id, name=n, path=f"/{n}", url=n,
            )
            db.session.add(f)
        db.session.commit()
    assert {f.name for f in collections.files_in(c)} == {"a", "b", "c"}


def test_get_by_id(db, workspace, app):
    with app.test_request_context():
        c = collections.create(workspace, "x")
    assert collections.get(workspace, c.id).id == c.id
    assert collections.get(workspace, 9999) is None


def _make_user(db, email):
    from filenergy.models import User

    u = User(email=email, username=email)
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u
