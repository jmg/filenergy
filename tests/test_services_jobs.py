"""Tests for the job-queue abstraction."""
import os
import sys
import types

import pytest

from filenergy.services import jobs


# A module-level callable jobs.enqueue can resolve by string.
_calls: list = []


def _record(*args, **kwargs):
    _calls.append((args, kwargs))


def setup_function(_):
    _calls.clear()


def test_enqueue_runs_synchronously_in_testing(app):
    jobs.enqueue("tests.test_services_jobs._record", 1, 2, k="v")
    assert _calls == [((1, 2), {"k": "v"})]


def test_is_sync_true_in_testing(app):
    assert jobs.is_sync()


def test_is_sync_via_env(monkeypatch, app):
    app.config["TESTING"] = False
    monkeypatch.setenv("FILENERGY_SYNC_JOBS", "true")
    try:
        assert jobs.is_sync()
    finally:
        app.config["TESTING"] = True


def test_thread_backend_runs_target(app, monkeypatch):
    """When not sync, the thread backend dispatches via threading.Thread."""
    app.config["TESTING"] = False
    monkeypatch.delenv("FILENERGY_SYNC_JOBS", raising=False)
    monkeypatch.delenv("FILENERGY_JOBS_BACKEND", raising=False)

    class _FakeThread:
        def __init__(self, target, args=(), kwargs=None, name=None, daemon=None):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr("threading.Thread", _FakeThread)
    try:
        jobs.enqueue("tests.test_services_jobs._record", 7)
    finally:
        app.config["TESTING"] = True
    assert _calls == [((7,), {})]


def test_rq_backend_falls_back_when_redis_url_missing(app, monkeypatch):
    app.config["TESTING"] = False
    monkeypatch.setenv("FILENERGY_JOBS_BACKEND", "rq")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("FILENERGY_SYNC_JOBS", raising=False)

    class _FakeThread:
        def __init__(self, target, args=(), kwargs=None, **k):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("threading.Thread", _FakeThread)
    try:
        jobs.enqueue("tests.test_services_jobs._record", "fallback")
    finally:
        app.config["TESTING"] = True
    assert _calls == [(("fallback",), {})]


def test_rq_backend_falls_back_when_rq_not_installed(app, monkeypatch):
    app.config["TESTING"] = False
    monkeypatch.setenv("FILENERGY_JOBS_BACKEND", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.delenv("FILENERGY_SYNC_JOBS", raising=False)

    import builtins
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name in ("rq", "redis"):
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)

    class _FakeThread:
        def __init__(self, target, args=(), **k):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("threading.Thread", _FakeThread)
    try:
        jobs.enqueue("tests.test_services_jobs._record", "no-rq")
    finally:
        app.config["TESTING"] = True
    assert _calls == [(("no-rq",), {})]


def test_rq_backend_dispatches_to_queue(app, monkeypatch):
    app.config["TESTING"] = False
    monkeypatch.setenv("FILENERGY_JOBS_BACKEND", "rq")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.delenv("FILENERGY_SYNC_JOBS", raising=False)

    queued: list = []

    fake_redis = types.ModuleType("redis")

    class _Conn:
        @staticmethod
        def from_url(url):
            return object()

    fake_redis.Redis = _Conn

    fake_rq = types.ModuleType("rq")

    class _Queue:
        def __init__(self, name, connection):
            self.name = name

        def enqueue(self, fn, *args, **kwargs):
            queued.append((fn, args, kwargs))

    fake_rq.Queue = _Queue
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "rq", fake_rq)

    try:
        jobs.enqueue("tests.test_services_jobs._record", "queued")
    finally:
        app.config["TESTING"] = True
    assert queued and queued[0][1] == ("queued",)


def test_run_swallows_exceptions(app, caplog):
    def boom():
        raise RuntimeError("expected")

    sys.modules["__main__boom"] = types.ModuleType("__main__boom")
    sys.modules["__main__boom"].boom = boom
    with caplog.at_level("ERROR", logger="filenergy.services.jobs"):
        jobs._run("__main__boom.boom", (), {})
    assert any("Job" in r.message for r in caplog.records)
