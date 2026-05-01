import os

from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

from filenergy import settings

app = Flask(__name__)
app.config.update(settings.FLASK_CONFIG)

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = settings.LOGIN_VIEW

# CSRF on every state-changing request. The /api/v1 and /webhooks/stripe
# blueprints opt out via @csrf.exempt because they authenticate by Bearer
# token (API keys) or HMAC signature (Stripe), not session cookies.
csrf = CSRFProtect(app)

from filenergy import middleware  # noqa: E402,F401
from filenergy import models  # noqa: E402,F401
from filenergy import views  # noqa: E402,F401
from filenergy import admin  # noqa: E402,F401

migrate = Migrate(app, db)

if not os.environ.get("FILENERGY_SKIP_CREATE_ALL"):
    with app.app_context():
        db.create_all()


# Expose presence of the compiled Tailwind bundle to every template. When
# `static/css/app.css` exists (produced by `npm run build:css` during the
# Docker build), base.html links to it and skips the Play CDN.
@app.context_processor
def _inject_css_bundle_flag():
    bundle = os.path.join(app.static_folder, "css", "app.css")
    return {"css_bundle_built": os.path.isfile(bundle)}


# CLI: weekly digest send-out. Wire as `flask send-digests` (cron / k8s job).
@app.cli.command("send-digests")
def _send_digests_cli():
    from filenergy.services import digest
    n = digest.send_pending()
    print(f"sent {n} digest(s)")

