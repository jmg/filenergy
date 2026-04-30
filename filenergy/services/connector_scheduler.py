"""Background scheduler for connector sync.

A single daemon thread wakes up every `interval_minutes`, walks every
ConnectorAccount whose `last_synced_at` is older than the interval, and
fires its connector's `sync()`. Errors are logged onto the account row
and don't crash the loop.

Started lazily on first request via `ensure_started()`. Disabled in
TESTING and when `FILENERGY_SYNC_SCHEDULER` is `false`.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import timedelta

from filenergy import app, db
from filenergy.models import ConnectorAccount, User, Workspace, utcnow
from filenergy.services import connectors

log = logging.getLogger(__name__)


_DEFAULT_INTERVAL_MINUTES = 60
_started = False
_lock = threading.Lock()


def is_enabled() -> bool:
    if os.environ.get("FILENERGY_SYNC_SCHEDULER", "true").lower() == "false":
        return False
    if app.config.get("TESTING"):
        return False
    return True


def interval_minutes() -> int:
    try:
        return int(os.environ.get("FILENERGY_SYNC_INTERVAL_MIN", _DEFAULT_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_MINUTES


def ensure_started() -> None:
    """Idempotently spin up the scheduler thread."""
    global _started
    if not is_enabled():
        return
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(
            target=_loop, name="connector-scheduler", daemon=True,
        ).start()


def _loop() -> None:
    while True:
        try:
            with app.app_context():
                run_due_syncs()
        except Exception:
            log.exception("connector scheduler tick failed")
        time.sleep(max(60, interval_minutes() * 60))


def run_due_syncs(*, now=None) -> list[dict]:
    """Run sync() on every account that's due. Returns a per-account result list.

    Public so a CLI / cron-driven setup can call it directly.
    """
    now = now or utcnow()
    cutoff = now - timedelta(minutes=interval_minutes())
    due_accounts = (
        ConnectorAccount.query.filter(
            (ConnectorAccount.last_synced_at.is_(None))
            | (ConnectorAccount.last_synced_at <= cutoff)
        ).all()
    )
    out: list[dict] = []
    for account in due_accounts:
        connector = connectors.get(account.kind)
        if connector is None:
            continue
        workspace = Workspace.query.get(account.workspace_id)
        if workspace is None:
            continue
        # Sync runs as the workspace owner (the human who connected).
        owner = User.query.get(workspace.owner_id)
        if owner is None:
            continue
        try:
            result = connector.sync(account, user=owner, workspace=workspace)
            out.append({"account_id": account.id, **result})
        except Exception as exc:
            account.last_error = str(exc)[:500]
            db.session.commit()
            out.append({"account_id": account.id, "error": str(exc)})
    return out
