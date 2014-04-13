from flask import g
from flask.ext.login import current_user
from filenergy import app


@app.before_request
def before_request():
    g.user = current_user