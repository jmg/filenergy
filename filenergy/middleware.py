from flask import g
from flask.ext.login import current_user
from filenergy import app, login_manager

from filenergy.services.user import UserService


@app.before_request
def before_request():
    g.user = current_user


@login_manager.user_loader
def load_user(id):
    return UserService().get_one(id=id)