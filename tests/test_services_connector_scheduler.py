"""Tests for the connector sync scheduler."""
import os
from datetime import timedelta

import pytest

from filenergy.models import ConnectorAccount, utcnow
from filenergy.services import connector_scheduler


def test_is_enabled_false_in_testing(app):
    assert connector_scheduler.is_enabled() is False


def test_is_enabled_when_flag_off(app, monkeypatch):
    app.config["TESTING"] = False
    monkeypatch.setenv("FILENERGY_SYNC_SCHEDULER", "false")
    try:
        assert connector_scheduler.is_enabled() is False
    finally:
        app.config["TESTING"] = True


def test_is_enabled_default_true(app, monkeypatch):
    app.config["TESTING"] = False
    monkeypatch.delenv("FILENERGY_SYNC_SCHEDULER", raising=False)
    try:
        assert connector_scheduler.is_enabled() is True
    finally:
        app.config["TESTING"] = True


def test_interval_minutes_default(monkeypatch):
    monkeypatch.delenv("FILENERGY_SYNC_INTERVAL_MIN", raising=False)
    assert connector_scheduler.interval_minutes() == 60


def test_interval_minutes_env_override(monkeypatch):
    monkeypatch.setenv("FILENERGY_SYNC_INTERVAL_MIN", "5")
    assert connector_scheduler.interval_minutes() == 5


def test_interval_minutes_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("FILENERGY_SYNC_INTERVAL_MIN", "not-a-number")
    assert connector_scheduler.interval_minutes() == 60


def test_ensure_started_noop_in_testing(app):
    """In TESTING the scheduler thread is never spun up."""
    connector_scheduler._started = False
    connector_scheduler.ensure_started()
    assert connector_scheduler._started is False


def test_ensure_started_spawns_thread_when_enabled(app, monkeypatch):
    """When TESTING is off and the flag isn't disabled, a daemon thread starts."""
    app.config["TESTING"] = False
    monkeypatch.delenv("FILENERGY_SYNC_SCHEDULER", raising=False)
    connector_scheduler._started = False
    captured = {}

    class _FakeThread:
        def __init__(self, target, name=None, daemon=None):
            captured["target"] = target
            captured["name"] = name
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr("threading.Thread", _FakeThread)
    try:
        connector_scheduler.ensure_started()
        # idempotent — second call is a no-op.
        connector_scheduler.ensure_started()
    finally:
        app.config["TESTING"] = True
        connector_scheduler._started = False
    assert captured.get("started") is True
    assert captured["name"] == "connector-scheduler"
    assert captured["daemon"] is True


def test_loop_runs_one_iteration_then_breaks(app, monkeypatch):
    """Drive `_loop` once: time.sleep raises, breaking us out cleanly."""
    ticks: list = []

    def fake_run_due_syncs():
        ticks.append("ran")
        return []

    def stop_sleep(_seconds):
        # Raise to break the infinite loop after the first iteration.
        raise SystemExit("test stop")

    monkeypatch.setattr(connector_scheduler, "run_due_syncs", fake_run_due_syncs)
    monkeypatch.setattr("time.sleep", stop_sleep)
    with pytest.raises(SystemExit):
        connector_scheduler._loop()
    assert ticks == ["ran"]


def test_loop_swallows_exceptions(app, monkeypatch):
    """An exception inside run_due_syncs is logged and the loop continues
    until the next sleep raises."""
    calls: list = []

    def boom():
        calls.append("boom")
        raise RuntimeError("kaboom")

    def stop_sleep(_):
        raise SystemExit("stop")

    monkeypatch.setattr(connector_scheduler, "run_due_syncs", boom)
    monkeypatch.setattr("time.sleep", stop_sleep)
    with pytest.raises(SystemExit):
        connector_scheduler._loop()
    assert calls == ["boom"]


def test_run_due_syncs_skips_when_owner_missing(db, workspace, app, monkeypatch):
    """Owner of the workspace was deleted — sync skipped, no crash."""
    workspace.owner_id = 9999  # nonexistent
    db.session.commit()
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="t", last_synced_at=None,
    )
    db.session.add(a)
    db.session.commit()
    with app.test_request_context():
        results = connector_scheduler.run_due_syncs()
    assert results == []


def test_run_due_syncs_skips_recent(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
        last_synced_at=utcnow() - timedelta(minutes=5),  # too recent
    )
    db.session.add(a)
    db.session.commit()
    monkeypatch.setenv("FILENERGY_SYNC_INTERVAL_MIN", "60")

    called = []
    monkeypatch.setattr(
        "filenergy.services.connectors.GoogleDriveConnector.sync",
        lambda self, account, *, user, workspace: called.append(account.id) or {"created": 0, "skipped": 0},
    )
    with app.test_request_context():
        results = connector_scheduler.run_due_syncs()
    assert results == []
    assert called == []


def test_run_due_syncs_runs_stale(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
        last_synced_at=utcnow() - timedelta(hours=2),  # stale
    )
    db.session.add(a)
    db.session.commit()
    monkeypatch.setenv("FILENERGY_SYNC_INTERVAL_MIN", "60")

    monkeypatch.setattr(
        "filenergy.services.connectors.GoogleDriveConnector.sync",
        lambda self, account, *, user, workspace: {"created": 3, "skipped": 1},
    )
    with app.test_request_context():
        results = connector_scheduler.run_due_syncs()
    assert results == [{"account_id": a.id, "created": 3, "skipped": 1}]


def test_run_due_syncs_runs_never_synced(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        account_label="me@x", access_token="t",
        last_synced_at=None,
    )
    db.session.add(a)
    db.session.commit()
    monkeypatch.setattr(
        "filenergy.services.connectors.GoogleDriveConnector.sync",
        lambda self, account, *, user, workspace: {"created": 1, "skipped": 0},
    )
    with app.test_request_context():
        results = connector_scheduler.run_due_syncs()
    assert results and results[0]["created"] == 1


def test_run_due_syncs_records_error(db, user, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="google_drive",
        access_token="t", last_synced_at=None,
    )
    db.session.add(a)
    db.session.commit()

    def boom(self, account, *, user, workspace):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        "filenergy.services.connectors.GoogleDriveConnector.sync", boom
    )
    with app.test_request_context():
        results = connector_scheduler.run_due_syncs()
    assert results and "error" in results[0]
    db.session.refresh(a)
    assert a.last_error and "provider down" in a.last_error


def test_run_due_syncs_skips_unknown_kind(db, workspace, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=workspace.id, kind="lol-not-real",
        access_token="t", last_synced_at=None,
    )
    db.session.add(a)
    db.session.commit()
    with app.test_request_context():
        results = connector_scheduler.run_due_syncs()
    assert results == []


def test_run_due_syncs_skips_when_workspace_missing(db, app, monkeypatch):
    a = ConnectorAccount(
        workspace_id=99999, kind="google_drive",
        access_token="t", last_synced_at=None,
    )
    db.session.add(a)
    db.session.commit()
    with app.test_request_context():
        results = connector_scheduler.run_due_syncs()
    assert results == []
