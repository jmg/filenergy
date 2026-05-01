from flask import Blueprint, g, jsonify, render_template, request
from flask_login import login_required

from filenergy.services import search as search_service

index_bp = Blueprint("index", __name__)


@index_bp.route("/")
def index():
    return render_template("index/index.html")


@index_bp.route("/search")
@login_required
def universal_search():
    """Powers the ⌘K command palette. Authenticated users only — the
    palette is a UI affordance, not an API-key surface.
    """
    q = request.args.get("q", "")
    results = search_service.search(g.workspace, q, limit=6)
    return jsonify(results=results)
