import logging
import time

from flask import g, request
from flask_login import current_user

from filenergy import app, login_manager
from filenergy.services import metrics, workspaces
from filenergy.services.user import UserService

log = logging.getLogger("filenergy.request")


@app.before_request
def before_request():
    g.user = current_user
    g.workspace = (
        workspaces.get_current(current_user)
        if getattr(current_user, "is_authenticated", False)
        else None
    )
    g._request_started_at = time.monotonic()


@app.after_request
def after_request(response):
    """Emit a structured per-request log line + metrics observation."""
    started_at = getattr(g, "_request_started_at", None)
    duration = time.monotonic() - started_at if started_at else 0.0
    endpoint = request.endpoint or "unknown"
    status = response.status_code

    log.info(
        "request",
        extra={
            "endpoint": endpoint,
            "method": request.method,
            "path": request.path,
            "status": status,
            "duration_ms": round(duration * 1000, 2),
            "user_id": getattr(g.user, "id", None) if g.user.is_authenticated else None,
            "workspace_id": getattr(g.workspace, "id", None) if g.workspace else None,
        },
    )
    if not request.path.startswith("/static/"):
        metrics.inc(
            "filenergy_http_requests_total",
            {"endpoint": endpoint, "method": request.method, "status": str(status)},
        )
        metrics.observe(
            "filenergy_http_request_duration_seconds",
            duration,
            {"endpoint": endpoint, "method": request.method},
        )
    return response


@login_manager.user_loader
def load_user(user_id):
    return UserService().get_one(id=int(user_id))
