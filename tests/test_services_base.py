import pytest

from filenergy.models import User
from filenergy.services.base import BaseService
from filenergy.services.user import UserService


def test_baseservice_requires_entity():
    svc = BaseService()
    with pytest.raises(Exception, match="entity must be"):
        svc.filter_by(email="x")


def test_get_one_returns_match(db, user):
    svc = UserService()
    found = svc.get_one(email=user.email)
    assert found.id == user.id


def test_get_one_missing_returns_none(db):
    assert UserService().get_one(email="nope@x") is None


def test_new_creates_unsaved_entity(db):
    u = UserService().new(email="z@x", username="z")
    assert isinstance(u, User)
    assert u.id is None


def test_get_or_new(db, user):
    svc = UserService()
    existing = svc.get_or_new(email=user.email)
    assert existing.id == user.id
    fresh = svc.get_or_new(email="never@seen")
    assert fresh.id is None


def test_get_object_or_404(db, user, app):
    with app.test_request_context():
        from werkzeug.exceptions import NotFound

        assert UserService().get_object_or_404(email=user.email).id == user.id
        with pytest.raises(NotFound):
            UserService().get_object_or_404(email="nope@x")


def test_save_persists(db):
    u = User(email="s@x", username="s")
    u.set_password("pw")
    UserService().save(u)
    assert u.id is not None


def test_getattr_delegates_to_query(db, user):
    """Untyped methods proxy through to .query."""
    svc = UserService()
    # `count` is a query method, not on BaseService directly.
    assert svc.count() == 1
