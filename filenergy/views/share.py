"""Public share-link landing + download routes.

Anyone with the token can fetch the file, subject to TTL and download cap.
"""
import mimetypes

from flask import Blueprint, abort, make_response, render_template

from filenergy.services import events, share_links
from filenergy.services.file import FileService

share_bp = Blueprint("share", __name__)


@share_bp.route("/<token>")
def landing(token):
    link = share_links.find_active(token)
    if link is None:
        abort(404)
    return render_template("share/landing.html", link=link, file=link.file)


@share_bp.route("/<token>/download")
def download(token):
    link = share_links.find_active(token)
    if link is None:
        abort(404)
    db_file = link.file
    content = FileService().get_content(db_file)
    response = make_response(content)
    mime, _ = mimetypes.guess_type(db_file.name)
    response.headers["Content-Type"] = mime or "application/octet-stream"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{db_file.name}"'
    )
    share_links.record_download(link)
    events.log_event(
        events.FILE_SHARE_DOWNLOADED,
        workspace_id=db_file.workspace_id,
        file_id=db_file.id,
        link_id=link.id,
    )
    return response
