import logging
import time

from flask import g, request
from flask_login import current_user

from filenergy import app, login_manager
from filenergy.services import (
    connector_scheduler,
    metrics,
    sessions as session_service,
    workspaces,
)
from filenergy.services.user import UserService

log = logging.getLogger("filenergy.request")


@app.before_request
def before_request():
    # Lazy-start the connector sync scheduler (no-op in TESTING).
    connector_scheduler.ensure_started()
    g.user = current_user
    # Reject cookies whose UserSession row was revoked (log-out
    # everywhere). Anonymous + token-less requests pass through.
    if not session_service.is_session_alive():
        from flask_login import logout_user
        logout_user()
    g.workspace = (
        workspaces.get_current(current_user)
        if getattr(current_user, "is_authenticated", False)
        else None
    )
    # Cheap last_seen update on authenticated requests.
    sess = session_service.current()
    if sess is not None:
        session_service.touch(sess)
    g._request_started_at = time.monotonic()


# Content Security Policy. Stricter would block the inline scripts in
# our Bootstrap-3 templates and the Swagger UI CDN at /api/v1/docs;
# loosening 'self' + listed CDN hosts is the minimum that keeps the app
# working without giving up framing/eval/object-tag protection.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://avatars.githubusercontent.com; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "form-action 'self' https://accounts.google.com https://*.dropbox.com "
    "https://api.notion.com https://slack.com https://stripe.com"
)

_SECURITY_HEADERS = {
    # Force HTTPS for a year + preload + subdomains. Harmless on HTTP
    # since browsers ignore HSTS over HTTP. Set
    # FILENERGY_DISABLE_HSTS=true to opt out (dev / unusual setups).
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": _CSP,
}


@app.after_request
def after_request(response):
    """Per-request log line + metrics observation + security headers."""
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

    import os
    for name, value in _SECURITY_HEADERS.items():
        if name == "Strict-Transport-Security" and (
            os.environ.get("FILENERGY_DISABLE_HSTS", "").lower() == "true"
        ):
            continue
        response.headers.setdefault(name, value)
    return response


@login_manager.user_loader
def load_user(user_id):
    return UserService().get_one(id=int(user_id))
