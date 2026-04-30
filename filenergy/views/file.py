import mimetypes

from flask import Blueprint, g, jsonify, make_response, render_template, request
from flask_login import login_required

from filenergy.models import File
from filenergy.services import events
from filenergy.services.file import FileService

file_bp = Blueprint("file", __name__)


@file_bp.route("/list/")
@login_required
def list_files():
    files = (
        File.query.filter_by(user_id=g.user.id).order_by(File.id.desc()).all()
    )
    return render_template("file/list.html", files=files)


@file_bp.route("/upload/")
@login_required
def upload():
    return render_template("file/upload.html")


@file_bp.route("/upload/", methods=["POST"])
@login_required
def upload_post():
    return FileService().save_file(request, g.user)


@file_bp.route("/download/")
def download_post():
    db_file = FileService().get_object_or_404(url=request.args.get("h"))
    return render_template(
        "file/download.html",
        file=db_file,
        file_size=FileService().get_size(db_file),
    )


@file_bp.route("/downloadnow/")
def download():
    db_file = FileService().get_object_or_404(url=request.args.get("h"))

    if not db_file.is_public:
        if not g.user.is_authenticated or db_file.user_id != g.user.id:
            return "Forbidden", 403

    content = FileService().get_content(db_file)

    response = make_response(content)
    mime, _ = mimetypes.guess_type(db_file.name)
    response.headers["Content-Type"] = mime or "application/octet-stream"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{db_file.name}"'
    )
    events.log_event(
        events.FILE_DOWNLOADED,
        user=g.user if g.user.is_authenticated else None,
        file_id=db_file.id,
        name=db_file.name,
    )
    return response


@file_bp.route("/search/", methods=["POST"])
def search():
    files = FileService().search(g.user, request.form.get("name", ""))
    return render_template("file/search.html", files=files)


@file_bp.route("/delete/", methods=["POST"])
@login_required
def delete():
    db_file = FileService().get_one(user=g.user, id=request.form.get("id"))
    if not FileService().delete(db_file):
        return "fail"
    return "ok"


@file_bp.route("/reindex/", methods=["POST"])
@login_required
def reindex():
    db_file = FileService().get_one(id=request.form.get("id"), user=g.user)
    if db_file is None:
        return jsonify(error="File not found"), 404
    ok = FileService().index_file(db_file)
    if ok:
        events.log_event(events.FILE_REINDEXED, user=g.user, file_id=db_file.id)
    return jsonify(
        ok=ok,
        indexed=db_file.indexed_at is not None,
        error=db_file.index_error,
        status=db_file.index_status,
    )


@file_bp.route("/make_public/", methods=["POST"])
@login_required
def make_public():
    db_file = FileService().get_one(id=request.form.get("id"), user=g.user)
    if db_file is None:
        return "fail"
    is_public = request.form.get("is_public") == "true"
    db_file.is_public = is_public
    FileService().save(db_file)
    events.log_event(
        events.FILE_MADE_PUBLIC if is_public else events.FILE_MADE_PRIVATE,
        user=g.user,
        file_id=db_file.id,
    )
    return "ok"
