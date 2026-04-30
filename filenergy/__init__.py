import os

from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

from filenergy import settings

app = Flask(__name__)
app.config.update(settings.FLASK_CONFIG)

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = settings.LOGIN_VIEW

from filenergy import middleware  # noqa: E402,F401
from filenergy import models  # noqa: E402,F401
from filenergy import views  # noqa: E402,F401
from filenergy import admin  # noqa: E402,F401

# Wire Alembic. Production deploys should run `flask db upgrade`. Local
# dev and tests still use `db.create_all()` so the suite doesn't depend on
# Alembic state. Set FILENERGY_SKIP_CREATE_ALL=1 when running migrations
# so autogenerate can compare against an empty schema.
migrate = Migrate(app, db)

if not os.environ.get("FILENERGY_SKIP_CREATE_ALL"):
    with app.app_context():
        db.create_all()

