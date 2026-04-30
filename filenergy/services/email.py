"""Tiny email service.

Two adapters: `log` (default — never sends, prints to stderr/logger) and
`smtp` (uses stdlib `smtplib`). The adapter is selected by env var
`FILENERGY_EMAIL_ADAPTER`. Both adapters are sync; for production volume
swap to a queue + provider (Postmark, SES, Resend).
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


class EmailError(RuntimeError):
    pass


def _adapter() -> str:
    return (os.environ.get("FILENERGY_EMAIL_ADAPTER") or "log").lower()


def _from() -> str:
    return os.environ.get("FILENERGY_EMAIL_FROM", "filenergy@example.com")


def send(to: str, subject: str, body: str) -> bool:
    """Send a single plain-text email. Returns True on success.

    Failures are logged, not raised — callers (invitations, notifications)
    treat email as best-effort.
    """
    adapter = _adapter()
    if adapter == "log":
        log.info("[email:%s] To=%s Subject=%s\n%s", adapter, to, subject, body)
        return True
    if adapter == "smtp":
        return _send_smtp(to, subject, body)
    log.error("Unknown FILENERGY_EMAIL_ADAPTER=%s", adapter)
    return False


def _send_smtp(to: str, subject: str, body: str) -> bool:
    host = os.environ.get("FILENERGY_SMTP_HOST")
    if not host:
        log.error("FILENERGY_SMTP_HOST not set")
        return False
    port = int(os.environ.get("FILENERGY_SMTP_PORT", "587"))
    user = os.environ.get("FILENERGY_SMTP_USER")
    password = os.environ.get("FILENERGY_SMTP_PASSWORD")
    use_tls = os.environ.get("FILENERGY_SMTP_TLS", "true").lower() == "true"

    msg = EmailMessage()
    msg["From"] = _from()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if user:
                server.login(user, password or "")
            server.send_message(msg)
        return True
    except Exception:
        log.exception("SMTP send failed")
        return False
