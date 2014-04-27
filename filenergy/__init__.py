from flask import Flask
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.login import LoginManager

import settings

app = Flask(__name__)

app.secret_key = settings.secret_key
app.config.update(settings.config)

db = SQLAlchemy(app)
db.create_all()

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = settings.login_view

import middleware
import models
import views
import admin