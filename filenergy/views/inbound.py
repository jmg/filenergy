"""Inbound email webhook.

Receives JSON POSTs from inbound-mail providers (Postmark, SendGrid,
Mailgun, Cloudflare Email Workers). The endpoint is CSRF-exempt and
authenticates by:

  - matching the To address to a per-workspace deterministic local-part
    derived from a shared secret, AND
  - optionally a `X-Inbound-Secret` header that providers can sign with.

The endpoint is a noop unless `FILENERGY_INBOUND_DOMAIN` and
`FILENERGY_INBOUND_SECRET` are both set.
"""
from __future__ import annotations

import hmac
import os

from flask import Blueprint, jsonify, request

from filenergy.services import inbound_email

inbound_bp = Blueprint("inbound", __name__)


def _check_provider_secret() -> bool:
    """Optional shared-secret header — providers like Postmark let you
    set a static string. Skip when not configured."""
    expected = os.environ.get("FILENERGY_INBOUND_SHARED_SECRET")
    if not expected:
        return True
    presented = request.headers.get("X-Inbound-Secret", "")
    return hmac.compare_digest(expected, presented)


@inbound_bp.route("/email", methods=["POST"])
def email():
    if not inbound_email.is_configured():
        return jsonify(error="inbound disabled"), 503
    if not _check_provider_secret():
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(error="bad payload"), 400
    result = inbound_email.ingest_payload(payload)
    if not result.get("ok"):
        # Return 200 anyway so providers don't keep retrying a bad
        # address; log internally instead.
        return jsonify(result), 200
    return jsonify(result), 200
