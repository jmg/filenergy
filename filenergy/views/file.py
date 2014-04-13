from filenergy import app, login_manager
from flask import render_template, request, g, redirect, url_for, flash, make_response
from flask.ext.login import login_required
from filenergy.models import File

from filenergy.services.file import FileService


@app.route("/file/list/")
@login_required
def list():

    context = {}
    context["files"] = FileService().filter_by(user=g.user)

    return render_template("file/list.html", **context)


@app.route("/file/upload/")
@login_required
def upload():

    return render_template("file/upload.html")


@app.route("/file/upload/", methods=['POST'])
def upload_post():

    FileService().save_file(request.files, g.user)

    return redirect(url_for("list"))


@app.route("/file/download/")
def download_post():

    context = {}
    context["file"] = FileService().get_object_or_404(url=request.args.get("h"))
    context["file_size"] = FileService().get_size(context["file"])

    return render_template("file/download.html", **context)


@app.route("/file/downloadnow/")
def download():

    db_file = FileService().get_object_or_404(url=request.args.get("h"))
    content = FileService().get_content(db_file)

    response = make_response(content)
    response.headers["Content-Disposition"] = "attachment; filename={0}".format(db_file.name)
    return response


@app.route("/file/search/", methods=['POST'])
def search():

    context = {}
    file_name = request.form.get("name")
    context["files"] = FileService().filter(File.name.like("%{0}%".format(file_name))).all()

    return render_template("file/search.html", **context)


@app.route("/file/delete/", methods=["POST"])
def delete():

    db_file = FileService().get_one(user=g.user, id=request.form.get("id"))

    if not FileService().delete(db_file):
        return "fail"

    return "ok"
