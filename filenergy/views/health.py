"""Liveness + readiness probes + Prometheus metrics.

- `/healthz` returns 200 instantly with no DB hit — for k8s liveness.
- `/readyz` does a cheap SELECT 1 to confirm DB and reports config status.
- `/metrics` exposes Prometheus-format counters/histograms (in-process).
"""
from flask import Blueprint, Response, jsonify

from filenergy import db
from filenergy.services import billing, chat, embeddings, metrics

health_bp = Blueprint("health", __name__)


@health_bp.route("/healthz")
def healthz():
    return jsonify(ok=True)


@health_bp.route("/metrics")
def metrics_endpoint():
    return Response(metrics.render(), mimetype="text/plain; version=0.0.4")


@health_bp.route("/readyz")
def readyz():
    db_ok = True
    db_error = None
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_error = str(exc)[:200]

    body = {
        "ok": db_ok,
        "db": {"ok": db_ok, "error": db_error},
        "anthropic": chat.is_configured(),
        "embeddings": embeddings.is_configured(),
        "stripe": billing.is_configured(),
    }
    return jsonify(body), (200 if db_ok else 503)
