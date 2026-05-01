"""Background job dispatch with two backends.

`thread`: spawn a daemon thread (default; zero-config).
`rq`:     push to a Redis queue (when REDIS_URL + a worker process exist).

Tests force synchronous execution by setting `SYNC_JOBS=true` or running
under `app.config["TESTING"]`.

Retries: pass `retries=N` (and optionally `retry_backoff_seconds=...`) to
get exponential-backoff retries for transient failures. Works in all
three modes:

  - sync: retries inline with `time.sleep` between attempts
  - thread: same, but in the daemon thread
  - rq: hands off to RQ's native `Retry` policy

Use:
    jobs.enqueue("filenergy.services.file._index_file_id", file_id, retries=3)

The target must be importable by string, because RQ workers don't share
the request's process. For thread mode we resolve the dotted path at
dispatch time.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
import time

from filenergy import app

log = logging.getLogger(__name__)


_DEFAULT_RETRIES = 0
_DEFAULT_BACKOFF_SECONDS = 2


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


def enqueue(
    target: str,
    *args,
    retries: int = _DEFAULT_RETRIES,
    retry_backoff_seconds: int = _DEFAULT_BACKOFF_SECONDS,
    **kwargs,
) -> None:
    """Schedule a background job. Returns immediately.

    `retries`: how many times to retry on exception before giving up.
    `retry_backoff_seconds`: base for exponential backoff (sleep = base * 2**attempt).
    """
    if is_sync():
        _run(target, args, kwargs, retries, retry_backoff_seconds)
        return
    backend = _backend()
    if backend == "rq":
        _enqueue_rq(target, args, kwargs, retries, retry_backoff_seconds)
    else:
        _enqueue_thread(target, args, kwargs, retries, retry_backoff_seconds)


def _run(
    target: str, args: tuple, kwargs: dict,
    retries: int, backoff: int,
) -> None:
    fn = _import_target(target)
    attempts = 0
    last_exc = None
    with app.app_context():
        while attempts <= retries:
            try:
                fn(*args, **kwargs)
                return
            except Exception as exc:
                last_exc = exc
                if attempts >= retries:
                    log.exception(
                        "Job %s failed after %d attempt(s)", target, attempts + 1,
                    )
                    return
                sleep_s = backoff * (2 ** attempts)
                log.warning(
                    "Job %s attempt %d failed (%s); retrying in %ds",
                    target, attempts + 1, exc, sleep_s,
                )
                # Skip the wait in tests so retries are fast.
                if not app.config.get("TESTING"):
                    time.sleep(sleep_s)
                attempts += 1
    if last_exc is not None:
        log.error("Job %s exhausted retries: %s", target, last_exc)


def _enqueue_thread(
    target: str, args: tuple, kwargs: dict,
    retries: int, backoff: int,
) -> None:
    threading.Thread(
        target=_run,
        args=(target, args, kwargs, retries, backoff),
        name=f"job-{target.rsplit('.', 1)[-1]}",
        daemon=True,
    ).start()


def _enqueue_rq(
    target: str, args: tuple, kwargs: dict,
    retries: int, backoff: int,
) -> None:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        log.warning(
            "FILENERGY_JOBS_BACKEND=rq but REDIS_URL not set; "
            "falling back to thread"
        )
        _enqueue_thread(target, args, kwargs, retries, backoff)
        return
    try:
        from redis import Redis  # type: ignore
        from rq import Queue  # type: ignore
    except ImportError:
        log.warning("rq/redis not installed; falling back to thread")
        _enqueue_thread(target, args, kwargs, retries, backoff)
        return
    queue_name = os.environ.get("FILENERGY_RQ_QUEUE", "filenergy")
    queue = Queue(queue_name, connection=Redis.from_url(redis_url))
    fn = _import_target(target)
    if retries > 0:
        try:
            from rq import Retry  # type: ignore
        except ImportError:
            queue.enqueue(fn, *args, **kwargs)
            return
        intervals = [backoff * (2 ** i) for i in range(retries)]
        queue.enqueue(
            fn, *args,
            retry=Retry(max=retries, interval=intervals),
            **kwargs,
        )
    else:
        queue.enqueue(fn, *args, **kwargs)
