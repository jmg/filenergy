from datetime import datetime
from filenergy import db
from werkzeug.security import generate_password_hash, check_password_hash


class BaseModel(db.Model):

    created_at = db.Column(db.DateTime)

    def __init__(self, *args, **kwargs):

        kwargs.update(created_at=datetime.now())
        db.Model.__init__(self, *args, **kwargs)

    __abstract__ = True


class User(BaseModel):

    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255), unique=False)
    email = db.Column(db.String(255), unique=True)

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return unicode(self.id)

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)


class File(BaseModel):

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(1000))
    path = db.Column(db.String(1000))
    url = db.Column(db.String(1000))
    is_public = db.Column(db.Boolean(), default=False)

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref=db.backref('files', lazy='dynamic'))