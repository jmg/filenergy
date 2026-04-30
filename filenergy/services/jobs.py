"""Background job dispatch with two backends.

`thread`: spawn a daemon thread (default; zero-config).
`rq`:     push to a Redis queue (when REDIS_URL + a worker process exist).

Tests force synchronous execution by setting `SYNC_JOBS=true` or running
under `app.config["TESTING"]`.

Use:
    jobs.enqueue("filenergy.services.file._index_file_id", file_id)

The target must be importable by string, because RQ workers don't share
the request's process. For thread mode we resolve the dotted path at
dispatch time.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading

from filenergy import app

log = logging.getLogger(__name__)


def _import_target(dotted: str):
    module_path, _, attr = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _backend() -> str:
    return (os.environ.get("FILENERGY_JOBS_BACKEND") or "thread").lower()


def is_sync() -> bool:
    return (
        os.environ.get("FILENERGY_SYNC_JOBS") == "true"
        or app.config.get("TESTING", False)
    )


def enqueue(target: str, *args, **kwargs) -> None:
    """Schedule a background job. Returns immediately."""
    if is_sync():
        _run(target, args, kwargs)
        return
    backend = _backend()
    if backend == "rq":
        _enqueue_rq(target, args, kwargs)
    else:
        _enqueue_thread(target, args, kwargs)


def _run(target: str, args: tuple, kwargs: dict) -> None:
    fn = _import_target(target)
    with app.app_context():
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("Job %s failed", target)


def _enqueue_thread(target: str, args: tuple, kwargs: dict) -> None:
    threading.Thread(
        target=_run, args=(target, args, kwargs),
        name=f"job-{target.rsplit('.', 1)[-1]}", daemon=True,
    ).start()


def _enqueue_rq(target: str, args: tuple, kwargs: dict) -> None:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        log.warning("FILENERGY_JOBS_BACKEND=rq but REDIS_URL not set; "
                    "falling back to thread")
        _enqueue_thread(target, args, kwargs)
        return
    try:
        from redis import Redis  # type: ignore
        from rq import Queue  # type: ignore
    except ImportError:
        log.warning("rq/redis not installed; falling back to thread")
        _enqueue_thread(target, args, kwargs)
        return
    queue_name = os.environ.get("FILENERGY_RQ_QUEUE", "filenergy")
    queue = Queue(queue_name, connection=Redis.from_url(redis_url))
    fn = _import_target(target)
    queue.enqueue(fn, *args, **kwargs)
