from filenergy import db


class User(db.Model):

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True)
    email = db.Column(db.String(120), unique=True)

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return unicode(self.id)


class File(db.Model):

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(1000))
    path = db.Column(db.String(1000))
    url = db.Column(db.String(1000))

    user_id = db.Column(db.Integer, db.ForeignKey('User.id'))
    user = db.relationship('User', backref=db.backref('files', lazy='dynamic'))