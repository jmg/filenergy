import pytest

from filenergy import settings
from filenergy.services import events, rate_limit


def test_check_ask_under_limit(db, user, app, monkeypatch):
    monkeypatch.setattr(settings, "ASK_RATE_LIMIT", 5)
    monkeypatch.setattr(settings, "ASK_RATE_WINDOW_SECONDS", 60)
    with app.test_request_context():
        for _ in range(4):
            events.log_event(events.ASK_QUESTION, user=user)
        rate_limit.check_ask(user)  # should NOT raise


def test_check_ask_over_limit(db, user, app, monkeypatch):
    monkeypatch.setattr(settings, "ASK_RATE_LIMIT", 2)
    monkeypatch.setattr(settings, "ASK_RATE_WINDOW_SECONDS", 60)
    with app.test_request_context():
        for _ in range(2):
            events.log_event(events.ASK_QUESTION, user=user)
        with pytest.raises(rate_limit.RateLimited) as exc:
            rate_limit.check_ask(user)
    assert exc.value.limit == 2
    assert exc.value.window == 60
    assert exc.value.retry_after == 60


def test_check_ask_anonymous_passes(db, app):
    with app.test_request_context():
        rate_limit.check_ask(None)  # no-op
        rate_limit.check_ask(type("Anon", (), {"id": None})())
