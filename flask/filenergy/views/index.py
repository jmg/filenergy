from filenergy import app, login_manager
from flask import render_template

from filenergy.models import User


@login_manager.user_loader
def load_user(id):
    return User.query.get(int(id))


@app.route("/")
def index():
    return render_template("index/index.html")