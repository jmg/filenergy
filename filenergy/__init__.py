from flask import Flask
from flask_login import LoginManager
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

with app.app_context():
    db.create_all()
