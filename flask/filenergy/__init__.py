from flask import Flask
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.login import LoginManager

app = Flask(__name__)

app.secret_key = "\xa90\x91\xcd\xce\xf2\xbe\x1d\x87\xbb;\xa7\xf3\x91K\xde\x05*D\x9b6\xe4U\xbf"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'

db = SQLAlchemy(app)
db.create_all()

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = '/accounts/login/'

import models
import views

if __name__ == "__main__":
    app.run(debug=True)