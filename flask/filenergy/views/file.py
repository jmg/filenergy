from filenergy import app, login_manager
from flask import render_template, request

from filenergy.services.file import FileService


@app.route("/file/list/")
def list():

    context = {}
    context["files"] = FileService().filter(user=request.user)

    return render_template("file/mine.html", context)


@app.route("/file/upload/", methods=['GET'])
def upload():

    return render_template("file/upload.html")


@app.route("/file/upload/", methods=['POST'])
def upload_post():

    FileService().save_file(self.request.FILES, self.request.user)

    return self.redirect("/file/")


@app.route("/file/download/", methods=['POST'])
def download_post():

    context = {}
    context["file"] = FileService().get_object_or_404(url=request.form.get("h"))
    context["file_size"] = FileService().get_size(context["file"])

    return render_template("file/download.html", context)


@app.route("/file/download/")
def download():

    db_file = FileService().get_object_or_404(url=form.GET.get("h"))

    return response_file(db_file.path)


@app.route("/file/search/", methods=['POST'])
def search():

    context = {}
    file_name = request.form.get("name")
    context["files"] = FileService().filter(name__contains=file_name)

    return render_template("file/search.html", context)


@app.route("/file/delete/")
def delete():

    db_file = FileService().get_one(user=request.user, id=request.form.get("id"))

    if not FileService().delete(db_file):
        return "fail"

    return "ok"


@app.route('/upload', methods=['GET', 'POST'])
def upload_file():

    if request.method == 'POST':
        f = request.files['the_file']
        f.save('/var/www/uploads/uploaded_file.txt')