import json
import time

from filenergy.models import Event
from filenergy.services import events


def test_log_event_persists(db, user, app):
    with app.test_request_context():
        e = events.log_event(events.FILE_UPLOADED, user=user, file_id=42)
    assert e.id is not None
    assert e.user_id == user.id
    assert json.loads(e.metadata_json)["file_id"] == 42


def test_log_event_without_user(db, app):
    with app.test_request_context():
        events.log_event("anonymous.something")
    e = Event.query.first()
    assert e.user_id is None


def test_log_event_handles_failure_gracefully(db, monkeypatch, app):
    """If commit explodes, the event helper swallows the error."""
    from filenergy import db as real_db

    def bad_commit():
        raise RuntimeError("disk full")

    monkeypatch.setattr(real_db.session, "commit", bad_commit)
    with app.test_request_context():
        result = events.log_event("noisy")
    assert result is None


def test_count_recent_window(db, user, app):
    with app.test_request_context():
        for _ in range(3):
            events.log_event(events.ASK_QUESTION, user=user)
        assert events.count_recent(user, events.ASK_QUESTION, since_seconds=60) == 3
        # Past window — nothing in the last 0 seconds.
        time.sleep(0.01)
        assert events.count_recent(user, events.ASK_QUESTION, since_seconds=0) == 0


def test_count_recent_filters_by_type(db, user, app):
    with app.test_request_context():
        events.log_event(events.ASK_QUESTION, user=user)
        events.log_event(events.FILE_UPLOADED, user=user)
        assert events.count_recent(user, events.ASK_QUESTION, since_seconds=60) == 1


def test_log_event_with_user_object_no_id(db, app):
    """A user-like object without a real id falls through to user_id=None."""

    class _NotAUser:
        pass

    with app.test_request_context():
        e = events.log_event("phantom", user=_NotAUser())
    assert e.user_id is None
