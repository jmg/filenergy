import mimetypes

from flask import Blueprint, g, jsonify, make_response, render_template, request
from flask_login import login_required

from filenergy.models import File
from filenergy.services import billing, collections, events, share_links
from filenergy.services.file import FileService

file_bp = Blueprint("file", __name__)


@file_bp.route("/<int:file_id>")
@login_required
def detail(file_id):
    from flask import abort
    f = File.query.filter_by(id=file_id, workspace_id=g.workspace.id).first()
    if f is None:
        abort(404)
    return render_template(
        "file/detail.html",
        file=f,
        all_collections=collections.list_for_workspace(g.workspace),
        share_links_active=share_links.list_for_file(f),
    )


@file_bp.route("/list/")
@login_required
def list_files():
    files = (
        File.query.filter_by(workspace_id=g.workspace.id)
        .order_by(File.id.desc())
        .all()
    )
    return render_template(
        "file/list.html",
        files=files,
        usage=billing.usage_summary(g.workspace),
    )


@file_bp.route("/upload/")
@login_required
def upload():
    return render_template(
        "file/upload.html", usage=billing.usage_summary(g.workspace)
    )


@file_bp.route("/upload/", methods=["POST"])
@login_required
def upload_post():
    try:
        billing.ensure_can_upload(g.workspace)
    except billing.QuotaExceeded as exc:
        events.log_event(
            events.UPLOAD_QUOTA_EXCEEDED,
            user=g.user,
            workspace_id=g.workspace.id,
            kind=exc.kind,
        )
        return jsonify(error=str(exc), kind=exc.kind), 402  # Payment Required
    return FileService().save_file(request, g.user, g.workspace)


@file_bp.route("/download/")
@login_required
def download_post():
    db_file = FileService().get_object_or_404(url=request.args.get("h"))
    if db_file.workspace_id != g.workspace.id and not db_file.is_public:
        return "Forbidden", 403
    return render_template(
        "file/download.html",
        file=db_file,
        file_size=FileService().get_size(db_file),
        share_links_active=share_links.list_for_file(db_file),
    )


@file_bp.route("/downloadnow/")
def download():
    db_file = FileService().get_object_or_404(url=request.args.get("h"))

    if not db_file.is_public:
        if not g.user.is_authenticated:
            return "Forbidden", 403
        if db_file.workspace_id != (g.workspace.id if g.workspace else None):
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
        workspace_id=db_file.workspace_id,
        file_id=db_file.id,
        name=db_file.name,
    )
    return response


@file_bp.route("/search/", methods=["POST"])
def search():
    workspace = g.workspace if g.user.is_authenticated else None
    files = FileService().search(workspace, g.user, request.form.get("name", ""))
    return render_template("file/search.html", files=files)


@file_bp.route("/delete/", methods=["POST"])
@login_required
def delete():
    db_file = File.query.filter_by(
        id=request.form.get("id"), workspace_id=g.workspace.id
    ).first()
    if not FileService().delete(db_file):
        return "fail"
    return "ok"


@file_bp.route("/reindex/", methods=["POST"])
@login_required
def reindex():
    db_file = File.query.filter_by(
        id=request.form.get("id"), workspace_id=g.workspace.id
    ).first()
    if db_file is None:
        return jsonify(error="File not found"), 404
    ok = FileService().index_file(db_file)
    if ok:
        events.log_event(
            events.FILE_REINDEXED,
            user=g.user,
            workspace_id=g.workspace.id,
            file_id=db_file.id,
        )
    return jsonify(
        ok=ok,
        indexed=db_file.indexed_at is not None,
        error=db_file.index_error,
        status=db_file.index_status,
    )


@file_bp.route("/make_public/", methods=["POST"])
@login_required
def make_public():
    db_file = File.query.filter_by(
        id=request.form.get("id"), workspace_id=g.workspace.id
    ).first()
    if db_file is None:
        return "fail"
    is_public = request.form.get("is_public") == "true"
    db_file.is_public = is_public
    FileService().save(db_file)
    events.log_event(
        events.FILE_MADE_PUBLIC if is_public else events.FILE_MADE_PRIVATE,
        user=g.user,
        workspace_id=g.workspace.id,
        file_id=db_file.id,
    )
    return "ok"


# ---- Share links ----


@file_bp.route("/share/", methods=["POST"])
@login_required
def create_share():
    db_file = File.query.filter_by(
        id=request.form.get("id"), workspace_id=g.workspace.id
    ).first()
    if db_file is None:
        return jsonify(error="File not found"), 404
    ttl_hours = request.form.get("ttl_hours")
    max_downloads = request.form.get("max_downloads")
    link = share_links.create(
        db_file,
        created_by=g.user,
        ttl_hours=int(ttl_hours) if ttl_hours and ttl_hours.isdigit() else None,
        max_downloads=(
            int(max_downloads) if max_downloads and max_downloads.isdigit() else None
        ),
    )
    events.log_event(
        events.FILE_SHARED,
        user=g.user,
        workspace_id=g.workspace.id,
        file_id=db_file.id,
        link_id=link.id,
    )
    return jsonify(token=link.token, expires_at=link.expires_at.isoformat() if link.expires_at else None)


@file_bp.route("/share/<int:link_id>/revoke", methods=["POST"])
@login_required
def revoke_share(link_id):
    from filenergy.models import ShareLink

    link = (
        ShareLink.query.join(File, ShareLink.file_id == File.id)
        .filter(ShareLink.id == link_id, File.workspace_id == g.workspace.id)
        .first()
    )
    if link is None:
        return jsonify(error="Not found"), 404
    share_links.revoke(link)
    return jsonify(ok=True)
