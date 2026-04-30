"""First-run wizard. Shown once per user after register/login."""
import io

from flask import Blueprint, g, redirect, render_template, request, url_for
from flask_login import login_required

from filenergy import db
from filenergy.services.file import FileService

onboarding_bp = Blueprint("onboarding", __name__)


_SAMPLE_FILES = [
    (
        "welcome.md",
        b"# Welcome to Filenergy\n\n"
        b"This is your private file vault with a chat layer.\n\n"
        b"- **Upload** any PDF, DOCX, Markdown, or text file. We extract "
        b"the text and embed it.\n"
        b"- **Ask** questions about your library at /ask. Claude answers "
        b"with citations.\n"
        b"- **Collections** group files into notebooks; per-doc and "
        b"per-collection chat are supported.\n"
        b"- **Share** any file via a TTL'd public link from its detail "
        b"page.\n",
    ),
    (
        "filenergy-pricing.md",
        b"# Pricing\n\n"
        b"Free: 100 questions/month, 100 MB storage, 25 files.\n"
        b"Pro ($19/mo): 2,000 questions, 5 GB, 1,000 files.\n"
        b"Team ($99/mo): 20,000 questions, 100 GB, 25,000 files, 25 "
        b"members.\n",
    ),
    (
        "tips.md",
        b"# Tips\n\n"
        b"- Drag files into the upload page or use POST /api/v1/files.\n"
        b"- Ask a question scoped to a single file from its detail page.\n"
        b"- Mint API keys at /settings/keys for programmatic access.\n"
        b"- Set up webhooks at /settings/webhooks to integrate with your "
        b"stack.\n",
    ),
]


@onboarding_bp.route("/")
@login_required
def index():
    has_files = bool(g.workspace.files.first())
    return render_template(
        "onboarding/index.html",
        has_files=has_files,
        workspace_name=g.workspace.name,
    )


@onboarding_bp.route("/seed", methods=["POST"])
@login_required
def seed():
    """Drop the sample files into the workspace and index them."""
    import os
    from filenergy import settings as cfg

    os.makedirs(cfg.UPLOAD_DIR, exist_ok=True)
    svc = FileService()
    for name, body in _SAMPLE_FILES:
        path = os.path.join(cfg.UPLOAD_DIR, f"sample-{name}")
        with open(path, "wb") as fd:
            fd.write(body)
        f = svc._persist_upload(
            path=path, name=name, user=g.user, workspace_id=g.workspace.id,
            is_public=False, size_bytes=len(body),
        )
        svc.index_file(f)
    return redirect(url_for("ask.index"))


@onboarding_bp.route("/skip", methods=["POST"])
@login_required
def skip():
    return redirect(url_for("index.index"))


@onboarding_bp.route("/rename", methods=["POST"])
@login_required
def rename_workspace():
    name = (request.form.get("name") or "").strip()
    if name:
        g.workspace.name = name[:120]
        db.session.commit()
    return redirect(url_for("onboarding.index"))
