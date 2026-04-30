import mimetypes

from flask import Blueprint, g, make_response, render_template, request
from flask_login import login_required

from filenergy.services.file import FileService

file_bp = Blueprint("file", __name__)


@file_bp.route("/list/")
@login_required
def list_files():
    files = FileService().filter_by(user=g.user).all()
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
    content = FileService().get_content(db_file)

    response = make_response(content)
    mime, _ = mimetypes.guess_type(db_file.name)
    response.headers["Content-Type"] = mime or "application/octet-stream"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{db_file.name}"'
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


@file_bp.route("/make_public/", methods=["POST"])
@login_required
def make_public():
    db_file = FileService().get_one(id=request.form.get("id"), user=g.user)
    if db_file is None:
        return "fail"
    db_file.is_public = request.form.get("is_public") == "true"
    FileService().save(db_file)
    return "ok"
