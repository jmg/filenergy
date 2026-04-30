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

