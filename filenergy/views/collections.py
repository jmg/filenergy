"""Collections (folders) UI and CRUD."""
from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from filenergy.models import File
from filenergy.services import collections

collections_bp = Blueprint("collections", __name__)


@collections_bp.route("/")
@login_required
def list_collections():
    return render_template(
        "collections/list.html",
        collections=collections.list_for_workspace(g.workspace),
    )


@collections_bp.route("/", methods=["POST"])
@login_required
def create():
    name = request.form.get("name", "")
    description = request.form.get("description", "")
    coll = collections.create(g.workspace, name, description)
    return redirect(url_for("collections.view", slug=coll.slug))


@collections_bp.route("/<slug>")
@login_required
def view(slug):
    coll = collections.get_by_slug(g.workspace, slug)
    if coll is None:
        abort(404)
    files = collections.files_in(coll)
    other_collections = [
        c for c in collections.list_for_workspace(g.workspace) if c.id != coll.id
    ]
    return render_template(
        "collections/view.html",
        collection=coll,
        files=files,
        other_collections=other_collections,
    )


@collections_bp.route("/<slug>/rename", methods=["POST"])
@login_required
def rename(slug):
    coll = collections.get_by_slug(g.workspace, slug)
    if coll is None:
        abort(404)
    collections.rename(coll, request.form.get("name", ""))
    return redirect(url_for("collections.view", slug=coll.slug))


@collections_bp.route("/<slug>/delete", methods=["POST"])
@login_required
def delete(slug):
    coll = collections.get_by_slug(g.workspace, slug)
    if coll is None:
        abort(404)
    collections.delete(coll)
    return redirect(url_for("collections.list_collections"))


@collections_bp.route("/assign", methods=["POST"])
@login_required
def assign():
    """Move a file into a collection (or out, when collection_id is empty)."""
    file_id = request.form.get("file_id")
    coll_id = request.form.get("collection_id")
    f = File.query.filter_by(id=file_id, workspace_id=g.workspace.id).first()
    if f is None:
        return jsonify(error="File not found"), 404
    coll = None
    if coll_id:
        coll = collections.get(g.workspace, int(coll_id))
        if coll is None:
            return jsonify(error="Collection not found"), 404
    collections.assign_file(f, coll)
    return jsonify(ok=True, collection_id=coll.id if coll else None)
