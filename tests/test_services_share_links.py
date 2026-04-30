from datetime import timedelta

from filenergy.models import File, ShareLink, utcnow
from filenergy.services import share_links


def _make_file(db, user, workspace):
    f = File(
        user_id=user.id, workspace_id=workspace.id,
        name="x.txt", path="/tmp/x", url="hh",
    )
    db.session.add(f)
    db.session.commit()
    return f


def test_create_with_ttl_and_max(db, user, workspace, app):
    f = _make_file(db, user, workspace)
    with app.test_request_context():
        link = share_links.create(
            f, created_by=user, ttl_hours=24, max_downloads=5
        )
    assert link.token
    assert link.expires_at is not None
    assert link.max_downloads == 5
    assert link.is_active()


def test_find_active_returns_link(db, user, workspace, app):
    f = _make_file(db, user, workspace)
    with app.test_request_context():
        link = share_links.create(f, created_by=user)
        found = share_links.find_active(link.token)
    assert found is not None
    assert found.id == link.id


def test_find_active_returns_none_if_revoked(db, user, workspace, app):
    f = _make_file(db, user, workspace)
    with app.test_request_context():
        link = share_links.create(f, created_by=user)
        share_links.revoke(link)
    assert share_links.find_active(link.token) is None


def test_find_active_returns_none_if_expired(db, user, workspace, app):
    f = _make_file(db, user, workspace)
    with app.test_request_context():
        link = share_links.create(f, created_by=user)
        link.expires_at = utcnow() - timedelta(hours=1)
        from filenergy import db as real_db

        real_db.session.commit()
    assert share_links.find_active(link.token) is None


def test_find_active_returns_none_if_max_downloads_reached(db, user, workspace, app):
    f = _make_file(db, user, workspace)
    with app.test_request_context():
        link = share_links.create(f, created_by=user, max_downloads=1)
        share_links.record_download(link)
    assert share_links.find_active(link.token) is None


def test_record_download_increments(db, user, workspace, app):
    f = _make_file(db, user, workspace)
    with app.test_request_context():
        link = share_links.create(f, created_by=user)
        share_links.record_download(link)
        share_links.record_download(link)
    assert link.download_count == 2


def test_find_active_unknown_token(app):
    with app.test_request_context():
        assert share_links.find_active("nope") is None


def test_list_for_file(db, user, workspace, app):
    f = _make_file(db, user, workspace)
    with app.test_request_context():
        share_links.create(f, created_by=user)
        share_links.create(f, created_by=user)
    assert len(share_links.list_for_file(f)) == 2
