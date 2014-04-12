from flask import Flask, g
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.login import LoginManager, current_user

app = Flask(__name__)

app.secret_key = "\xa90\x91\xcd\xce\xf2\xbe\x1d\x87\xbb;\xa7\xf3\x91K\xde\x05*D\x9b6\xe4U\xbf"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/test.db'

db = SQLAlchemy(app)
db.create_all()

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = '/accounts/login/'

@app.before_request
def before_request():
    g.user = current_user

import models
import views
