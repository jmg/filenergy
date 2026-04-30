from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from filenergy import db


def utcnow():
    return datetime.now(timezone.utc)


class BaseModel(db.Model):

    __abstract__ = True

    created_at = db.Column(db.DateTime, default=utcnow)


class User(BaseModel):

    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255))
    email = db.Column(db.String(255), unique=True)
    is_superuser = db.Column(db.Boolean(), default=False)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)

    def __str__(self):
        return self.email or self.username or f"User<{self.id}>"


class File(BaseModel):

    __tablename__ = "file"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(1000))
    path = db.Column(db.String(1000))
    url = db.Column(db.String(1024), unique=True, index=True)
    is_public = db.Column(db.Boolean(), default=False)

    indexed_at = db.Column(db.DateTime, nullable=True)
    index_error = db.Column(db.Text, nullable=True)
    text_content = db.Column(db.Text, nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user = db.relationship("User", backref=db.backref("files", lazy="dynamic"))

    chunks = db.relationship(
        "Chunk",
        backref="file",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )


class Chunk(BaseModel):
    """A retrieval-sized slice of a file's extracted text plus its embedding."""

    __tablename__ = "chunk"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), index=True)
    position = db.Column(db.Integer)
    content = db.Column(db.Text)
    # JSON-serialized list[float]; small files keep this lightweight without pgvector.
    embedding = db.Column(db.Text)
